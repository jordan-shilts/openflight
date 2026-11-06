"""Read OpenFlight session logs for offline analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

SESSION_GLOB = "session_*.jsonl"

_ENTRY_LISTS = {
    "shot_detected": "shots",
    "trigger_event": "triggers",
    "iwr6843_capture": "iwr6843_captures",
    "error": "errors",
    "sim_send": "sim_sends",
}


@dataclass
class SessionLog:
    """One session file, keeping only the entry types the report reads."""

    path: Path
    start: dict = field(default_factory=dict)
    shots: list[dict] = field(default_factory=list)
    triggers: list[dict] = field(default_factory=list)
    iwr6843_captures: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    sim_sends: list[dict] = field(default_factory=list)
    malformed_lines: int = 0

    @property
    def session_id(self) -> str:
        """Session id from the start entry, else the file name."""
        return str(self.start.get("session_id") or self.path.stem)

    @property
    def start_time(self) -> str:
        """ISO start time, or an empty string when the start entry is missing."""
        return str(self.start.get("start_time") or "")

    @property
    def mode(self) -> str:
        """Capture mode recorded at session start."""
        return str(self.start.get("mode") or "unknown")

    @property
    def is_mock(self) -> bool:
        """Whether every shot in the session was simulated."""
        return self.mode == "mock"


def load_session(path: Path) -> SessionLog:
    """Parse one JSONL session, counting lines that are not JSON objects."""
    session = SessionLog(path=path)
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                session.malformed_lines += 1
                continue
            if not isinstance(entry, dict):
                session.malformed_lines += 1
                continue
            entry_type = entry.get("type")
            if entry_type == "session_start":
                session.start = entry
            elif entry_type in _ENTRY_LISTS:
                getattr(session, _ENTRY_LISTS[entry_type]).append(entry)
    return session


def find_session_files(paths: Iterable[Path]) -> list[Path]:
    """Expand directories to their session logs, each file listed once."""
    found: dict[Path, None] = {}
    for path in paths:
        path = Path(path).expanduser()
        if path.is_dir():
            for child in sorted(path.glob(SESSION_GLOB)):
                found[child.resolve()] = None
        elif path.is_file():
            found[path.resolve()] = None
        else:
            raise FileNotFoundError(f"no session log or directory at {path}")
    return list(found)


def load_sessions(paths: Iterable[Path]) -> list[SessionLog]:
    """Load every session under ``paths``, oldest first."""
    sessions = [load_session(path) for path in find_session_files(paths)]
    return sorted(sessions, key=lambda session: (session.start_time, session.path.name))
