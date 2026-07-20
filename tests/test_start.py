"""Tests for the `start` script — the one thing users actually run.

`start`'s stdout is the product: it is the only context the agent gets. So the
things worth pinning are the things an agent would silently do wrong if they
regressed — asking the human which speaker is the agent, the message cap, the
absolute paths, and never leaking a key.

No network: every case here either avoids the API entirely (`--help`) or points
the CLI at an unroutable address so the login probe fails fast and takes the
not-signed-in branch.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START = REPO_ROOT / "start"


def run_start(*args: str, home: str, extra_env: dict[str, str] | None = None):
    """Run `start` with a scratch HOME so no real credentials are touched."""
    env = dict(os.environ)
    env.update(
        {
            "HOME": home,
            "HUMALIKE_CONFIG_DIR": str(Path(home) / ".humalike"),
            # Unroutable on purpose: no test may depend on a reachable API.
            "HUMALIKE_API_URL": "http://127.0.0.1:9",
            "HUMALIKE_API_KEY": "",
        }
    )
    env.update(extra_env or {})
    return subprocess.run(
        ["/bin/bash", str(START), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


#: A stand-in for ``python3`` that answers the two questions ``start`` asks it:
#: the version probe (``-c``) and the state of the login. Lets the playbook and
#: the ready/not-ready branches be tested without a reachable API, and keeps the
#: assertions on ``start``'s own logic rather than on the login script's.
STUB_PYTHON = """#!/bin/bash
# Match on $1, not on "$*": the checkout path itself contains "-c".
if [ "$1" = "-c" ]; then exit 0; fi
case "$*" in
  *--status*) exit {status_exit} ;;
  *--begin*)
    echo
    echo "  Sign in to Humalike to continue."
    echo
    echo "    Open:      https://humalike.ai/cli-auth?code=TEST-CODE"
    echo "    Your code: TEST-CODE"
    echo
    exit 0 ;;
esac
exit 0
"""


def stub_python_dir(tmpdir: str, *, logged_in: bool) -> str:
    """Write the stub and return a PATH entry that shadows the real python3."""
    d = Path(tmpdir) / "stubbin"
    d.mkdir(parents=True, exist_ok=True)
    stub = d / "python3"
    stub.write_text(STUB_PYTHON.format(status_exit=0 if logged_in else 1), encoding="utf-8")
    stub.chmod(0o755)
    return f"{d}:{os.environ.get('PATH', '')}"


class StartScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_start_is_executable(self) -> None:
        self.assertTrue(os.access(START, os.X_OK), "start must be chmod +x")

    def test_help_exits_zero_and_lists_the_modes(self) -> None:
        result = run_start("--help", home=self.home)
        self.assertEqual(result.returncode, 0)
        for flag in ("--status", "--wait-login", "--help"):
            self.assertIn(flag, result.stdout)

    def test_unknown_option_is_rejected(self) -> None:
        result = run_start("--nonsense", home=self.home)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown option", result.stderr)

    def test_status_reports_not_ready_without_a_key(self) -> None:
        result = run_start("--status", home=self.home)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not signed in", result.stdout.lower())

    def _ready_output(self) -> str:
        path = stub_python_dir(self.home, logged_in=True)
        result = run_start(home=self.home, extra_env={"PATH": path})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_playbook_carries_the_rules_an_agent_must_not_lose(self) -> None:
        """The failure mode this guards is a report about the wrong party."""
        out = self._ready_output()

        self.assertIn("ASK WHICH SPEAKER IS THE AGENT", out)
        self.assertIn("Never guess silently", out)
        self.assertIn("250", out)
        self.assertIn("never trim silently", out)
        self.assertIn("Never invent", out)

    def test_ready_run_says_so_and_does_not_re_login(self) -> None:
        """Idempotence: the second paste must not restart the device flow."""
        out = self._ready_output()
        self.assertIn("Signed in and ready", out)
        self.assertNotIn("ACTION REQUIRED", out)

    def test_not_signed_in_run_leads_with_the_link_then_the_playbook(self) -> None:
        path = stub_python_dir(self.home, logged_in=False)
        result = run_start(home=self.home, extra_env={"PATH": path})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ACTION REQUIRED", result.stdout)
        self.assertIn("https://humalike.ai/cli-auth?code=TEST-CODE", result.stdout)
        # The link must come before the playbook: it is what unblocks everything.
        self.assertLess(
            result.stdout.index("ACTION REQUIRED"),
            result.stdout.index("HUMALIKE SOCIAL AUDIT"),
        )
        self.assertIn("--wait-login", result.stdout)

    def test_playbook_uses_absolute_paths(self) -> None:
        """Agents run commands from arbitrary cwds; relative paths would break."""
        out = self._ready_output()
        checked = 0
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("python3 ") and "humalike_audit.py" in stripped:
                path = stripped.split()[1]
                self.assertTrue(
                    path.startswith("/") or path.startswith("~/"),
                    f"playbook path is not absolute: {path}",
                )
                checked += 1
        self.assertGreaterEqual(checked, 3, "expected the playbook's audit commands")

    def test_login_failure_is_one_actionable_message_not_a_traceback(self) -> None:
        result = run_start(home=self.home)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not start the Humalike sign-in", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_never_prints_the_api_key(self) -> None:
        """A key in an agent transcript is a leaked key."""
        secret = "hk_live_this_must_never_be_printed"
        creds = Path(self.home) / ".humalike"
        creds.mkdir(parents=True, exist_ok=True)
        (creds / "credentials").write_text(f'{{"api_key": "{secret}"}}', encoding="utf-8")

        result = run_start(home=self.home, extra_env={"HUMALIKE_API_KEY": secret})
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_missing_python_fails_with_one_plain_line(self) -> None:
        """PATH without python3 — the prerequisite check must say so, not crash."""
        result = subprocess.run(
            ["/bin/bash", str(START), "--status"],
            capture_output=True,
            text=True,
            env={"HOME": self.home, "PATH": "/nonexistent"},
            timeout=60,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("python3 is not installed", result.stderr)


if __name__ == "__main__":
    sys.exit(unittest.main())
