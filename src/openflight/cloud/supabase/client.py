"""HTTP client for the owner's Supabase project (its PostgREST API).

Reuses the FlightWeb client's standard-library transport, so tests inject a
fake ``request_fn`` and nothing here needs the network. The contract is
``scripts/supabase/schema.sql``: rows are upserted with the publishable key in
``apikey`` and this Pi's token in ``x-openflight-device-token``; the database
fills ``device_id`` from the token, so it is never sent.
"""

import enum
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..client import DEFAULT_TIMEOUT, CloudNetworkError, HttpResponse, urllib_request
from .config import uses_bearer

SESSIONS_TABLE = "golf_sessions"
SHOTS_TABLE = "golf_shots"
DEVICE_FUNCTION = "golf_request_device"
TOKEN_HEADER = "x-openflight-device-token"
UPSERT_PREFER = "resolution=merge-duplicates,return=minimal"

# Postgres "insufficient_privilege": a row-level security check failed. The
# schema's policies fail when the token is unregistered or the row belongs to
# another device.
RLS_DENIED_CODE = "42501"
# PostgREST cannot find the function / table, or Postgres has no such relation
# or function: schema.sql has not been run in this project.
SCHEMA_MISSING_CODES = frozenset({"PGRST202", "PGRST205", "42P01", "42883"})

MAX_MESSAGE_CHARS = 300
# A connectivity probe gives up quickly: it only asks "is it back yet?".
PROBE_TIMEOUT_S = 5


class Outcome(enum.Enum):
    """What an upload run should do after one request."""

    OK = "ok"
    # Wrong key, unregistered token, or another device's rows: the owner must act.
    UNAUTHORIZED = "unauthorized"
    SCHEMA_MISSING = "schema_missing"
    # Offline, timeout, rate limit, 5xx, or an answer that is not from PostgREST.
    RETRY = "retry"
    # Any other 4xx: the project refused this data.
    REJECTED = "rejected"


@dataclass
class Reply:
    """One request's outcome; ``status`` is None when the server was not reached."""

    outcome: Outcome
    status: Optional[int]
    message: str = ""
    body: Any = None


def _parse_body(raw: bytes) -> Tuple[bool, Any]:
    """(True, parsed) for an empty or JSON body; (False, None) for anything else."""
    if not raw or not raw.strip():
        return True, None
    try:
        return True, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, None


def _error_message(status: int, body: Any) -> str:
    detail = body if isinstance(body, dict) else {}
    code = detail.get("code")
    text = detail.get("message") or detail.get("msg") or detail.get("error") or ""
    label = f"{status} {code}" if code else str(status)
    message = f"{label}: {text}" if text else label
    return message[:MAX_MESSAGE_CHARS]


def classify(resp: HttpResponse) -> Reply:
    """Map an HTTP response to the action an upload run should take."""
    is_json, body = _parse_body(resp.body)
    if 200 <= resp.status < 300:
        if is_json:
            return Reply(Outcome.OK, resp.status, body=body)
        # A captive portal or proxy page, not PostgREST: nothing was stored.
        return Reply(Outcome.RETRY, resp.status, f"{resp.status}: the answer was not from Supabase")

    code = str(body.get("code") or "") if isinstance(body, dict) else ""
    message = _error_message(resp.status, body)
    if resp.status in (401, 403) or code == RLS_DENIED_CODE:
        return Reply(Outcome.UNAUTHORIZED, resp.status, message)
    if resp.status == 404 or code in SCHEMA_MISSING_CODES:
        return Reply(Outcome.SCHEMA_MISSING, resp.status, message)
    if resp.status in (408, 429) or resp.status >= 500:
        return Reply(Outcome.RETRY, resp.status, message)
    return Reply(Outcome.REJECTED, resp.status, message)


class SupabaseClient:
    """Thin client over the two upserts and the device check schema.sql allows."""

    def __init__(
        self,
        url: str,
        key: str,
        device_token: str,
        request_fn: Callable[..., HttpResponse] = urllib_request,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.url = url.rstrip("/")
        self._key = key
        self._device_token = device_token
        self._request = request_fn
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        headers = {
            "apikey": self._key,
            TOKEN_HEADER: self._device_token,
            "Content-Type": "application/json",
        }
        if uses_bearer(self._key):
            headers["Authorization"] = f"Bearer {self._key}"
        return headers

    def _send(self, path: str, body: bytes, headers: Dict[str, str]) -> Reply:
        try:
            resp = self._request(
                "POST",
                f"{self.url}/rest/v1/{path}",
                data=body,
                headers=headers,
                timeout=self.timeout,
            )
        except CloudNetworkError as exc:
            return Reply(Outcome.RETRY, None, f"network error: {exc}"[:MAX_MESSAGE_CHARS])
        return classify(resp)

    def upsert(self, table: str, rows: List[Dict[str, Any]]) -> Reply:
        """Insert or update ``rows`` (all with the same keys) by ``id``."""
        headers = self._headers()
        headers["Prefer"] = UPSERT_PREFER
        body = json.dumps(rows, allow_nan=False, separators=(",", ":")).encode("utf-8")
        return self._send(f"{table}?on_conflict=id", body, headers)

    def reachable(self) -> bool:
        """True if the project answers at all: one GET to ``/rest/v1/``.

        Any answer below 500 counts, including 401 or 404 (the push that
        follows tells those apart); no answer or a 5xx does not.
        """
        try:
            resp = self._request(
                "GET",
                f"{self.url}/rest/v1/",
                data=None,
                headers=self._headers(),
                timeout=PROBE_TIMEOUT_S,
            )
        except CloudNetworkError:
            return False
        return resp.status < 500

    def registered_device(self) -> Reply:
        """Ask which ``golf_devices`` row this Pi's token matches. Writes nothing.

        On success ``body`` is the device's uuid, or None when the token is not
        registered.
        """
        headers = self._headers()
        headers["Accept"] = "application/json"
        reply = self._send(f"rpc/{DEVICE_FUNCTION}", b"{}", headers)
        if (
            reply.outcome is Outcome.OK
            and reply.body is not None
            and not isinstance(reply.body, str)
        ):
            return Reply(Outcome.RETRY, reply.status, "unexpected answer to the device check")
        return reply
