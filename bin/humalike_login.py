#!/usr/bin/env python3
"""Log a machine into Humalike and save an API key.

WHY A DEVICE FLOW
-----------------
The person running this is usually inside an agent session (Claude Code, Codex,
Cursor), and the agent cannot be handed a password, cannot click a consent
button, and must never see the user's Humalike credentials. So we use the same
shape as ``gh auth login`` and every TV-app sign-in:

  1. This script asks the API for a short-lived session and gets back a URL
     plus a short user code.
  2. The HUMAN opens that URL in a browser they already trust, signs in or signs
     up (Clerk handles both -- there is no separate "create an account" step),
     and approves.
  3. This script polls until the approval lands, then saves the minted key.

The agent orchestrates, but only the human ever authenticates. The API key is
minted at the end and is the only secret that ever touches this machine.

POLLING IS DELIBERATELY STUBBORN
--------------------------------
By contract, an HTTP error mid-poll is transient -- the session's own TTL is the
real deadline, not any single failed request. So the loop keeps going on network
errors and 5xx responses, and stops only on a definite answer (authorized,
denied, expired) or when the session expires. Getting this wrong would mean a
user approves in the browser and the CLI has already given up.

SPLITTING THE FLOW FOR AGENTS
----------------------------
Run plain, this blocks: it prints the link and then polls until the human
approves. That is right for a person at a terminal and wrong for an agent,
whose shell call would not return until approval -- so the agent could not
relay the link to the human it is waiting on. Deadlock.

``--begin`` and ``--resume`` cut that in half. ``--begin`` creates the session,
saves it, prints the link and exits immediately; the agent relays the link.
``--resume`` picks the same session back up and polls it to completion. One
poller, no orphaned background process, and the human always has the link
before anything blocks.

Usage
-----
    python3 bin/humalike_login.py            # run the flow (blocks)
    python3 bin/humalike_login.py --begin    # print the link, exit now
    python3 bin/humalike_login.py --resume   # poll the session --begin started
    python3 bin/humalike_login.py --status   # is there a working key already?
    python3 bin/humalike_login.py --json     # machine-readable, for agents
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _hcommon import (  # noqa: E402
    ApiError,
    HumalikeError,
    Transport,
    TransportError,
    UrllibTransport,
    api_base_url,
    cli_gateway_key,
    credentials_mode,
    credentials_path,
    keys_base_url,
    load_api_key,
    post_json,
    redact,
    save_api_key,
    verify_api_key,
)

CLIENT_NAME = "claude-code"

#: Fallback cadence if the server does not state one. The server's ``interval``
#: always wins: it is the API telling us how often it is willing to be asked.
DEFAULT_POLL_INTERVAL_SECONDS = 3

#: Hard ceiling on the wait, independent of what the server reports, so a bad
#: ``expires_in`` can never wedge an agent session forever.
MAX_WAIT_SECONDS = 900


def short_hostname() -> str:
    """A recognisable name for this machine, shown on the approval page.

    This is how the user tells "the laptop I am sitting at" apart from some
    other session, so it is worth getting right -- but it is cosmetic, and any
    failure here must not block a login.
    """
    try:
        name = socket.gethostname().split(".")[0].strip()
    except OSError:
        name = ""
    return name or "unknown-host"


def create_session(transport: Transport, *, client: str, hostname: str) -> dict[str, Any]:
    """Start a device-authorization session.

    This lane is anonymous: there is nothing to authenticate with yet, which is
    the entire reason the flow exists.
    """
    url = f"{keys_base_url()}/v1/keys/actions/cli_create"
    payload = {
        "client": client,
        "hostname": hostname,
        "os": platform.system() or None,
    }
    return post_json(transport, url, payload, api_key=cli_gateway_key())


def poll_once(transport: Transport, device_code: str) -> dict[str, Any]:
    """Ask whether the human has approved yet.

    Retries are disabled here because the surrounding loop *is* the retry: it
    already paces itself to the server's interval and owns the deadline.
    """
    url = f"{keys_base_url()}/v1/keys/actions/cli_poll"
    return post_json(
        transport,
        url,
        {"device_code": device_code},
        api_key=cli_gateway_key(),
        retries=1,
    )


class LoginDenied(HumalikeError):
    """The human explicitly rejected the request on the approval page."""


class LoginExpired(HumalikeError):
    """Nobody approved in time, or the device code is not recognised."""


def poll_until_resolved(
    transport: Transport,
    device_code: str,
    *,
    interval: int,
    deadline_seconds: int,
    sleep: Any = time.sleep,
    now: Any = time.monotonic,
) -> dict[str, Any]:
    """Poll until the session resolves; return the payload that carried the key.

    ``sleep`` and ``now`` are injected so the tests can drive the whole state
    machine -- pending, then authorized, then a transport blip -- instantly.
    """
    started = now()
    interval = max(1, interval)

    while True:
        try:
            body = poll_once(transport, device_code)
            status = str(body.get("status") or "").lower()

            if status == "authorized":
                if not body.get("api_key"):
                    # The key is returned exactly once, on this transition. If
                    # it is absent, a previous poll already consumed it and the
                    # key is unrecoverable -- say so rather than looping.
                    raise HumalikeError(
                        "the session was approved but no key came back; it was likely "
                        "already collected. Run login again to mint a fresh one."
                    )
                return body
            if status == "denied":
                raise LoginDenied("the sign-in request was denied in the browser.")
            if status == "expired":
                raise LoginExpired("the sign-in request expired before it was approved.")
            # "pending", or a status this client does not know: keep waiting.

        except (TransportError, ApiError) as exc:
            # By contract these are transient during polling. The session TTL,
            # enforced below, is the only real deadline.
            if isinstance(exc, ApiError) and exc.status == 402:
                raise
        if now() - started >= deadline_seconds:
            raise LoginExpired("timed out waiting for the sign-in to be approved.")

        sleep(interval)


def session_path() -> Path:
    """Where ``--begin`` parks the pending session for ``--resume`` to pick up.

    Sits beside the credentials file so ``HUMALIKE_CONFIG_DIR`` redirects both
    together and no test can touch a real home directory.
    """
    return credentials_path().parent / "login-session.json"


def save_session(session: dict[str, Any]) -> Path:
    """Persist a pending session, 0600.

    The device code is not an API key, but it is a bearer token for a sign-in
    in flight, so it gets the same treatment as one.
    """
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session), encoding="utf-8")
    path.chmod(0o600)
    return path


def load_session() -> dict[str, Any]:
    """Read back the pending session, or explain why there is not one."""
    path = session_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HumalikeError(
            "no sign-in is in progress. Run `humalike_login.py --begin` first."
        ) from None
    except (OSError, ValueError) as exc:
        raise HumalikeError(f"the pending sign-in session is unreadable: {exc}") from None
    if not isinstance(data, dict) or not data.get("device_code"):
        raise HumalikeError(
            "the pending sign-in session is incomplete. Run `--begin` again."
        )
    return data


def clear_session() -> None:
    """Drop a session that has served its purpose. Never fatal."""
    try:
        session_path().unlink()
    except OSError:
        pass


def begin_login(transport: Transport, *, open_browser: bool) -> dict[str, Any]:
    """Create a sign-in session, save it, and return the human-facing details.

    Deliberately does not poll: the caller needs the link in hand *now* so it
    can be handed to the person who has to click it.
    """
    session = create_session(transport, client=CLIENT_NAME, hostname=short_hostname())

    verification_uri = str(session.get("verification_uri") or "")
    device_code = str(session.get("device_code") or "")
    if not device_code or not verification_uri:
        raise HumalikeError("the server did not return a usable sign-in session.")

    pending = {
        "verification_uri": verification_uri,
        "user_code": str(session.get("user_code") or ""),
        "device_code": device_code,
        "interval": int(session.get("interval") or DEFAULT_POLL_INTERVAL_SECONDS),
        "expires_in": int(session.get("expires_in") or MAX_WAIT_SECONDS),
    }
    save_session(pending)

    if open_browser:
        try:
            webbrowser.open(verification_uri)
        except Exception:  # noqa: BLE001 - a missing browser must never be fatal
            pass

    return pending


def print_session_block(pending: dict[str, Any]) -> None:
    """Print the link the human must open.

    Always printed, even when a browser opened: half the people running this
    are on a headless box or over SSH, where the browser call silently does
    nothing and this text is the only way through.
    """
    print()
    print("  Sign in to Humalike to continue.")
    print()
    print(f"    Open:      {pending['verification_uri']}")
    if pending.get("user_code"):
        print(f"    Your code: {pending['user_code']}")
    print()
    print("  New to Humalike? Signing in on that page creates your account.")
    print()


def finish_login(transport: Transport, pending: dict[str, Any]) -> dict[str, Any]:
    """Poll a pending session to completion and persist the minted key."""
    deadline = min(int(pending.get("expires_in") or MAX_WAIT_SECONDS), MAX_WAIT_SECONDS)
    result = poll_until_resolved(
        transport,
        str(pending["device_code"]),
        interval=int(pending.get("interval") or DEFAULT_POLL_INTERVAL_SECONDS),
        deadline_seconds=deadline,
    )
    clear_session()

    api_key = str(result["api_key"])
    account = result.get("account") if isinstance(result.get("account"), dict) else None
    path = save_api_key(api_key, account=account)

    return {
        "ok": True,
        "credentials_path": str(path),
        "api_key_preview": redact(api_key),
        "account": account,
        "key_name": result.get("key_name"),
    }


def run_login(transport: Transport, *, open_browser: bool, as_json: bool) -> dict[str, Any]:
    """The blocking flow: create, show the link, then wait for approval."""
    pending = begin_login(transport, open_browser=open_browser)
    if not as_json:
        print_session_block(pending)
        print("  Waiting for approval... (Ctrl-C to cancel)")
        print()
    return finish_login(transport, pending)


def check_status(transport: Transport) -> dict[str, Any]:
    """Report whether a usable key is already on this machine."""
    api_key = load_api_key()
    if not api_key:
        return {"logged_in": False, "reason": "no saved credentials"}

    working = verify_api_key(transport, api_key)
    mode = credentials_mode()
    return {
        "logged_in": working,
        "reason": None if working else "the saved key was rejected",
        "api_key_preview": redact(api_key),
        "credentials_path": str(credentials_path()),
        "credentials_mode": f"{mode:04o}" if mode is not None else None,
        "source": "environment" if _key_from_env() else "file",
    }


def _key_from_env() -> bool:
    import os

    return bool(os.environ.get("HUMALIKE_API_KEY", "").strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="humalike_login.py",
        description=(
            "Sign in to Humalike from a terminal and save an API key. "
            "Prints a URL for you to open; approving it there creates your "
            "account if you do not have one yet."
        ),
        epilog=(
            "The key is saved to ~/.humalike/credentials with mode 0600. "
            "Set HUMALIKE_API_KEY to override it for a single run.\n"
            "Env: HUMALIKE_API_URL (default https://api.humalike.com), "
            "HUMALIKE_KEYS_URL (dev-only split-port override)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="check whether a working key is already saved, then exit",
    )
    parser.add_argument(
        "--begin",
        action="store_true",
        help="start a sign-in, print the link, and exit without waiting",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="wait for the sign-in that --begin started to be approved",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit machine-readable JSON (use this when an agent is driving)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="never try to open a browser; just print the URL",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    transport = UrllibTransport()

    try:
        if args.status:
            status = check_status(transport)
            if args.as_json:
                print(json.dumps(status, indent=2))
            elif status["logged_in"]:
                print(f"Signed in ({status['api_key_preview']}, from {status['source']}).")
            else:
                print(f"Not signed in: {status['reason']}.")
            return 0 if status["logged_in"] else 1

        if args.begin:
            pending = begin_login(transport, open_browser=not args.no_browser)
            if args.as_json:
                # The device code stays out of this: the caller does not need
                # it (--resume reads it from disk) and it does not belong in an
                # agent's transcript.
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "verification_uri": pending["verification_uri"],
                            "user_code": pending["user_code"],
                            "expires_in": pending["expires_in"],
                        },
                        indent=2,
                    )
                )
            else:
                print_session_block(pending)
            return 0

        if args.resume:
            result = finish_login(transport, load_session())
            if args.as_json:
                print(json.dumps(result, indent=2))
            else:
                print(f"  Signed in. Key saved to {result['credentials_path']} (mode 0600).")
                print()
            return 0

        result = run_login(
            transport, open_browser=not args.no_browser, as_json=args.as_json
        )
        if args.as_json:
            print(json.dumps(result, indent=2))
        else:
            print(f"  Signed in. Key saved to {result['credentials_path']} (mode 0600).")
            print()
        return 0

    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except HumalikeError as exc:
        if args.as_json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
