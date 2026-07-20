"""Tests for the device-authorization login flow.

The poll loop is the part worth testing hardest: it is the only state machine
in the repo, and getting it wrong means a user approves in their browser and
the CLI has already walked away.
"""

from __future__ import annotations

import os
import unittest

from support import FakeTransport, IsolatedConfigTestCase  # noqa: E402

import _hcommon
import humalike_login  # noqa: E402
from _hcommon import ApiError, TransportError, load_api_key  # noqa: E402
from humalike_login import (  # noqa: E402
    LoginDenied,
    LoginExpired,
    build_parser,
    check_status,
    create_session,
    poll_until_resolved,
    short_hostname,
)
from _hcommon import HumalikeError  # noqa: E402

AUTHORIZED = {
    "status": "authorized",
    "api_key": "ak_minted_key",
    "key_name": "claude-code (testbox)",
    "account": {"email": "user@example.com"},
}
PENDING = {"status": "pending", "api_key": None}


class NoSleep:
    """Collects the delays the loop asked for, without actually waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class FakeClock:
    """A monotonic clock the test advances explicitly."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class TestArgumentParsing(unittest.TestCase):
    def test_defaults(self) -> None:
        args = build_parser().parse_args([])
        self.assertFalse(args.status)
        self.assertFalse(args.as_json)
        self.assertFalse(args.no_browser)

    def test_flags(self) -> None:
        args = build_parser().parse_args(["--status", "--json", "--no-browser"])
        self.assertTrue(args.status)
        self.assertTrue(args.as_json)
        self.assertTrue(args.no_browser)


class TestShortHostname(unittest.TestCase):
    def test_returns_a_nonempty_label(self) -> None:
        name = short_hostname()
        self.assertTrue(name)
        self.assertNotIn(".", name, "the hostname should be the short form")


class TestCreateSession(IsolatedConfigTestCase):
    def test_posts_to_cli_create_with_the_public_client_id(self) -> None:
        """The lane is anonymous in the sense that no USER credential exists
        yet, but it is not unauthenticated: production answers a bare request
        with 401, so the public client identifier always goes with it. This
        test previously asserted the opposite and shipped a login that could
        not log anyone in."""
        transport = FakeTransport([(200, {"device_code": "hcd_x", "user_code": "hcu_y"})])
        create_session(transport, client="claude-code", hostname="testbox")
        call = transport.calls[0]
        self.assertTrue(call["url"].endswith("/v1/keys/actions/cli_create"))
        self.assertEqual(call["payload"]["client"], "claude-code")
        self.assertEqual(call["payload"]["hostname"], "testbox")
        self.assertEqual(
            call["headers"]["Authorization"], f"Bearer {_hcommon.GATEWAY_KEY_DEFAULT}"
        )

    def test_sends_the_gateway_key_when_one_is_configured(self) -> None:
        """Staging and local stacks carry their own key, so the env var wins
        over the published production default."""
        os.environ["HUMALIKE_CLI_GATEWAY_KEY"] = "local-cli-gateway-key"
        transport = FakeTransport([(200, {})])
        create_session(transport, client="c", hostname="h")
        self.assertEqual(
            transport.calls[0]["headers"]["Authorization"],
            "Bearer local-cli-gateway-key",
        )

    def test_uses_the_keys_url_override(self) -> None:
        os.environ["HUMALIKE_API_URL"] = "http://127.0.0.1:8010"
        os.environ["HUMALIKE_KEYS_URL"] = "http://127.0.0.1:8011"
        transport = FakeTransport([(200, {})])
        create_session(transport, client="c", hostname="h")
        self.assertTrue(transport.calls[0]["url"].startswith("http://127.0.0.1:8011"))


class TestPollLoop(IsolatedConfigTestCase):
    def _poll(self, responses, *, deadline=600):
        transport = FakeTransport(responses)
        sleep, clock = NoSleep(), FakeClock()

        def ticking_sleep(seconds: float) -> None:
            sleep(seconds)
            clock.value += seconds

        return transport, poll_until_resolved(
            transport,
            "hcd_x",
            interval=3,
            deadline_seconds=deadline,
            sleep=ticking_sleep,
            now=clock,
        )

    def test_authorized_immediately(self) -> None:
        _, result = self._poll([(200, AUTHORIZED)])
        self.assertEqual(result["api_key"], "ak_minted_key")

    def test_pending_then_authorized(self) -> None:
        transport, result = self._poll(
            [(200, PENDING), (200, PENDING), (200, AUTHORIZED)]
        )
        self.assertEqual(result["api_key"], "ak_minted_key")
        self.assertEqual(len(transport.calls), 3)

    def test_respects_the_server_interval(self) -> None:
        transport = FakeTransport([(200, PENDING), (200, AUTHORIZED)])
        sleep, clock = NoSleep(), FakeClock()
        poll_until_resolved(
            transport, "hcd_x", interval=7, deadline_seconds=600,
            sleep=sleep, now=clock,
        )
        self.assertEqual(sleep.delays, [7])

    def test_denied_raises(self) -> None:
        with self.assertRaises(LoginDenied):
            self._poll([(200, {"status": "denied"})])

    def test_expired_raises(self) -> None:
        with self.assertRaises(LoginExpired):
            self._poll([(200, {"status": "expired"})])

    def test_http_errors_are_transient_and_polling_continues(self) -> None:
        """By contract, a failed poll is not a failed login -- only the session
        TTL ends the wait. This is the regression that matters most."""
        transport, result = self._poll(
            [
                (500, {"error": {"message": "boom"}}),
                TransportError("network blip"),
                (503, {"error": {"message": "cold start"}}),
                (200, AUTHORIZED),
            ]
        )
        self.assertEqual(result["api_key"], "ak_minted_key")
        self.assertEqual(len(transport.calls), 4)

    def test_unknown_status_is_treated_as_pending(self) -> None:
        _, result = self._poll([(200, {"status": "something_new"}), (200, AUTHORIZED)])
        self.assertEqual(result["api_key"], "ak_minted_key")

    def test_gives_up_at_the_deadline(self) -> None:
        with self.assertRaises(LoginExpired):
            self._poll([(200, PENDING)] * 10, deadline=6)

    def test_authorized_without_a_key_is_reported_clearly(self) -> None:
        """The key is returned exactly once; a second collection cannot recover it."""
        with self.assertRaises(HumalikeError) as caught:
            self._poll([(200, {"status": "authorized", "api_key": None})])
        self.assertIn("already collected", str(caught.exception))

    def test_402_is_not_swallowed_as_transient(self) -> None:
        with self.assertRaises(ApiError):
            self._poll([(402, {"error": {"message": "out of credits"}})])


class TestRunLogin(IsolatedConfigTestCase):
    def test_saves_the_key_at_0600(self) -> None:
        transport = FakeTransport(
            [
                (200, {
                    "device_code": "hcd_x",
                    "user_code": "hcu_y",
                    "verification_uri": "https://humalike.ai/cli/auth?code=hcu_y",
                    "expires_in": 600,
                    "interval": 1,
                }),
                (200, AUTHORIZED),
            ]
        )
        result = humalike_login.run_login(transport, open_browser=False, as_json=True)
        self.assertTrue(result["ok"])
        self.assertEqual(load_api_key(), "ak_minted_key")
        self.assertNotIn("ak_minted_key", result["api_key_preview"])

    def test_rejects_a_session_without_a_verification_uri(self) -> None:
        transport = FakeTransport([(200, {"device_code": "hcd_x"})])
        with self.assertRaises(HumalikeError):
            humalike_login.run_login(transport, open_browser=False, as_json=True)


class TestCheckStatus(IsolatedConfigTestCase):
    def test_not_logged_in_without_credentials(self) -> None:
        status = check_status(FakeTransport([]))
        self.assertFalse(status["logged_in"])
        self.assertEqual(status["reason"], "no saved credentials")

    def test_logged_in_with_a_working_key(self) -> None:
        humalike_login.save_api_key("ak_good")
        status = check_status(FakeTransport([(200, {"user_id": "u1"})]))
        self.assertTrue(status["logged_in"])
        self.assertEqual(status["credentials_mode"], "0600")
        self.assertEqual(status["source"], "file")

    def test_rejected_key_reports_not_logged_in(self) -> None:
        humalike_login.save_api_key("ak_dead")
        status = check_status(FakeTransport([(401, {"error": {"message": "nope"}})]))
        self.assertFalse(status["logged_in"])
        self.assertEqual(status["reason"], "the saved key was rejected")

    def test_status_never_prints_the_raw_key(self) -> None:
        humalike_login.save_api_key("ak_supersecretvalue")
        status = check_status(FakeTransport([(200, {})]))
        self.assertNotIn("supersecretvalue", str(status))


if __name__ == "__main__":
    unittest.main()
