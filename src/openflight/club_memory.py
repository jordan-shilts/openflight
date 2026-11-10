"""Remember the club picked on the kiosk across restarts.

Without this every start comes up on Driver, and the first swings of a session
are tagged with the wrong club until someone notices.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Union

from .clubs import ClubType

logger = logging.getLogger(__name__)

DEFAULT_CLUB_PATH = Path.home() / ".config" / "openflight" / "club.json"
CLUB_PATH_ENV = "OPENFLIGHT_CLUB_PATH"


def resolve_club_path(path: Union[str, Path, None] = None) -> Path:
    """Argument, then ``OPENFLIGHT_CLUB_PATH``, then the user default.

    Tests point the env var at a temp file so they cannot rewrite
    ``~/.config/openflight/club.json``.
    """
    if path is not None and str(path).strip():
        return Path(path).expanduser()
    env_path = (os.environ.get(CLUB_PATH_ENV) or "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_CLUB_PATH


def load_saved_club(path: Union[str, Path, None] = None) -> ClubType | None:
    """The club saved by the last selection; None when there is none to trust."""
    club_path = resolve_club_path(path)
    try:
        saved = json.loads(club_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        logger.warning("[CLUB] Could not read the saved club at %s: %s", club_path, error)
        return None
    try:
        club = ClubType(saved.get("club") if isinstance(saved, dict) else None)
    except ValueError:
        logger.warning("[CLUB] Ignoring an unknown saved club in %s", club_path)
        return None
    return None if club is ClubType.UNKNOWN else club


def save_club(club: ClubType, path: Union[str, Path, None] = None) -> bool:
    """Write the selection atomically. False when it could not be saved."""
    club_path = resolve_club_path(path)
    temp_path = club_path.with_name(f"{club_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        club_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(json.dumps({"club": club.value}) + "\n", encoding="utf-8")
        os.replace(temp_path, club_path)
    except OSError as error:
        logger.warning("[CLUB] Could not save the club to %s: %s", club_path, error)
        with contextlib.suppress(OSError):
            temp_path.unlink(missing_ok=True)
        return False
    return True
