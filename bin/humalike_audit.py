#!/usr/bin/env python3
"""Run a Humalike social audit on a chat transcript.

WHAT AN AUDIT IS
----------------
You give it a conversation between an AI agent and a human. Humalike replays it
and answers the question the transcript cannot answer on its own: *how did that
land?* It scores the agent's social conduct, points at the specific messages
that damaged the interaction, and rewrites the worst ones.

THE THREE-CALL SHAPE, AND WHY IT IS THREE CALLS
-----------------------------------------------
``prepare`` -> (a human confirms) -> ``launch`` -> poll ``status``

The split exists because of the confirmation in the middle. An audit is scored
*from one participant's point of view*, so picking the wrong speaker as "the
agent" does not produce a slightly-off report -- it produces a report about the
wrong party. ``prepare`` parses the transcript and reports who is in it;
``launch`` will not start until someone states which of those speakers is the
agent. The server offers a guess, and the guess is usually right, but it is
offered as a default for a human to confirm, never applied silently.

``prepare`` costs credits (it runs an LLM to normalize the transcript, in
whatever format and language it arrives in). ``status`` is free -- it is a plain
read of stored state -- which is why polling it is cheap and safe.

Usage
-----
    python3 bin/humalike_audit.py prepare --file chat.txt
    python3 bin/humalike_audit.py launch --run-id <id> --agent "Nova"
    python3 bin/humalike_audit.py wait --run-id <id>
    python3 bin/humalike_audit.py show --run-id <id>

Add ``--json`` to any subcommand for machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _hcommon import (  # noqa: E402
    ApiError,
    HumalikeError,
    Transport,
    UrllibTransport,
    api_base_url,
    load_api_key,
    post_json,
)

#: The audit projection is a free read, so polling it often is fine. Five
#: seconds keeps the run feeling responsive without hammering the service.
POLL_INTERVAL_SECONDS = 5.0

#: A full audit runs several LLM stages back to back. Ten minutes is generous
#: for the largest accepted transcript and still bounded, so a stuck run
#: surfaces as a clear timeout instead of an agent that never returns.
DEFAULT_WAIT_TIMEOUT_SECONDS = 600.0

#: How long to keep waiting for rewrites after the core stages finish, when no
#: rewrite has appeared yet.
#:
#: The rewrites come from a best-effort ``compose`` stage that starts AFTER the
#: core stages, so there is a window where the run looks complete and the reply
#: list is still legitimately empty. Without this grace period the poll loop
#: reads that empty plateau as "settled" and prints a report with no rewrites --
#: the single most valuable part of the output, silently missing.
#:
#: A run whose compose stage genuinely produced nothing (it is allowed to fail
#: without failing the run) falls through after this window rather than hanging.
COMPOSE_GRACE_SECONDS = 90.0

#: The transcript ceiling the server enforces, quoted in `--help` so the limit
#: is discoverable before you paste. Deliberately NOT enforced client-side: only
#: the server's normalizer knows how many messages a blob of text contains (line
#: count is not message count), so a local guess would refuse valid transcripts.
#: The server's refusal is the authority, and it states the real count.
MESSAGE_CAP = 250


def app_base_url() -> str:
    """Origin of the web console, used to build the shareable run permalink."""
    return os.environ.get("HUMALIKE_APP_URL", "https://humalike.ai").rstrip("/")


def permalink(run_id: str) -> str:
    return f"{app_base_url()}/audit?run={run_id}"


def require_api_key() -> str:
    key = load_api_key()
    if not key:
        raise HumalikeError(
            "no Humalike API key found. Run: python3 bin/humalike_login.py"
        )
    return key


# --------------------------------------------------------------------------
# API calls
# --------------------------------------------------------------------------


def audit_prepare(transport: Transport, api_key: str, raw_text: str) -> dict[str, Any]:
    """Parse and store a transcript. Billed. Starts nothing."""
    url = f"{api_base_url()}/v1/social-observability/actions/audit_prepare"
    return post_json(transport, url, {"raw_text": raw_text}, api_key=api_key, timeout=120.0)


def audit_launch(
    transport: Transport, api_key: str, run_id: str, agent_name: str
) -> dict[str, Any]:
    """Confirm the agent and hand the run to the server.

    Returns as soon as the run is queued; the stages run server-side. Safe to
    retry -- the server treats a repeat launch of the same run as a no-op.
    """
    url = f"{api_base_url()}/v1/social-observability/actions/audit_launch"
    payload = {"run_id": run_id, "agent_name": agent_name}
    return post_json(transport, url, payload, api_key=api_key)


def audit_fetch(transport: Transport, api_key: str, run_id: str) -> dict[str, Any]:
    """Read the whole stored run. Free, no LLM -- this is the one to poll."""
    url = f"{api_base_url()}/v1/social-observability/projections/audit-run"
    return post_json(transport, url, {"run_id": run_id}, api_key=api_key)


def core_stages_complete(run: dict[str, Any]) -> bool:
    """True once all three core stages have been written.

    The server runs them in the order report -> read -> foresee, so the REPORT
    IS NOT A COMPLETION SIGNAL: it is the first stage to land. Treating it as
    "done" would return a run with a score and findings but no rewrites, which
    is exactly the interesting half missing.

    ``foresee`` (verdicts) is the last core stage, so all three being present
    is what actually means the core pipeline finished.
    """
    return (
        isinstance(run.get("report"), dict)
        and run.get("read") is not None
        and run.get("verdicts") is not None
    )


def is_finished(run: dict[str, Any]) -> bool:
    """Whether a run has enough written to render meaningfully.

    Kept as the check for the one-shot ``status``/``show`` commands, where
    there is no second sample to compare against. ``wait`` additionally
    requires the replies to settle -- see :func:`wait_for_run`.
    """
    return core_stages_complete(run)


def wait_for_run(
    transport: Transport,
    api_key: str,
    run_id: str,
    *,
    timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    interval: float = POLL_INTERVAL_SECONDS,
    sleep: Any = time.sleep,
    now: Any = time.monotonic,
    on_poll: Any = None,
) -> dict[str, Any]:
    """Poll until the run finishes, or raise on timeout.

    The rewrites are written by a best-effort ``compose`` stage that runs AFTER
    the core stages and produces a count the client cannot predict, so
    "finished" takes three conditions:

    1. the core stages are all present (see :func:`core_stages_complete`);
    2. the reply count stopped changing between two consecutive polls; and
    3. at least one rewrite has appeared, OR
       :data:`COMPOSE_GRACE_SECONDS` has passed since the core stages finished.

    Condition 3 is the subtle one. Compose has not started when the core stages
    land, so the reply list sits at zero for a while -- and a zero that has not
    changed between two polls looks exactly like a settled zero. Without the
    grace window this loop returns a report with no rewrites, which is the most
    valuable part of the output missing with no error to show for it.
    """
    started = now()
    previous_reply_count: int | None = None
    core_completed_at: float | None = None

    while True:
        run = audit_fetch(transport, api_key, run_id)
        reply_count = len(run.get("replies") or [])
        timestamp = now()

        if core_stages_complete(run):
            if core_completed_at is None:
                core_completed_at = timestamp
            settled = reply_count == previous_reply_count
            waited_long_enough = (
                reply_count > 0 or timestamp - core_completed_at >= COMPOSE_GRACE_SECONDS
            )
            if settled and waited_long_enough:
                return run

        previous_reply_count = reply_count

        if on_poll:
            on_poll(run)
        if now() - started >= timeout:
            raise HumalikeError(
                f"the audit did not finish within {timeout:.0f}s. "
                f"It may still be running -- check {permalink(run_id)}"
            )
        sleep(interval)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _rule(char: str = "-", width: int = 72) -> str:
    return char * width


def _wrap(text: str, indent: str = "  ", width: int = 72) -> str:
    return textwrap.fill(
        " ".join(str(text).split()),
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
    )


def _message_index(run: dict[str, Any]) -> list[dict[str, Any]]:
    transcript = run.get("transcript") or {}
    messages = transcript.get("messages")
    return messages if isinstance(messages, list) else []


def _message_at(run: dict[str, Any], index: Any) -> dict[str, Any] | None:
    """Resolve a positional index from a verdict or reply into its message."""
    messages = _message_index(run)
    if not isinstance(index, int) or not (0 <= index < len(messages)):
        return None
    entry = messages[index]
    return entry if isinstance(entry, dict) else None


def _message_by_id(run: dict[str, Any], message_id: Any) -> dict[str, Any] | None:
    """Findings reference messages by id rather than position."""
    if not message_id:
        return None
    for entry in _message_index(run):
        if isinstance(entry, dict) and entry.get("id") == message_id:
            return entry
    return None


def render_run(run: dict[str, Any]) -> str:
    """Render a finished run as readable text.

    Everything printed here comes from the server payload. Nothing is inferred,
    summarised, or invented by this client -- if a field is absent, the section
    is simply omitted.
    """
    lines: list[str] = []
    run_id = str(run.get("run_id", ""))
    agent = run.get("agent_name") or "(not confirmed)"
    report = run.get("report") if isinstance(run.get("report"), dict) else {}

    lines.append("")
    lines.append(_rule("="))
    lines.append(f"  HUMALIKE SOCIAL AUDIT — agent: {agent}")
    lines.append(_rule("="))

    # ---- Score -----------------------------------------------------------
    score = report.get("health_score")
    if isinstance(score, (int, float)):
        pct = round(float(score) * 100)
        lines.append("")
        lines.append(f"  Health score: {pct}/100")

    summary = report.get("summary")
    if summary:
        lines.append("")
        lines.append(_wrap(summary))

    # ---- Findings --------------------------------------------------------
    findings = report.get("findings")
    if isinstance(findings, list) and findings:
        lines.append("")
        lines.append(_rule())
        lines.append(f"  FINDINGS ({len(findings)})")
        lines.append(_rule())
        for position, finding in enumerate(findings, start=1):
            if not isinstance(finding, dict):
                continue
            severity = str(finding.get("severity") or "").upper()
            issue = finding.get("issue") or "(no description)"
            lines.append("")
            lines.append(f"  {position}. [{severity}] {issue}")

            source = _message_by_id(run, finding.get("before_message_id"))
            if source:
                lines.append("")
                lines.append(_wrap(f"said: \"{source.get('text', '')}\"", indent="     "))

            recommendation = finding.get("recommendation")
            if recommendation:
                lines.append("")
                lines.append(_wrap(f"fix: {recommendation}", indent="     "))

    # ---- Rewrites --------------------------------------------------------
    replies = run.get("replies")
    verdicts = run.get("verdicts") if isinstance(run.get("verdicts"), list) else []
    verdict_by_index = {
        v.get("index"): v for v in verdicts if isinstance(v, dict)
    }

    rewritten = [
        r
        for r in (replies or [])
        if isinstance(r, dict) and (r.get("reply") or r.get("messages"))
    ]
    if rewritten:
        lines.append("")
        lines.append(_rule())
        lines.append(f"  REWRITES ({len(rewritten)})")
        lines.append(_rule())
        lines.append("")
        lines.append(_wrap(
            "These are suggestions for what the agent could have said instead. "
            "They were not sent anywhere.",
        ))

        for reply in rewritten:
            index = reply.get("index")
            original = _message_at(run, index)
            verdict = verdict_by_index.get(index) or {}

            lines.append("")
            risk = verdict.get("risk")
            header = f"  message #{index}" if isinstance(index, int) else "  message"
            if risk:
                header = f"{header}  (risk: {risk})"
            lines.append(header)

            if verdict.get("summary"):
                lines.append(_wrap(str(verdict["summary"]), indent="     "))

            if original:
                lines.append("")
                lines.append("     actually said:")
                lines.append(_wrap(f"\"{original.get('text', '')}\"", indent="       "))

            lines.append("")
            lines.append("     Humalike would have said:")
            texts = reply.get("messages") if isinstance(reply.get("messages"), list) else None
            if texts:
                for text in texts:
                    lines.append(_wrap(f"\"{text}\"", indent="       "))
            elif reply.get("reply"):
                lines.append(_wrap(f"\"{reply['reply']}\"", indent="       "))

    # ---- Footer ----------------------------------------------------------
    lines.append("")
    lines.append(_rule())
    if run_id:
        lines.append(f"  Full report: {permalink(run_id)}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Transcript input
# --------------------------------------------------------------------------


def read_transcript(*, file_path: str | None, use_stdin: bool) -> str:
    """Load the transcript from a file or stdin.

    Read as UTF-8 with replacement rather than strict: exports from phones and
    chat apps carry all sorts of encoding debris, and refusing to read a
    transcript over one bad byte would be a poor trade. The server's normalizer
    handles the messy shape from there.
    """
    if use_stdin:
        raw = sys.stdin.read()
    elif file_path:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise HumalikeError(f"no such file: {path}")
        raw = path.read_text(encoding="utf-8", errors="replace")
    else:
        raise HumalikeError("provide a transcript with --file PATH or --stdin")

    if not raw.strip():
        raise HumalikeError("the transcript is empty")
    return raw


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------


def cmd_prepare(args: argparse.Namespace, transport: Transport) -> int:
    api_key = require_api_key()
    raw = read_transcript(file_path=args.file, use_stdin=args.stdin)

    result = audit_prepare(transport, api_key, raw)

    if args.as_json:
        print(json.dumps(result, indent=2))
        return 0

    participants = result.get("participants") or []
    guess = result.get("agent_guess")
    print()
    print(f"  Parsed {result.get('messages')} messages.")
    print(f"  Speakers: {', '.join(str(p) for p in participants)}")
    print()
    if guess:
        print(f"  Best guess at the agent: {guess}")
    print("  Confirm which speaker is the AI agent, then run:")
    print(
        f"    python3 bin/humalike_audit.py launch --run-id {result.get('run_id')} "
        f"--agent \"{guess or '<speaker>'}\""
    )
    print()
    return 0


def cmd_launch(args: argparse.Namespace, transport: Transport) -> int:
    api_key = require_api_key()
    result = audit_launch(transport, api_key, args.run_id, args.agent)

    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  Audit queued for agent \"{result.get('agent_name')}\".")
        print(f"  Run: python3 bin/humalike_audit.py wait --run-id {args.run_id}")
    return 0


def cmd_status(args: argparse.Namespace, transport: Transport) -> int:
    api_key = require_api_key()
    run = audit_fetch(transport, api_key, args.run_id)
    done = is_finished(run)

    if args.as_json:
        print(json.dumps(
            {
                "run_id": run.get("run_id"),
                "agent_name": run.get("agent_name"),
                "finished": done,
                "has_read": run.get("read") is not None,
                "verdicts": len(run.get("verdicts") or []),
                "replies": len(run.get("replies") or []),
                "has_report": isinstance(run.get("report"), dict),
            },
            indent=2,
        ))
    else:
        print(f"  Run {run.get('run_id')}: {'finished' if done else 'in progress'}")
    return 0 if done else 2


def cmd_wait(args: argparse.Namespace, transport: Transport) -> int:
    api_key = require_api_key()

    def progress(_run: dict[str, Any]) -> None:
        if not args.as_json:
            print("  ...", flush=True)

    run = wait_for_run(
        transport, api_key, args.run_id, timeout=args.timeout, on_poll=progress
    )

    if args.as_json:
        print(json.dumps(run, indent=2))
    else:
        print(render_run(run))
    return 0


def cmd_show(args: argparse.Namespace, transport: Transport) -> int:
    api_key = require_api_key()
    run = audit_fetch(transport, api_key, args.run_id)

    if args.as_json:
        print(json.dumps(run, indent=2))
        return 0

    if not is_finished(run):
        print("  This run has not finished yet. Run `wait --run-id ...` first.")
        return 2

    print(render_run(run))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="humalike_audit.py",
        description=(
            "Run a Humalike social audit on a chat transcript: score how the "
            "agent's messages landed, flag the ones that hurt, and rewrite them."
        ),
        epilog=(
            "Typical run:\n"
            "  python3 bin/humalike_audit.py prepare --file chat.txt\n"
            "  python3 bin/humalike_audit.py launch --run-id <id> --agent \"Nova\"\n"
            "  python3 bin/humalike_audit.py wait --run-id <id>\n"
            "\n"
            "`prepare` spends credits. `status`/`show` are free.\n"
            f"Transcripts are capped at {MESSAGE_CAP} messages.\n"
            "Env: HUMALIKE_API_URL, HUMALIKE_API_KEY, HUMALIKE_APP_URL."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit machine-readable JSON (use this when an agent is driving)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="parse a transcript and list its speakers (spends credits)",
        description=(
            "Parse a transcript in any format or language, store it, and report "
            "who is in it. Nothing runs until you confirm the agent and launch."
        ),
    )
    source = prepare.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="path to the transcript file")
    source.add_argument(
        "--stdin", action="store_true", help="read the transcript from stdin"
    )
    prepare.set_defaults(handler=cmd_prepare)

    launch = subparsers.add_parser(
        "launch",
        help="confirm which speaker is the agent and start the audit",
        description=(
            "Start the audit. --agent must be one of the speakers `prepare` "
            "reported, and should be confirmed by a human, not guessed."
        ),
    )
    launch.add_argument("--run-id", required=True, help="run id from `prepare`")
    launch.add_argument(
        "--agent", required=True, help="the speaker that is the AI agent"
    )
    launch.set_defaults(handler=cmd_launch)

    status = subparsers.add_parser(
        "status", help="check whether a run has finished (free)"
    )
    status.add_argument("--run-id", required=True)
    status.set_defaults(handler=cmd_status)

    wait = subparsers.add_parser(
        "wait", help="poll until the run finishes, then print the results"
    )
    wait.add_argument("--run-id", required=True)
    wait.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        help=f"seconds to wait before giving up (default {DEFAULT_WAIT_TIMEOUT_SECONDS:.0f})",
    )
    wait.set_defaults(handler=cmd_wait)

    show = subparsers.add_parser("show", help="print the results of a finished run")
    show.add_argument("--run-id", required=True)
    show.set_defaults(handler=cmd_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    transport = UrllibTransport()

    try:
        return int(args.handler(args, transport))
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except ApiError as exc:
        # The server's wording is relayed verbatim: it carries the specifics
        # (the real message count, the valid speaker names) that we would only
        # blur by rephrasing.
        message = exc.message
        if exc.is_out_of_credits:
            message = f"{message}\n  Top up at {app_base_url()} to continue."
        elif exc.is_auth_failure:
            message = f"{message}\n  Run: python3 bin/humalike_login.py"
        if args.as_json:
            print(json.dumps({"ok": False, "status": exc.status, "error": exc.message}, indent=2))
        else:
            print(f"Error: {message}", file=sys.stderr)
        return 1
    except HumalikeError as exc:
        if args.as_json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
