"""Shared plumbing for the Humalike audit CLI.

WHY THIS MODULE EXISTS
----------------------
Both entry points (``humalike_login.py`` and ``humalike_audit.py``) need the
same four things: resolve the base URLs, talk HTTP/JSON to the Humalike API,
read/write the credentials file, and turn server errors into messages a human
can act on. Keeping that in one place means the two scripts cannot drift apart
on, say, what a timeout is or where the key lives.

TWO HARD CONSTRAINTS SHAPE THE CODE
-----------------------------------
1. **Standard library only.** The whole point of this tool is that an agent can
   run it in a fresh shell with no ``pip install`` step. Anything that would
   need a wheel is off the table, so we use ``urllib.request`` directly instead
   of ``requests``. This is why the HTTP code below is more verbose than you
   would normally write.

2. **The HTTP layer is injectable.** ``Transport`` is a tiny protocol with one
   method. The real implementation uses urllib; the tests pass a fake. Nothing
   in this repo's test suite touches the network, which keeps the tests fast
   and hermetic.

A NOTE ON SECRETS
-----------------
API keys are never logged, never echoed, and never included in an exception
message. ``redact()`` exists so that diagnostics can mention a key without
leaking it.
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

#: Where the hosted API lives. Overridable so that the same client can drive a
#: local stack during development.
DEFAULT_API_URL = "https://api.humalike.com"

#: Every network call is bounded. A hung socket must never turn into a hung
#: agent session, so there is no code path here without a timeout.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Transient failures (a 502 from a cold service, a dropped connection) are
#: retried a few times with a linear backoff. Anything the server answered
#: deliberately -- a 4xx -- is NOT retried, because retrying will not change it.
DEFAULT_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5

#: Mode 0600: owner read/write only. The credentials file holds a live API key,
#: so a group- or world-readable file would be a real leak on a shared box.
CREDENTIALS_MODE = 0o600


def api_base_url() -> str:
    """Base URL for the audit endpoints (``HUMALIKE_API_URL``)."""
    return os.environ.get("HUMALIKE_API_URL", DEFAULT_API_URL).rstrip("/")


def keys_base_url() -> str:
    """Base URL for the device-auth endpoints.

    In production these live behind the same gateway as everything else, so
    this defaults to :func:`api_base_url`. The override exists purely for local
    development, where the services run as separate containers on separate
    ports (svc-social-observability on 8010, svc-keys on 8011) with no gateway
    in front of them. Treat ``HUMALIKE_KEYS_URL`` as a dev-only knob.
    """
    return os.environ.get("HUMALIKE_KEYS_URL", api_base_url()).rstrip("/")


def cli_gateway_key() -> str | None:
    """Shared key that fronts the anonymous device-auth lane, if one is needed.

    In production the API gateway injects this before the request reaches
    svc-keys, so the CLI sends nothing and the lane is anonymous from the
    client's point of view. A local stack has no gateway in front of it, so a
    developer pointing this CLI at ``127.0.0.1`` must supply the key themselves.

    Dev-only knob. Leave it unset against the hosted API.
    """
    value = os.environ.get("HUMALIKE_CLI_GATEWAY_KEY", "").strip()
    return value or None


def credentials_path() -> Path:
    """Location of the saved key. ``HUMALIKE_CONFIG_DIR`` redirects it, which is
    what the tests use to avoid writing into a real home directory."""
    root = os.environ.get("HUMALIKE_CONFIG_DIR")
    base = Path(root) if root else Path.home() / ".humalike"
    return base / "credentials"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class HumalikeError(Exception):
    """Base class for every failure this CLI reports to a human.

    Anything raised as a ``HumalikeError`` is expected: it gets printed as a
    clean one-line message and a non-zero exit, never as a traceback.
    """


class ApiError(HumalikeError):
    """The server answered, and the answer was a refusal.

    ``message`` is the server's own wording. We deliberately relay it verbatim
    rather than paraphrasing: the server knows things the client does not (the
    exact message count against the cap, which participant names are valid,
    whether the account is out of credits) and rewording that only loses
    information.
    """

    def __init__(self, status: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code

    @property
    def is_out_of_credits(self) -> bool:
        """402 is the API's 'this account cannot pay for that' answer."""
        return self.status == 402

    @property
    def is_auth_failure(self) -> bool:
        """401/403 mean the key is missing, revoked, or expired -- i.e. the
        caller should re-run the login flow rather than retry."""
        return self.status in (401, 403)


class TransportError(HumalikeError):
    """The request never produced an HTTP answer (DNS, TCP, TLS, timeout)."""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Transport(Protocol):
    """The single seam between this CLI and the network.

    Implementations return the HTTP status and the decoded JSON body. They do
    NOT raise on 4xx/5xx -- interpreting status codes is the caller's job, and
    keeping that decision out of the transport is what makes the fake used in
    tests a two-line class.
    """

    def post(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        ...


class UrllibTransport:
    """The real transport, built on ``urllib.request`` so we stay dependency-free."""

    def post(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> tuple[int, dict[str, Any]]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        for name, value in headers.items():
            request.add_header(name, value)

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as exc:
            # An HTTPError still carries a body, and for this API that body is
            # the error envelope we actually want to show the user.
            return exc.code, _decode(exc.read())
        except urllib.error.URLError as exc:
            raise TransportError(f"could not reach {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TransportError(f"timed out after {timeout:.0f}s calling {url}") from exc


def _decode(raw: bytes) -> dict[str, Any]:
    """Parse a JSON object body, tolerating an empty or non-JSON response.

    A gateway returning an HTML error page must not crash the client with a
    JSONDecodeError -- the status code is still meaningful on its own.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def extract_error_message(status: int, body: dict[str, Any]) -> tuple[str, str | None]:
    """Pull the server's own wording out of an error envelope.

    The API shape is ``{"error": {"code": ..., "message": ..., "details": [...]}}``.
    Field-level details are appended when present because they are usually the
    actionable part ("over the 250-message cap" tells you more than "validation
    failed"). Falls back to a bare status line if the body is not an envelope.
    """
    error = body.get("error")
    if not isinstance(error, dict):
        return f"HTTP {status}", None

    message = str(error.get("message") or f"HTTP {status}")
    code = error.get("code")

    details = error.get("details")
    if isinstance(details, list):
        rendered = [
            f"{d.get('field')}: {d.get('message')}" if d.get("field") else str(d.get("message"))
            for d in details
            if isinstance(d, dict) and d.get("message")
        ]
        if rendered:
            message = f"{message} ({'; '.join(rendered)})"

    return message, (str(code) if code else None)


def post_json(
    transport: Transport,
    url: str,
    payload: dict[str, Any],
    *,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    sleep: Any = None,
) -> dict[str, Any]:
    """POST JSON and return the decoded body, or raise.

    Retries cover transport failures and 5xx responses only. A 4xx is the
    server making a decision, so it is surfaced immediately -- retrying a 400
    just makes the user wait longer for the same answer.

    ``sleep`` is injected so tests can run the retry logic without real delays.
    """
    if sleep is None:
        import time

        sleep = time.sleep

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_error: HumalikeError | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            status, body = transport.post(url, payload, headers, timeout)
        except TransportError as exc:
            last_error = exc
        else:
            if 200 <= status < 300:
                return body

            message, code = extract_error_message(status, body)
            error = ApiError(status, message, code)
            # Deliberate refusal: do not retry.
            if status < 500:
                raise error
            last_error = error

        if attempt < retries:
            sleep(RETRY_BACKOFF_SECONDS * attempt)

    assert last_error is not None  # a loop of >=1 attempt always sets this
    raise last_error


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def load_api_key() -> str | None:
    """Return the API key to use, or ``None`` if there is not one yet.

    The environment variable wins over the saved file on purpose: it is the
    escape hatch for CI, for containers, and for anyone driving a second
    account without disturbing their saved login.
    """
    from_env = os.environ.get("HUMALIKE_API_KEY", "").strip()
    if from_env:
        return from_env

    path = credentials_path()
    if not path.exists():
        return None

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        data = json.loads(raw)
    except ValueError:
        # Tolerate a plain-text key file: someone may reasonably have written
        # the key in by hand, and failing on that would be gratuitous.
        stripped = raw.strip()
        return stripped or None

    key = data.get("api_key") if isinstance(data, dict) else None
    return str(key) if key else None


def save_api_key(api_key: str, *, account: dict[str, Any] | None = None) -> Path:
    """Persist the key at mode 0600 and return where it landed.

    The directory is created 0700 and the file's mode is set explicitly rather
    than relying on the process umask, which is not guaranteed to be restrictive.
    """
    path = credentials_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    document: dict[str, Any] = {"api_key": api_key}
    if account:
        document["account"] = account

    # Create with the right mode from the start, so the key is never briefly
    # readable by anyone else between write and chmod.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CREDENTIALS_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")

    os.chmod(path, CREDENTIALS_MODE)  # in case the file already existed
    return path


def credentials_mode() -> int | None:
    """Permission bits of the credentials file, for ``--status`` to report."""
    path = credentials_path()
    if not path.exists():
        return None
    return stat.S_IMODE(path.stat().st_mode)


def redact(api_key: str | None) -> str:
    """Render a key safely for display: enough to recognise, not enough to use."""
    if not api_key:
        return "<none>"
    if len(api_key) <= 10:
        return "*" * len(api_key)
    return f"{api_key[:6]}...{api_key[-4:]}"


# --------------------------------------------------------------------------
# Key validation
# --------------------------------------------------------------------------


def verify_api_key(
    transport: Transport,
    api_key: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Return True if the key is currently accepted by the API.

    Uses ``whoami`` because it is the cheapest authenticated call in the
    product: no LLM, no credits, no side effects. A 401/403 is a definite "this
    key is dead"; anything else (a 500, a network blip) is inconclusive, and we
    report the key as usable rather than pushing the user through a login they
    may not need.
    """
    url = f"{api_base_url()}/v1/turn-taking/actions/whoami"
    try:
        post_json(transport, url, {}, api_key=api_key, timeout=timeout, retries=1)
    except ApiError as exc:
        if exc.is_auth_failure:
            return False
        return True
    except TransportError:
        return True
    return True
