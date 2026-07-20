"""Tests for the shared plumbing: URLs, HTTP behaviour, and credential storage."""

from __future__ import annotations

import json
import os
import stat
import unittest

from support import FakeTransport, IsolatedConfigTestCase  # noqa: E402

import _hcommon  # noqa: E402
from _hcommon import (  # noqa: E402
    ApiError,
    TransportError,
    api_base_url,
    credentials_path,
    extract_error_message,
    keys_base_url,
    load_api_key,
    post_json,
    redact,
    save_api_key,
    verify_api_key,
)


class TestBaseUrls(IsolatedConfigTestCase):
    def test_defaults_to_production(self) -> None:
        os.environ.pop("HUMALIKE_API_URL", None)
        self.assertEqual(api_base_url(), "https://api.humalike.com")

    def test_trailing_slash_is_stripped(self) -> None:
        os.environ["HUMALIKE_API_URL"] = "http://127.0.0.1:8010/"
        self.assertEqual(api_base_url(), "http://127.0.0.1:8010")

    def test_keys_url_follows_api_url_by_default(self) -> None:
        """In production both live behind one gateway, so one var configures both."""
        os.environ["HUMALIKE_API_URL"] = "https://api.example.com"
        os.environ.pop("HUMALIKE_KEYS_URL", None)
        self.assertEqual(keys_base_url(), "https://api.example.com")

    def test_keys_url_can_be_split_for_local_dev(self) -> None:
        """Locally the services are separate containers on separate ports."""
        os.environ["HUMALIKE_API_URL"] = "http://127.0.0.1:8010"
        os.environ["HUMALIKE_KEYS_URL"] = "http://127.0.0.1:8011"
        self.assertEqual(api_base_url(), "http://127.0.0.1:8010")
        self.assertEqual(keys_base_url(), "http://127.0.0.1:8011")


class TestErrorEnvelope(unittest.TestCase):
    def test_extracts_server_message(self) -> None:
        message, code = extract_error_message(
            400, {"error": {"code": "VALIDATION_ERROR", "message": "bad transcript"}}
        )
        self.assertEqual(message, "bad transcript")
        self.assertEqual(code, "VALIDATION_ERROR")

    def test_appends_field_details(self) -> None:
        """Details carry the actionable part of a cap violation."""
        message, _ = extract_error_message(
            400,
            {
                "error": {
                    "message": "the audit accepts at most 250 messages",
                    "details": [
                        {"field": "raw_text", "message": "over the 250-message cap"}
                    ],
                }
            },
        )
        self.assertIn("at most 250 messages", message)
        self.assertIn("over the 250-message cap", message)

    def test_falls_back_when_body_is_not_an_envelope(self) -> None:
        message, code = extract_error_message(502, {})
        self.assertEqual(message, "HTTP 502")
        self.assertIsNone(code)


class TestPostJson(unittest.TestCase):
    def test_returns_body_on_success(self) -> None:
        transport = FakeTransport([(200, {"run_id": "abc"})])
        result = post_json(transport, "http://x/y", {"a": 1})
        self.assertEqual(result, {"run_id": "abc"})

    def test_sends_bearer_token(self) -> None:
        transport = FakeTransport([(200, {})])
        post_json(transport, "http://x/y", {}, api_key="ak_secret")
        self.assertEqual(
            transport.calls[0]["headers"]["Authorization"], "Bearer ak_secret"
        )

    def test_omits_auth_header_when_anonymous(self) -> None:
        """cli_create has nothing to authenticate with yet."""
        transport = FakeTransport([(200, {})])
        post_json(transport, "http://x/y", {})
        self.assertNotIn("Authorization", transport.calls[0]["headers"])

    def test_4xx_raises_immediately_without_retrying(self) -> None:
        """A deliberate refusal will not change on retry."""
        transport = FakeTransport([(400, {"error": {"message": "nope"}})])
        with self.assertRaises(ApiError) as caught:
            post_json(transport, "http://x/y", {}, retries=3, sleep=lambda _s: None)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.message, "nope")
        self.assertEqual(len(transport.calls), 1)

    def test_5xx_is_retried_then_succeeds(self) -> None:
        transport = FakeTransport([(503, {}), (200, {"ok": True})])
        result = post_json(transport, "http://x/y", {}, retries=3, sleep=lambda _s: None)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(transport.calls), 2)

    def test_transport_error_is_retried_then_raised(self) -> None:
        transport = FakeTransport([TransportError("down"), TransportError("down")])
        with self.assertRaises(TransportError):
            post_json(transport, "http://x/y", {}, retries=2, sleep=lambda _s: None)
        self.assertEqual(len(transport.calls), 2)

    def test_402_is_flagged_as_out_of_credits(self) -> None:
        transport = FakeTransport([(402, {"error": {"message": "out of credits"}})])
        with self.assertRaises(ApiError) as caught:
            post_json(transport, "http://x/y", {})
        self.assertTrue(caught.exception.is_out_of_credits)
        self.assertFalse(caught.exception.is_auth_failure)

    def test_401_is_flagged_as_auth_failure(self) -> None:
        transport = FakeTransport([(401, {"error": {"message": "bad key"}})])
        with self.assertRaises(ApiError) as caught:
            post_json(transport, "http://x/y", {})
        self.assertTrue(caught.exception.is_auth_failure)


class TestCredentials(IsolatedConfigTestCase):
    def test_save_then_load_round_trips(self) -> None:
        save_api_key("ak_live_123")
        self.assertEqual(load_api_key(), "ak_live_123")

    def test_saved_file_is_owner_only(self) -> None:
        """A world-readable key file is a real leak on a shared machine."""
        path = save_api_key("ak_live_123")
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0600, got {mode:04o}")

    def test_saved_file_is_json_with_account(self) -> None:
        path = save_api_key("ak_live_123", account={"email": "a@b.com"})
        document = json.loads(path.read_text())
        self.assertEqual(document["api_key"], "ak_live_123")
        self.assertEqual(document["account"], {"email": "a@b.com"})

    def test_overwriting_preserves_permissions(self) -> None:
        save_api_key("ak_first")
        path = save_api_key("ak_second")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(load_api_key(), "ak_second")

    def test_missing_file_yields_none(self) -> None:
        self.assertIsNone(load_api_key())

    def test_environment_variable_wins_over_file(self) -> None:
        """The env var is the escape hatch for CI and for driving a second account."""
        save_api_key("ak_from_file")
        os.environ["HUMALIKE_API_KEY"] = "ak_from_env"
        self.assertEqual(load_api_key(), "ak_from_env")

    def test_plain_text_key_file_is_tolerated(self) -> None:
        """Someone may reasonably paste the key in by hand."""
        path = credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("  ak_handwritten  \n")
        self.assertEqual(load_api_key(), "ak_handwritten")


class TestRedact(unittest.TestCase):
    def test_long_key_shows_only_the_ends(self) -> None:
        rendered = redact("ak_live_abcdefghijklmnop")
        self.assertNotIn("abcdefghij", rendered)
        self.assertTrue(rendered.startswith("ak_liv"))

    def test_short_key_is_fully_masked(self) -> None:
        self.assertEqual(redact("abc"), "***")

    def test_missing_key_is_labelled(self) -> None:
        self.assertEqual(redact(None), "<none>")



class ExtractErrorMessageDetailShapes(unittest.TestCase):
    """Both detail shapes must survive: the services send {field, message},
    the framework's request validation sends pydantic's {loc, msg}. Dropping
    the latter left a mistyped run id reading "request validation failed"
    with no clue which field or why."""

    def test_pydantic_loc_msg_detail_is_rendered(self) -> None:
        message, code = extract_error_message(
            422,
            {
                "error": {
                    "code": "validation_failed",
                    "message": "request validation failed",
                    "details": [
                        {"loc": ["body", "run_id"], "msg": "Input should be a valid UUID"}
                    ],
                }
            },
        )
        self.assertEqual(
            message, "request validation failed (run_id: Input should be a valid UUID)"
        )
        self.assertEqual(code, "validation_failed")

    def test_service_field_message_detail_still_rendered(self) -> None:
        message, _ = extract_error_message(
            400,
            {
                "error": {
                    "message": "This transcript has 300 messages; the audit accepts at most 250.",
                    "details": [{"field": "raw_text", "message": "over the 250-message cap"}],
                }
            },
        )
        self.assertIn("(raw_text: over the 250-message cap)", message)

    def test_detail_without_any_text_is_skipped(self) -> None:
        message, _ = extract_error_message(
            400, {"error": {"message": "nope", "details": [{"loc": ["body"]}, "junk"]}}
        )
        self.assertEqual(message, "nope")



class CredentialsRememberTheirDeployment(unittest.TestCase):
    """A key is only valid on the deployment that minted it, and an agent runs
    every CLI command in a fresh shell with none of the environment that logged
    in. So the login records its endpoints, and later calls follow them."""

    def test_saved_api_url_is_used_when_the_env_is_absent(self) -> None:
        os.environ["HUMALIKE_API_URL"] = "http://127.0.0.1:8010"
        save_api_key("ak_local")
        del os.environ["HUMALIKE_API_URL"]
        self.assertEqual(api_base_url(), "http://127.0.0.1:8010")
        self.assertEqual(load_api_key(), "ak_local")

    def test_env_still_wins_over_the_saved_value(self) -> None:
        os.environ["HUMALIKE_API_URL"] = "http://127.0.0.1:8010"
        save_api_key("ak_local")
        os.environ["HUMALIKE_API_URL"] = "https://api.humalike.com"
        self.assertEqual(api_base_url(), "https://api.humalike.com")

    def test_separate_keys_url_is_recorded_only_when_it_differs(self) -> None:
        os.environ["HUMALIKE_API_URL"] = "http://127.0.0.1:8010"
        os.environ["HUMALIKE_KEYS_URL"] = "http://127.0.0.1:8011"
        path = save_api_key("ak_local")
        del os.environ["HUMALIKE_API_URL"]
        del os.environ["HUMALIKE_KEYS_URL"]
        self.assertEqual(keys_base_url(), "http://127.0.0.1:8011")
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["api_url"], "http://127.0.0.1:8010")

    def test_no_saved_login_falls_back_to_the_hosted_api(self) -> None:
        self.assertEqual(api_base_url(), _hcommon.DEFAULT_API_URL)


class TestVerifyApiKey(IsolatedConfigTestCase):
    def test_200_means_the_key_works(self) -> None:
        transport = FakeTransport([(200, {"user_id": "user_1"})])
        self.assertTrue(verify_api_key(transport, "ak_good"))

    def test_401_means_the_key_is_dead(self) -> None:
        transport = FakeTransport([(401, {"error": {"message": "bad key"}})])
        self.assertFalse(verify_api_key(transport, "ak_dead"))

    def test_server_error_is_inconclusive_and_does_not_force_a_relogin(self) -> None:
        """A 500 says nothing about the key, so we must not send the user
        through a login they do not need."""
        transport = FakeTransport([(500, {"error": {"message": "boom"}})])
        self.assertTrue(verify_api_key(transport, "ak_unknown"))

    def test_network_failure_is_inconclusive(self) -> None:
        transport = FakeTransport([TransportError("offline")])
        self.assertTrue(verify_api_key(transport, "ak_unknown"))

    def test_probe_hits_whoami(self) -> None:
        transport = FakeTransport([(200, {})])
        verify_api_key(transport, "ak_good")
        self.assertTrue(
            transport.calls[0]["url"].endswith("/v1/turn-taking/actions/whoami")
        )


if __name__ == "__main__":
    unittest.main()
