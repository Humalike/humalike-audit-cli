"""Test helpers: a fake transport and an isolated config directory.

The point of this module is that no test in this suite ever opens a socket or
writes to the real ``~/.humalike``. Both are achieved by injection rather than
monkeypatching internals, which keeps the tests honest about the seams the
production code actually exposes.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bin"))


class FakeTransport:
    """A scripted stand-in for :class:`UrllibTransport`.

    Queue up ``(status, body)`` pairs, or an exception instance to raise. Every
    call is recorded so tests can assert on what was sent -- including that the
    Authorization header was present and correctly formed.
    """

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float = 30.0,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append(
            {"url": url, "payload": payload, "headers": headers, "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError(f"FakeTransport ran out of responses at {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class IsolatedConfigTestCase(unittest.TestCase):
    """Base case that points the credential helpers at a temp directory."""

    def setUp(self) -> None:
        super().setUp()
        self._tempdir = tempfile.TemporaryDirectory()
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "HUMALIKE_CONFIG_DIR",
                "HUMALIKE_API_KEY",
                "HUMALIKE_API_URL",
                "HUMALIKE_KEYS_URL",
                "HUMALIKE_APP_URL",
                "HUMALIKE_CLI_GATEWAY_KEY",
            )
        }
        os.environ["HUMALIKE_CONFIG_DIR"] = self._tempdir.name
        os.environ.pop("HUMALIKE_API_KEY", None)
        os.environ.pop("HUMALIKE_CLI_GATEWAY_KEY", None)

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tempdir.cleanup()
        super().tearDown()
