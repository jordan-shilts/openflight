"""Map allowlisted session-log entries to ``golf_sessions`` / ``golf_shots`` rows.

Ids are deterministic so sending the same data again is an idempotent upsert:

- A session's id is the one FlightWeb uses (``filtering.session_id_for``): the
  ``session_uuid`` embedded in ``session_start``, else a UUIDv5 of the
  filename. Supabase has no FlightWeb device id, so the fallback is keyed by
  the filename alone.
- A shot's id is a UUIDv5 in the session's namespace of its ``shot_number``,
  which the server assigns once per shot and never reuses within a session. A
  shot logged without a number falls back to the byte offset of its line,
  stable because session files are only ever appended to.
"""

import math
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, Optional

from ...d_plane import trusted_attack_angle, trusted_club_path
from .. import filtering

# Stands in for the FlightWeb device id in the fallback session id.
FALLBACK_DEVICE_KEY = ""

# Shot.to_dict() key for each numeric golf_shots column read straight across.
SHOT_NUMBER_COLUMNS = {
    "ball_speed_mph": "ball_speed_mph",
    "club_speed_mph": "club_speed_mph",
    "smash_factor": "smash_factor",
    "launch_v_deg": "launch_angle_vertical",
    "launch_h_deg": "launch_angle_horizontal",
    "spin_rpm": "spin_rpm",
    "spin_axis_deg": "spin_axis_deg",
    "carry_yards": "carry_spin_adjusted",
    "face_angle_deg": "face_angle_deg",
    "dynamic_loft_deg": "dynamic_loft_deg",
}

_DELIVERY_NUMBERS = (
    "club_angle_deg",
    "club_path_deg",
    "experimental_fused_attack_angle_deg",
    "experimental_fused_club_path_deg",
)
_DELIVERY_TIERS = (
    "experimental_fused_attack_angle_confidence",
    "experimental_fused_club_path_confidence",
)


def _number(value: Any) -> Optional[float]:
    """A finite float, or None for missing, non-numeric, boolean, NaN or infinite values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def json_safe(value: Any) -> Any:
    """``value`` with NaN and infinities replaced by None.

    Python's json module writes and reads them, but they are not JSON and a
    jsonb column refuses them.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def to_timestamptz(value: Any) -> Optional[str]:
    """An ISO 8601 timestamp with a UTC offset, or None if ``value`` is not one.

    The session logger writes the Pi's local time without an offset; it gets
    the Pi's offset for that date, because a timestamptz column would read it
    as UTC.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
    except (ValueError, OverflowError, OSError):
        return None
    return parsed.isoformat()


def session_id(session_uuid: Any, filename: str) -> str:
    """The session's row id (see module docstring)."""
    if session_uuid:
        try:
            uuid.UUID(str(session_uuid))
        except ValueError:
            session_uuid = None  # not a uuid column value: use the fallback
    return filtering.session_id_for(session_uuid, FALLBACK_DEVICE_KEY, filename)


def shot_id(session: str, shot_number: Optional[int], line_offset: int) -> str:
    """The shot's row id (see module docstring)."""
    key = f"shot:{shot_number}" if shot_number is not None else f"line:{line_offset}"
    return str(uuid.uuid5(uuid.UUID(session), key))


def session_row(
    session: str,
    filename: str,
    start_entry: Optional[Dict[str, Any]],
    end_entry: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """The ``golf_sessions`` row from the session's start and end entries."""
    start = start_entry or {}
    end = end_entry or {}
    return {
        "id": session,
        "started_at": to_timestamptz(start.get("start_time") or start.get("ts")),
        "ended_at": to_timestamptz(end.get("end_time") or end.get("ts")),
        "info": json_safe(
            {"file": filename, "session_start": start_entry, "session_end": end_entry}
        ),
    }


def shot_row(entry: Dict[str, Any], session: str, line_offset: int) -> Dict[str, Any]:
    """The ``golf_shots`` row for one ``shot_detected`` entry.

    ``data`` holds the whole entry. Club path and attack angle are the ones the
    D-plane and simulators use: the club radar's, else the camera's fused one
    at a trusted tier.
    """
    delivery = SimpleNamespace(
        **{name: _number(entry.get(name)) for name in _DELIVERY_NUMBERS},
        **{name: entry.get(name) for name in _DELIVERY_TIERS},
    )
    club_path = trusted_club_path(delivery)
    attack_angle = trusted_attack_angle(delivery)
    shot_number = _integer(entry.get("shot_number"))

    row: Dict[str, Any] = {
        "id": shot_id(session, shot_number, line_offset),
        "session_id": session,
        "shot_number": shot_number,
        "shot_at": to_timestamptz(entry.get("timestamp") or entry.get("ts")),
        "club": _text(entry.get("club")),
        "profile_name": _text(entry.get("profile_name")),
    }
    for column, key in SHOT_NUMBER_COLUMNS.items():
        row[column] = _number(entry.get(key))
    row["club_path_deg"] = club_path[0] if club_path else None
    row["attack_angle_deg"] = attack_angle[0] if attack_angle else None
    row["data"] = json_safe(entry)
    return row
