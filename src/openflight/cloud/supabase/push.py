"""Incremental, idempotent upload of session shots to Supabase.

The session directory stays the queue (see ``spool``). Supabase keeps its own
files, so it never touches FlightWeb's markers:

- ``<session>.jsonl.supabase``: the byte offset of the lines already handled,
  plus the session id and its start/end entries (so the session row can be
  rebuilt without re-reading the file). A run reads only the complete lines
  appended since, never the raw I/Q before them; a line still being written
  waits for the next run. The offset moves only after Supabase accepted
  everything before it, so a failed run resends, and the upserts dedupe.
- ``<log_dir>/.supabase-state.json``: the last run for ``status``, and a pause
  after the project refused this Pi (wrong key, unregistered token, or no
  schema), so background runs stop hammering it until the owner acts.
- ``<log_dir>/.supabase-push.lock``: held by the run that is uploading, so two
  runs never upload the same lines at once.
- ``<log_dir>/.supabase-push.again``: every run leaves this before taking the
  lock. One that finds the lock taken exits, and the holder sees the file after
  its pass and makes another, so a shot logged mid-run still goes up promptly.

Only allowlisted entries are read (``filtering.screen_entry``, the same rule as
FlightWeb's filtered upload), even when FlightWeb raw uploads are on.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from .. import filtering, spool
from . import rows
from .client import SESSIONS_TABLE, SHOTS_TABLE, Outcome, Reply
from .config import SupabaseConfig, token_sha256

SIDECAR_SUFFIX = ".supabase"
STATE_NAME = ".supabase-state.json"
LOCK_NAME = ".supabase-push.lock"
AGAIN_NAME = ".supabase-push.again"

# Shot rows per request: one shot's data is a few KB.
BATCH_SIZE = 50
# Background runs leave the project alone this long after it refused this Pi.
PAUSE_S = 30 * 60
# Bound on back-to-back passes requested while one run holds the lock.
MAX_PASSES = 5

# Why a run stopped early.
STOP_INACTIVE = "inactive"
STOP_BUSY = "busy"
STOP_PAUSED = "paused"
STOP_RETRY = Outcome.RETRY.value
STOP_UNAUTHORIZED = Outcome.UNAUTHORIZED.value
STOP_SCHEMA_MISSING = Outcome.SCHEMA_MISSING.value
NEEDS_OWNER = frozenset({STOP_PAUSED, STOP_UNAUTHORIZED, STOP_SCHEMA_MISSING})

# What a run learned about reaching the project, recorded as ``link`` in the
# state file for the server (``link.SupabaseLink``) and ``status``.
LINK_ONLINE = "online"
LINK_OFFLINE = "offline"
LINK_NEEDS_ATTENTION = "needs_attention"

# Bytes every shot_detected line contains (its "type" value). Counting pending
# shots skips lines without them unparsed; the allowlist still decides.
_SHOT_MARKER = b"shot_detected"

OutFn = Callable[[str], None]


def sidecar_path(session_path: Path) -> Path:
    """``session_x.jsonl`` -> ``session_x.jsonl.supabase``."""
    return session_path.with_name(session_path.name + SIDECAR_SUFFIX)


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    # Replace in one step: a run killed mid-write must not leave a torn file.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def format_time(epoch: float) -> str:
    """A Unix time as local ISO 8601 with its UTC offset, to the second."""
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _entry(value: Any) -> Optional[Dict[str, Any]]:
    return value if isinstance(value, dict) and value else None


@dataclass
class SessionProgress:
    """What one session file has already sent to Supabase."""

    offset: int = 0
    session_id: str = ""
    session_start: Optional[Dict[str, Any]] = None
    session_end: Optional[Dict[str, Any]] = None
    # The session row built from the two entries above is in Supabase.
    session_sent: bool = False
    shots_sent: int = 0
    # Set when the project refused this session's data (a 4xx other than auth).
    rejected: Optional[Dict[str, Any]] = None

    @classmethod
    def load(cls, path: Path) -> "SessionProgress":
        """The sidecar at ``path``; a fresh start if it is absent or unreadable."""
        data = _read_json(path)
        session_id = data.get("session_id")
        if not data or not isinstance(session_id, str):
            return cls()
        return cls(
            offset=_count(data.get("offset")),
            session_id=session_id,
            session_start=_entry(data.get("session_start")),
            session_end=_entry(data.get("session_end")),
            session_sent=data.get("session_sent") is True,
            shots_sent=_count(data.get("shots_sent")),
            rejected=_entry(data.get("rejected")),
        )

    def save(self, path: Path) -> None:
        """Write the sidecar."""
        _write_json_atomic(path, asdict(self))


@dataclass
class PushSummary:
    """Outcome of one ``push`` call."""

    dry_run: bool = False
    sessions_updated: int = 0
    shots_sent: int = 0
    requests_ok: int = 0
    oversize_shots: int = 0
    rejected: List[str] = field(default_factory=list)
    stopped: Optional[str] = None
    message: str = ""

    @property
    def needs_owner(self) -> bool:
        """True when uploads cannot resume until the owner fixes something."""
        return self.stopped in NEEDS_OWNER


class _SessionUpload:
    """Read one session's new lines and send them in batches."""

    def __init__(self, path: Path, progress: SessionProgress, client, dry_run: bool):
        self.path = path
        self.progress = progress
        self.client = client
        self.dry_run = dry_run
        # Keyed by row id: a shot repeated within one batch is sent once.
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.shots_sent = 0
        self.session_rows_sent = 0
        self.requests_ok = 0
        self.oversize_shots = 0

    def feed(self, raw: bytes, line_start: int) -> None:
        """Take one complete line that starts at byte ``line_start``."""
        text = raw.decode("utf-8", errors="replace")
        entry = filtering.parse_line(text)
        if entry is None:
            return
        progress = self.progress
        entry_type = entry.get("type")
        if not progress.session_id:
            # The session logger writes session_start first.
            first_uuid = entry.get("session_uuid") if entry_type == "session_start" else None
            progress.session_id = rows.session_id(first_uuid, self.path.name)

        verdict = filtering.screen_entry(text, entry)
        if verdict is filtering.LineVerdict.OVERSIZE and entry_type == "shot_detected":
            self.oversize_shots += 1
        if verdict is not filtering.LineVerdict.KEEP:
            return
        if entry_type == "session_start" and progress.session_start is None:
            progress.session_start = entry
            progress.session_sent = False
        elif entry_type == "session_end":
            progress.session_end = entry
            progress.session_sent = False
        elif entry_type == "shot_detected":
            row = rows.shot_row(entry, progress.session_id, line_start)
            self.pending[row["id"]] = row

    def _send(self, table: str, batch: List[Dict[str, Any]]) -> Optional[Reply]:
        """None on success, else the failing reply."""
        if self.dry_run:
            return None
        reply = self.client.upsert(table, batch)
        if reply.outcome is not Outcome.OK:
            return reply
        self.requests_ok += 1
        return None

    def flush(self, position: int, sidecar: Path) -> Optional[Reply]:
        """Send the session row if it changed, then pending shots; then save ``position``."""
        progress = self.progress
        if progress.session_id and not progress.session_sent:
            row = rows.session_row(
                progress.session_id, self.path.name, progress.session_start, progress.session_end
            )
            reply = self._send(SESSIONS_TABLE, [row])
            if reply is not None:
                return reply
            progress.session_sent = True
            self.session_rows_sent += 1
        if self.pending:
            batch = list(self.pending.values())
            reply = self._send(SHOTS_TABLE, batch)
            if reply is not None:
                return reply
            self.shots_sent += len(batch)
            progress.shots_sent += len(batch)
            self.pending.clear()
        if position != progress.offset or self.requests_ok:
            progress.offset = position
            if not self.dry_run:
                progress.save(sidecar)
        return None


def _read_and_send(upload: _SessionUpload, sidecar: Path) -> Optional[Reply]:
    """Feed every complete line after the saved offset, flushing each full batch."""
    position = upload.progress.offset
    with upload.path.open("rb") as handle:
        handle.seek(position)
        for raw in handle:
            if not raw.endswith(b"\n"):
                break  # still being written; the next run reads it whole
            upload.feed(raw, position)
            position += len(raw)
            if len(upload.pending) >= BATCH_SIZE:
                reply = upload.flush(position, sidecar)
                if reply is not None:
                    return reply
    return upload.flush(position, sidecar)


def _report_session(path: Path, upload: _SessionUpload, summary: PushSummary, out: OutFn) -> None:
    summary.shots_sent += upload.shots_sent
    summary.requests_ok += upload.requests_ok
    summary.oversize_shots += upload.oversize_shots
    if upload.shots_sent or upload.session_rows_sent:
        summary.sessions_updated += 1
        if summary.dry_run:
            out(
                f"Supabase (dry run): {path.name}: would send "
                f"{'the session row and ' if upload.session_rows_sent else ''}"
                f"{upload.shots_sent} shot(s)."
            )
        elif upload.shots_sent:
            out(f"Supabase: {path.name}: uploaded {upload.shots_sent} shot(s).")
        else:
            out(f"Supabase: {path.name}: session details updated.")
    if upload.oversize_shots:
        out(
            f"Supabase: {path.name}: {upload.oversize_shots} shot line(s) over "
            f"{filtering.MAX_LINE_BYTES // 1024} KB not sent."
        )


def _sync_session(path: Path, client, summary: PushSummary, out: OutFn) -> Optional[Reply]:
    """Upload what one session appended since the last run.

    Returns the reply that should stop the whole run, or None to continue.
    """
    sidecar = sidecar_path(path)
    progress = SessionProgress.load(sidecar)
    if progress.rejected:
        return None
    size = path.stat().st_size
    if size < progress.offset:
        # The file was replaced or truncated: start over; upserts dedupe.
        progress = SessionProgress()
    if size == progress.offset and (progress.session_sent or not progress.session_id):
        return None

    upload = _SessionUpload(path, progress, client, summary.dry_run)
    reply = _read_and_send(upload, sidecar)
    _report_session(path, upload, summary, out)
    if reply is None or reply.outcome is not Outcome.REJECTED:
        return reply

    summary.rejected.append(path.name)
    out(
        f"Supabase: {path.name}: the project refused this session ({reply.message}); "
        "skipping it. `openflight-cloud supabase push --resend` tries again."
    )
    saved = SessionProgress.load(sidecar)
    saved.rejected = {
        "status": reply.status,
        "message": reply.message,
        "at": format_time(time.time()),
    }
    saved.save(sidecar)
    return None


def _one_pass(log_dir: Path, client, summary: PushSummary, out: OutFn) -> None:
    """Sync every session, oldest first, so shots go up in the order they were hit."""
    for path in spool.session_files(log_dir):
        try:
            reply = _sync_session(path, client, summary, out)
        except OSError as exc:
            out(f"Supabase: {path.name}: could not read it ({exc}); skipped.")
            continue
        if reply is not None:
            summary.stopped = reply.outcome.value
            summary.message = reply.message
            return


def config_fingerprint(config: SupabaseConfig) -> str:
    """Identifies the URL, key and token a pause was recorded for (no secrets)."""
    material = "\n".join(
        [config.supabase_url, config.supabase_key, token_sha256(config.supabase_device_token)]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def load_state(log_dir: Path) -> Dict[str, Any]:
    """The device-level state file ({} if absent or unreadable)."""
    return _read_json(Path(log_dir) / STATE_NAME)


def active_pause(log_dir: Path, config: SupabaseConfig, now: float) -> Optional[Dict[str, Any]]:
    """The pause in force for this config, if any.

    A pause recorded for a different URL, key or token does not apply: changing
    the config is how the owner fixes what caused it.
    """
    pause = load_state(log_dir).get("paused")
    if not isinstance(pause, dict) or pause.get("config") != config_fingerprint(config):
        return None
    until = pause.get("until")
    if isinstance(until, bool) or not isinstance(until, (int, float)) or now >= until:
        return None
    return pause


def clear_pause(log_dir: Path) -> None:
    """Forget a pause, e.g. after ``supabase test`` found the Pi registered."""
    log_dir = Path(log_dir)
    state = load_state(log_dir)
    if state.pop("paused", None) is not None and log_dir.is_dir():
        _write_json_atomic(log_dir / STATE_NAME, state)


def _link_state(summary: PushSummary) -> Optional[str]:
    """Whether the run reached the project; None if it made no request."""
    if summary.stopped in (STOP_UNAUTHORIZED, STOP_SCHEMA_MISSING):
        return LINK_NEEDS_ATTENTION
    if summary.stopped == STOP_RETRY:
        # No answer (DNS, connect, timeout) or a 5xx: either way, nothing
        # can be uploaded until it is back.
        return LINK_OFFLINE
    if summary.requests_ok:
        return LINK_ONLINE
    return None


def _record_run(log_dir: Path, config: SupabaseConfig, summary: PushSummary, now: float) -> None:
    state = load_state(log_dir)
    state["last_run_at"] = format_time(now)
    link = _link_state(summary)
    if link is not None:
        state["link"] = {
            "state": link,
            "message": summary.message,
            "reason": summary.stopped if link == LINK_NEEDS_ATTENTION else "",
            "at": now,
        }
    if summary.stopped in (STOP_UNAUTHORIZED, STOP_SCHEMA_MISSING):
        state["paused"] = {
            "reason": summary.stopped,
            "message": summary.message,
            "at": format_time(now),
            "until": now + PAUSE_S,
            "config": config_fingerprint(config),
        }
        state["last_error"] = summary.message
    elif summary.stopped == STOP_RETRY:
        state["last_error"] = summary.message
    elif summary.requests_ok:
        state.pop("paused", None)
        state.pop("last_error", None)
    if summary.shots_sent or summary.sessions_updated:
        state["last_upload_at"] = format_time(now)
        state["last_upload_shots"] = summary.shots_sent
    _write_json_atomic(log_dir / STATE_NAME, state)


@contextlib.contextmanager
def _exclusive(lock_path: Path) -> Iterator[bool]:
    """Yield True while holding the push lock, or False if another run holds it."""
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            locked = False
        yield locked
    finally:
        os.close(fd)  # closing releases the lock


def _take_request(again: Path) -> bool:
    """Consume the pending pass request; False if there was none."""
    try:
        again.unlink()
    except FileNotFoundError:
        return False
    return True


def clear_progress(log_dir: Path) -> int:
    """Delete every session sidecar so the next run sends everything again."""
    cleared = 0
    for path in spool.session_files(log_dir):
        sidecar = sidecar_path(path)
        if sidecar.exists():
            sidecar.unlink()
            cleared += 1
    return cleared


def _final_report(summary: PushSummary, url: str, out: OutFn) -> None:
    if summary.stopped == STOP_RETRY:
        out(f"Supabase: could not upload to {url} ({summary.message}); will retry on the next run.")
    elif summary.stopped == STOP_UNAUTHORIZED:
        out(
            f"Supabase: the project refused this Pi ({summary.message}). Either the device "
            "is not registered yet (run the line `openflight-cloud supabase setup` printed "
            "in the SQL editor) or the key is wrong. Background uploads pause for "
            f"{PAUSE_S // 60} minutes; run `openflight-cloud supabase test` once it is fixed."
        )
    elif summary.stopped == STOP_SCHEMA_MISSING:
        out(
            f"Supabase: the OpenFlight tables were not found ({summary.message}). Run "
            "scripts/supabase/schema.sql in the project's SQL editor, then "
            "`openflight-cloud supabase test`."
        )
    elif not (summary.sessions_updated or summary.rejected):
        out("Supabase: nothing new to upload.")


def push(
    config: SupabaseConfig,
    log_dir: Path,
    client,
    *,
    dry_run: bool = False,
    resend: bool = False,
    respect_pause: bool = False,
    out: OutFn = print,
    now_fn: Callable[[], float] = time.time,
) -> PushSummary:
    """Upload every session's new shots. Never raises for HTTP or network errors.

    ``respect_pause`` is for background runs (the timer, session end, after
    each shot): they skip while a pause is in force. A run the owner starts by
    hand always tries. ``resend`` forgets what was sent and sends it all again.
    """
    summary = PushSummary(dry_run=dry_run)
    if not dry_run and not config.is_active():
        out("Supabase: uploads are off or not set up (`openflight-cloud supabase status`).")
        summary.stopped = STOP_INACTIVE
        return summary
    log_dir = Path(log_dir)
    if not log_dir.is_dir():
        out("Supabase: nothing to upload (no session logs yet).")
        return summary
    if dry_run:
        _one_pass(log_dir, client, summary, out)
        _final_report(summary, config.supabase_url, out)
        return summary

    pause = active_pause(log_dir, config, now_fn()) if respect_pause else None
    if pause is not None:
        out(
            f"Supabase: paused since {pause.get('at')} ({pause.get('message')}); next try after "
            f"{format_time(pause['until'])}. Run `openflight-cloud supabase test` once it is fixed."
        )
        summary.stopped = STOP_PAUSED
        return summary

    again = log_dir / AGAIN_NAME
    again.touch()
    passes = 0
    for _ in range(MAX_PASSES):
        with _exclusive(log_dir / LOCK_NAME) as locked:
            if not locked:
                if passes == 0:
                    summary.stopped = STOP_BUSY
                break
            if not _take_request(again):
                break
            if resend and passes == 0:
                out(f"Supabase: sending everything again ({clear_progress(log_dir)} session(s)).")
            _one_pass(log_dir, client, summary, out)
            passes += 1
            _record_run(log_dir, config, summary, now_fn())
        if summary.stopped:
            break
    if summary.stopped == STOP_BUSY:
        out("Supabase: another upload is running; it will pick up new shots.")
    else:
        _final_report(summary, config.supabase_url, out)
    return summary


def session_overview(log_dir: Path) -> Dict[str, int]:
    """Per-session upload state for ``status``."""
    counts = {"total": 0, "up_to_date": 0, "waiting": 0, "rejected": 0, "shots_sent": 0}
    for path in spool.session_files(log_dir):
        progress = SessionProgress.load(sidecar_path(path))
        counts["total"] += 1
        counts["shots_sent"] += progress.shots_sent
        if progress.rejected:
            counts["rejected"] += 1
        elif progress.offset == path.stat().st_size:
            counts["up_to_date"] += 1
        else:
            counts["waiting"] += 1
    return counts


def _is_uploadable_shot(raw: bytes) -> bool:
    text = raw.decode("utf-8", errors="replace")
    entry = filtering.parse_line(text)
    return (
        entry is not None
        and entry.get("type") == "shot_detected"
        and filtering.screen_entry(text, entry) is filtering.LineVerdict.KEEP
    )


def pending_shot_count(log_dir: Path) -> int:
    """Allowlisted shots on disk that no run has uploaded yet.

    Reads only the complete lines after each session's saved offset. A session
    the project refused is not waiting to go, so it is left out.
    """
    count = 0
    for path in spool.session_files(log_dir):
        progress = SessionProgress.load(sidecar_path(path))
        if progress.rejected:
            continue
        try:
            size = path.stat().st_size
            offset = progress.offset if progress.offset <= size else 0
            if offset == size:
                continue
            with path.open("rb") as handle:
                handle.seek(offset)
                for raw in handle:
                    if raw.endswith(b"\n") and _SHOT_MARKER in raw and _is_uploadable_shot(raw):
                        count += 1
        except OSError:
            continue
    return count


def rejected_sessions(log_dir: Path) -> List[Dict[str, Any]]:
    """Sessions the project refused, with the reason, for ``status``."""
    found = []
    for path in spool.session_files(log_dir):
        rejected = SessionProgress.load(sidecar_path(path)).rejected
        if rejected:
            found.append(dict(rejected, file=path.name))
    return found
