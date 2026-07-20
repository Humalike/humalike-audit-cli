"""Tests for the audit client: argument handling, the wait loop, and rendering.

The rendering tests use a payload shaped exactly like a real finished run, so
that a change to the renderer that would drop a section is caught here.
"""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout

from support import FakeTransport, IsolatedConfigTestCase  # noqa: E402

import humalike_audit  # noqa: E402
from _hcommon import ApiError, HumalikeError, save_api_key  # noqa: E402
from humalike_audit import (  # noqa: E402
    audit_launch,
    audit_prepare,
    build_parser,
    is_finished,
    permalink,
    read_transcript,
    render_run,
    require_api_key,
    wait_for_run,
)

RUN_ID = "f7b19f60-48aa-491a-b5c0-79cfdb21f7b6"

FINISHED_RUN = {
    "run_id": RUN_ID,
    "agent_name": "Nova",
    "transcript": {
        "messages": [
            {"id": "m1", "speaker": "Maya", "text": "where is my order"},
            {"id": "m2", "speaker": "Nova", "text": "I'd be absolutely delighted to help!"},
            {"id": "m3", "speaker": "Maya", "text": "that is not an answer"},
        ]
    },
    "read": {"prompt_block": "..."},
    "verdicts": [
        {
            "index": 1,
            "risk": "high",
            "summary": "Maya becomes annoyed by the lack of detail.",
            "predicted_message": "still waiting",
        }
    ],
    "report": {
        "health_score": 0.1,
        "summary": "Maya is at high risk of churning.",
        "findings": [
            {
                "issue": "Excessive emotional mirroring",
                "severity": "high",
                "recommendation": "Answer the question in the first sentence.",
                "before_message_id": "m2",
            }
        ],
    },
    "replies": [
        {
            "index": 1,
            "reply": None,
            "messages": ["your order is in transit", "want me to pull the tracking?"],
        }
    ],
}

UNFINISHED_RUN = {
    "run_id": RUN_ID,
    "agent_name": "Nova",
    "transcript": {"messages": [{"id": "m1", "speaker": "Maya", "text": "hi"}]},
    "read": None,
    "verdicts": None,
    "report": None,
    "replies": [],
}


class TestArgumentParsing(unittest.TestCase):
    def test_prepare_requires_a_transcript_source(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["prepare"])

    def test_prepare_rejects_both_sources_at_once(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["prepare", "--file", "a.txt", "--stdin"])

    def test_prepare_with_file(self) -> None:
        args = build_parser().parse_args(["prepare", "--file", "chat.txt"])
        self.assertEqual(args.file, "chat.txt")
        self.assertFalse(args.stdin)

    def test_launch_requires_run_id_and_agent(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["launch", "--run-id", RUN_ID])
        args = build_parser().parse_args(
            ["launch", "--run-id", RUN_ID, "--agent", "Nova"]
        )
        self.assertEqual(args.agent, "Nova")

    def test_json_is_a_global_flag(self) -> None:
        args = build_parser().parse_args(["--json", "show", "--run-id", RUN_ID])
        self.assertTrue(args.as_json)

    def test_a_subcommand_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args([])

    def test_wait_has_a_default_timeout(self) -> None:
        args = build_parser().parse_args(["wait", "--run-id", RUN_ID])
        self.assertEqual(args.timeout, humalike_audit.DEFAULT_WAIT_TIMEOUT_SECONDS)


class TestTranscriptInput(IsolatedConfigTestCase):
    def test_reads_a_file(self) -> None:
        path = os.path.join(self._tempdir.name, "chat.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Maya: hello\nNova: hi\n")
        self.assertIn("Maya: hello", read_transcript(file_path=path, use_stdin=False))

    def test_missing_file_is_reported_clearly(self) -> None:
        with self.assertRaises(HumalikeError) as caught:
            read_transcript(file_path="/nope/missing.txt", use_stdin=False)
        self.assertIn("no such file", str(caught.exception))

    def test_empty_transcript_is_rejected_before_spending_credits(self) -> None:
        path = os.path.join(self._tempdir.name, "empty.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("   \n\n")
        with self.assertRaises(HumalikeError):
            read_transcript(file_path=path, use_stdin=False)

    def test_undecodable_bytes_do_not_break_the_read(self) -> None:
        """Phone exports carry encoding debris; refusing over one bad byte
        would be a poor trade."""
        path = os.path.join(self._tempdir.name, "messy.txt")
        with open(path, "wb") as handle:
            handle.write(b"Maya: caf\xe9 talk\n")
        self.assertIn("Maya", read_transcript(file_path=path, use_stdin=False))

    def test_no_source_is_an_error(self) -> None:
        with self.assertRaises(HumalikeError):
            read_transcript(file_path=None, use_stdin=False)


class TestApiCalls(IsolatedConfigTestCase):
    def test_prepare_posts_raw_text_with_auth(self) -> None:
        transport = FakeTransport([(200, {"run_id": RUN_ID, "messages": 3})])
        audit_prepare(transport, "ak_key", "Maya: hi")
        call = transport.calls[0]
        self.assertTrue(call["url"].endswith("/actions/audit_prepare"))
        self.assertEqual(call["payload"], {"raw_text": "Maya: hi"})
        self.assertEqual(call["headers"]["Authorization"], "Bearer ak_key")

    def test_launch_posts_run_id_and_agent(self) -> None:
        transport = FakeTransport([(200, {"status": "queued"})])
        audit_launch(transport, "ak_key", RUN_ID, "Nova")
        self.assertEqual(
            transport.calls[0]["payload"], {"run_id": RUN_ID, "agent_name": "Nova"}
        )

    def test_a_bad_agent_name_surfaces_the_server_message_verbatim(self) -> None:
        transport = FakeTransport(
            [(400, {"error": {"message": "'Bob' never speaks in this transcript"}})]
        )
        with self.assertRaises(ApiError) as caught:
            audit_launch(transport, "ak_key", RUN_ID, "Bob")
        self.assertEqual(caught.exception.message, "'Bob' never speaks in this transcript")

    def test_require_api_key_points_at_login_when_absent(self) -> None:
        with self.assertRaises(HumalikeError) as caught:
            require_api_key()
        self.assertIn("humalike_login.py", str(caught.exception))

    def test_require_api_key_returns_the_saved_key(self) -> None:
        save_api_key("ak_saved")
        self.assertEqual(require_api_key(), "ak_saved")


#: The server writes the report FIRST (order: report -> read -> foresee), so a
#: run can carry a full report while the rewrites do not exist yet.
REPORT_ONLY_RUN = dict(UNFINISHED_RUN, report=FINISHED_RUN["report"])


class TestFinishDetection(unittest.TestCase):
    def test_a_run_with_all_core_stages_is_finished(self) -> None:
        self.assertTrue(is_finished(FINISHED_RUN))

    def test_a_run_with_nothing_is_not(self) -> None:
        self.assertFalse(is_finished(UNFINISHED_RUN))

    def test_a_report_alone_is_not_finished(self) -> None:
        """The regression that matters: the report is the FIRST stage written,
        not the last. Treating it as done returns a run with no rewrites."""
        self.assertFalse(is_finished(REPORT_ONLY_RUN))

    def test_missing_verdicts_is_not_finished(self) -> None:
        """foresee is the last core stage."""
        self.assertFalse(is_finished(dict(FINISHED_RUN, verdicts=None)))

    def test_missing_read_is_not_finished(self) -> None:
        self.assertFalse(is_finished(dict(FINISHED_RUN, read=None)))

    def test_empty_replies_do_not_block_completion(self) -> None:
        """A clean conversation legitimately has nothing worth rewriting."""
        self.assertTrue(is_finished(dict(FINISHED_RUN, replies=[])))


class TestWaitLoop(IsolatedConfigTestCase):
    def _wait(self, responses):
        transport = FakeTransport(responses)
        run = wait_for_run(
            transport, "ak_key", RUN_ID, sleep=lambda _s: None, now=lambda: 0.0
        )
        return transport, run

    def test_polls_until_all_stages_land_and_replies_settle(self) -> None:
        transport, run = self._wait(
            [(200, UNFINISHED_RUN), (200, FINISHED_RUN), (200, FINISHED_RUN)]
        )
        self.assertEqual(run["run_id"], RUN_ID)
        self.assertEqual(len(transport.calls), 3)

    def test_does_not_return_on_the_report_alone(self) -> None:
        """Otherwise `wait` prints a score and findings with no rewrites."""
        transport, run = self._wait(
            [(200, REPORT_ONLY_RUN), (200, FINISHED_RUN), (200, FINISHED_RUN)]
        )
        self.assertEqual(len(run["replies"]), 1)
        self.assertEqual(len(transport.calls), 3)

    def test_waits_for_replies_to_stop_growing(self) -> None:
        """compose runs after the core stages and streams replies in."""
        growing = dict(FINISHED_RUN, replies=[])
        half = dict(FINISHED_RUN, replies=FINISHED_RUN["replies"])
        transport, run = self._wait(
            [(200, growing), (200, half), (200, half)]
        )
        self.assertEqual(len(run["replies"]), 1)
        self.assertEqual(len(transport.calls), 3)

    def test_an_empty_reply_list_does_not_end_the_wait_early(self) -> None:
        """compose starts AFTER the core stages, so replies sit at zero for a
        while. A stable zero inside the grace window is not 'settled' -- this
        is the regression that produced reports with no rewrites."""
        empty = dict(FINISHED_RUN, replies=[])
        transport = FakeTransport(
            [(200, empty), (200, empty), (200, FINISHED_RUN), (200, FINISHED_RUN)]
        )
        run = wait_for_run(
            transport, "ak_key", RUN_ID, sleep=lambda _s: None, now=lambda: 0.0
        )
        self.assertEqual(len(run["replies"]), 1)

    def test_a_genuinely_empty_compose_falls_through_after_the_grace_window(self) -> None:
        """compose may fail without failing the run; we must not hang on it."""
        empty = dict(FINISHED_RUN, replies=[])
        clock = iter([0.0, 0.0, 1000.0, 1000.0, 2000.0, 2000.0])
        transport = FakeTransport([(200, empty)] * 6)
        run = wait_for_run(
            transport, "ak_key", RUN_ID,
            timeout=100_000.0, sleep=lambda _s: None, now=lambda: next(clock),
        )
        self.assertEqual(run["replies"], [])

    def test_polls_the_free_projection_endpoint(self) -> None:
        transport, _ = self._wait([(200, FINISHED_RUN), (200, FINISHED_RUN)])
        self.assertTrue(transport.calls[0]["url"].endswith("/projections/audit-run"))

    def test_timeout_mentions_the_permalink(self) -> None:
        clock = iter([0.0, 0.0, 999.0, 999.0])
        transport = FakeTransport([(200, UNFINISHED_RUN)] * 4)
        with self.assertRaises(HumalikeError) as caught:
            wait_for_run(
                transport, "ak_key", RUN_ID,
                timeout=10.0, sleep=lambda _s: None, now=lambda: next(clock),
            )
        self.assertIn(RUN_ID, str(caught.exception))


class TestPermalink(IsolatedConfigTestCase):
    def test_default_origin(self) -> None:
        os.environ.pop("HUMALIKE_APP_URL", None)
        self.assertEqual(permalink(RUN_ID), f"https://humalike.ai/audit?run={RUN_ID}")

    def test_origin_is_overridable(self) -> None:
        os.environ["HUMALIKE_APP_URL"] = "http://localhost:3000/"
        self.assertEqual(permalink(RUN_ID), f"http://localhost:3000/audit?run={RUN_ID}")


class TestRendering(IsolatedConfigTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.output = render_run(FINISHED_RUN)

    def test_shows_the_agent_and_score(self) -> None:
        self.assertIn("Nova", self.output)
        self.assertIn("10/100", self.output)

    def test_shows_the_summary(self) -> None:
        self.assertIn("high risk of churning", self.output)

    def test_shows_findings_with_severity_and_fix(self) -> None:
        self.assertIn("FINDINGS (1)", self.output)
        self.assertIn("[HIGH]", self.output)
        self.assertIn("Excessive emotional mirroring", self.output)
        self.assertIn("Answer the question in the first sentence.", self.output)

    def test_finding_quotes_the_message_it_points_at(self) -> None:
        """before_message_id is an id, so this exercises the id lookup."""
        self.assertIn("absolutely delighted", self.output)

    def test_rewrites_pair_the_original_with_the_replacement(self) -> None:
        self.assertIn("actually said:", self.output)
        self.assertIn("Humalike would have said:", self.output)
        self.assertIn("your order is in transit", self.output)

    def test_rewrite_resolves_the_original_by_position(self) -> None:
        """replies[].index is positional into transcript.messages."""
        self.assertIn("message #1", self.output)

    def test_shows_the_verdict_risk(self) -> None:
        self.assertIn("risk: high", self.output)

    def test_states_that_rewrites_were_not_sent(self) -> None:
        """An honest product must never imply it acted on the user's behalf."""
        self.assertIn("not sent anywhere", self.output)

    def test_ends_with_the_permalink(self) -> None:
        self.assertIn(f"/audit?run={RUN_ID}", self.output)

    def test_a_single_string_reply_renders(self) -> None:
        run = dict(FINISHED_RUN, replies=[{"index": 1, "reply": "just say the thing", "messages": None}])
        self.assertIn("just say the thing", render_run(run))

    def test_a_run_with_no_rewrites_omits_the_section(self) -> None:
        run = dict(FINISHED_RUN, replies=[])
        rendered = render_run(run)
        self.assertNotIn("REWRITES", rendered)
        self.assertIn("FINDINGS", rendered)

    def test_a_report_without_findings_omits_the_section(self) -> None:
        run = dict(FINISHED_RUN, report={"health_score": 0.9, "summary": "clean", "findings": []})
        rendered = render_run(run)
        self.assertNotIn("FINDINGS", rendered)
        self.assertIn("90/100", rendered)

    def test_an_out_of_range_index_does_not_crash(self) -> None:
        """The renderer must survive a payload it did not expect."""
        run = dict(FINISHED_RUN, replies=[{"index": 99, "reply": "x", "messages": None}])
        self.assertIn("x", render_run(run))


class TestShowCommand(IsolatedConfigTestCase):
    def test_show_refuses_an_unfinished_run(self) -> None:
        save_api_key("ak_key")
        args = build_parser().parse_args(["show", "--run-id", RUN_ID])
        transport = FakeTransport([(200, UNFINISHED_RUN)])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = humalike_audit.cmd_show(args, transport)
        self.assertEqual(code, 2)
        self.assertIn("has not finished", buffer.getvalue())

    def test_show_renders_a_finished_run(self) -> None:
        save_api_key("ak_key")
        args = build_parser().parse_args(["show", "--run-id", RUN_ID])
        transport = FakeTransport([(200, FINISHED_RUN)])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = humalike_audit.cmd_show(args, transport)
        self.assertEqual(code, 0)
        self.assertIn("HUMALIKE SOCIAL AUDIT", buffer.getvalue())


class TestExitCodes(IsolatedConfigTestCase):
    def test_missing_key_exits_nonzero(self) -> None:
        code = humalike_audit.main(["show", "--run-id", RUN_ID])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
