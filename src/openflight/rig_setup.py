"""Saved rig setups: where the radar sits relative to the ball, per location.

Launch angle needs the radar-to-ball distance. A fixed simulator is taped once.
On the course the distance changes every time the case is set down, so a guess
would be the largest error in the system. The golf ball solves that: it is
always the same size, so its apparent width gives its distance once the lens
scale is known, and swings hit in the taped simulator setup teach that scale.

The learned value is diameter_px * floor_distance_m rather than a focal length.
It absorbs the fixed offsets between the lens and the radar in the case, which
stay the same as long as the case sits on the ground in both setups.
"""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path

logger = logging.getLogger(__name__)

SIMULATOR = "simulator"
COURSE = "course"
SETUP_IDS = (SIMULATOR, COURSE)

DEFAULT_RADAR_HEIGHT_M = 0.1524  # the reference IWR6843 calibration's height
BALL_DISTANCE_LIMITS_M = (0.6, 6.0)
NET_DISTANCE_LIMITS_M = (1.0, 15.0)
RADAR_HEIGHT_LIMITS_M = (0.02, 1.5)
# Tighter than a typed distance: beyond this the ball is too few pixels to trust.
CAMERA_DISTANCE_LIMITS_M = (0.6, 3.5)
BALL_DIAMETER_LIMITS_PX = (6.0, 60.0)
PITCH_LIMITS_DEG = (-45.0, 45.0)
MAX_TILT_OFFSET_DEG = 20.0
MAX_SAMPLES = 25
MIN_SCALE_SAMPLES = 3

_UPDATE_KEYS = frozenset(
    {
        "active",
        "setup",
        "ball_distance_m",
        "auto_distance",
        "net_enabled",
        "net_distance_m",
        "radar_height_m",
    }
)


class RigSetupError(ValueError):
    """A setup change that cannot be applied; the message is safe to show."""


@dataclass(frozen=True)
class RigSetup:
    """One location's settings."""

    ball_distance_m: float | None
    auto_distance: bool
    net_enabled: bool
    net_distance_m: float
    ball_height_m: float


@dataclass(frozen=True)
class RigGeometry:
    """What the radar pipeline needs for the next swing."""

    setup_id: str
    ball_distance_m: float | None
    distance_source: str  # "camera", "manual" or "none"
    tee_range_m: float | None
    net_range_m: float | None
    radar_height_m: float
    ball_height_m: float
    tilt_offset_deg: float


def _default_setups() -> dict[str, RigSetup]:
    # The mat lifts the ball's centre to about 40 mm; on grass it is the radius.
    return {
        SIMULATOR: RigSetup(
            ball_distance_m=None,
            auto_distance=False,
            net_enabled=True,
            net_distance_m=4.6,
            ball_height_m=0.040,
        ),
        COURSE: RigSetup(
            ball_distance_m=None,
            auto_distance=True,
            net_enabled=False,
            net_distance_m=4.6,
            ball_height_m=0.021,
        ),
    }


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _within(value: object, limits: tuple[float, float]) -> bool:
    return _is_number(value) and limits[0] <= float(value) <= limits[1]


def _checked(name: str, value: object, limits: tuple[float, float]) -> float:
    if not _is_number(value):
        raise RigSetupError(f"{name} must be a number")
    if not _within(value, limits):
        raise RigSetupError(f"{name} must be between {limits[0]:g} and {limits[1]:g}")
    return round(float(value), 4)


def _checked_flag(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise RigSetupError(f"{name} must be true or false")
    return value


class RigSetupStore:
    """Thread-safe, file-backed rig setups. A bad file falls back to defaults."""

    def __init__(self, path: Path | str):
        self._path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._active = SIMULATOR
        self._setups = _default_setups()
        self._radar_height_m = DEFAULT_RADAR_HEIGHT_M
        self._scale_samples: list[float] = []
        self._pitch_samples: list[float] = []
        # Where the ball is right now. Not saved: it moves with every setup.
        self._camera_distance_m: float | None = None
        self._pitch_deg: float | None = None
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("[RIG] Ignoring unreadable %s: %s", self._path, error)
            return
        if not isinstance(data, dict):
            logger.warning("[RIG] Ignoring malformed %s", self._path)
            return
        if data.get("active") in SETUP_IDS:
            self._active = data["active"]
        saved = data.get("setups")
        if isinstance(saved, dict):
            for setup_id in SETUP_IDS:
                if isinstance(saved.get(setup_id), dict):
                    self._setups[setup_id] = self._restored(setup_id, saved[setup_id])
        if _within(data.get("radar_height_m"), RADAR_HEIGHT_LIMITS_M):
            self._radar_height_m = float(data["radar_height_m"])
        self._scale_samples = self._numbers(data.get("scale_samples"))
        self._pitch_samples = self._numbers(data.get("pitch_samples"))

    def _restored(self, setup_id: str, saved: dict) -> RigSetup:
        """A saved setup, keeping the default for any field that is not valid."""
        default = self._setups[setup_id]
        distance = saved.get("ball_distance_m")
        return replace(
            default,
            ball_distance_m=(
                float(distance)
                if _within(distance, BALL_DISTANCE_LIMITS_M)
                else default.ball_distance_m
            ),
            auto_distance=(
                saved["auto_distance"]
                if isinstance(saved.get("auto_distance"), bool)
                else default.auto_distance
            ),
            net_enabled=(
                saved["net_enabled"]
                if isinstance(saved.get("net_enabled"), bool)
                else default.net_enabled
            ),
            net_distance_m=(
                float(saved["net_distance_m"])
                if _within(saved.get("net_distance_m"), NET_DISTANCE_LIMITS_M)
                else default.net_distance_m
            ),
        )

    @staticmethod
    def _numbers(values: object) -> list[float]:
        if not isinstance(values, list):
            return []
        return [float(value) for value in values if _is_number(value)][-MAX_SAMPLES:]

    def _save(self) -> None:
        """Write atomically; a failed save is logged and the change stays in memory."""
        payload = {
            "active": self._active,
            "setups": {setup_id: asdict(setup) for setup_id, setup in self._setups.items()},
            "radar_height_m": self._radar_height_m,
            "scale_samples": self._scale_samples,
            "pitch_samples": self._pitch_samples,
        }
        temporary = self._path.with_name(f"{self._path.name}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temporary, self._path)
        except OSError as error:
            logger.warning("[RIG] Could not save %s: %s", self._path, error)

    def _learns_camera_scale(self) -> bool:
        """Only a taped distance may teach the scale, never a course estimate."""
        setup = self._setups[self._active]
        return (
            self._active == SIMULATOR
            and not setup.auto_distance
            and setup.ball_distance_m is not None
        )

    def _camera_scale(self) -> float | None:
        if len(self._scale_samples) < MIN_SCALE_SAMPLES:
            return None
        return statistics.median(self._scale_samples)

    def _ball_distance(self) -> tuple[float | None, str]:
        setup = self._setups[self._active]
        if setup.auto_distance and self._camera_distance_m is not None:
            return self._camera_distance_m, "camera"
        if setup.ball_distance_m is not None:
            return setup.ball_distance_m, "manual"
        return None, "none"

    def _tilt_offset(self) -> float:
        """How far the case is tipped compared with the simulator."""
        if self._learns_camera_scale() or self._pitch_deg is None or not self._pitch_samples:
            return 0.0
        offset = self._pitch_deg - statistics.median(self._pitch_samples)
        return max(-MAX_TILT_OFFSET_DEG, min(MAX_TILT_OFFSET_DEG, offset))

    def _geometry(self) -> RigGeometry:
        setup = self._setups[self._active]
        distance, source = self._ball_distance()
        tee_range = (
            None
            if distance is None
            else math.hypot(distance, self._radar_height_m - setup.ball_height_m)
        )
        return RigGeometry(
            setup_id=self._active,
            ball_distance_m=distance,
            distance_source=source,
            tee_range_m=tee_range,
            net_range_m=setup.net_distance_m if setup.net_enabled else None,
            radar_height_m=self._radar_height_m,
            ball_height_m=setup.ball_height_m,
            tilt_offset_deg=self._tilt_offset(),
        )

    def geometry(self) -> RigGeometry:
        """The geometry for the next swing."""
        with self._lock:
            return self._geometry()

    def snapshot(self) -> dict:
        """Everything the setup screen shows."""
        with self._lock:
            geometry = self._geometry()
            return {
                "active": self._active,
                "setups": {setup_id: asdict(setup) for setup_id, setup in self._setups.items()},
                "radar_height_m": self._radar_height_m,
                "camera_ready": self._camera_scale() is not None,
                "scale_samples_needed": max(0, MIN_SCALE_SAMPLES - len(self._scale_samples)),
                "effective": {
                    "ball_distance_m": geometry.ball_distance_m,
                    "distance_source": geometry.distance_source,
                    "net_distance_m": geometry.net_range_m,
                },
            }

    def update(self, payload: object) -> dict:
        """Apply a change from the screen or phone; all-or-nothing."""
        if not isinstance(payload, dict):
            raise RigSetupError("send a JSON object")
        unknown = set(payload) - _UPDATE_KEYS
        if unknown:
            raise RigSetupError(f"unknown setting: {sorted(unknown)[0]}")
        with self._lock:
            active = payload.get("active", self._active)
            if active not in SETUP_IDS:
                raise RigSetupError(f"active must be one of {', '.join(SETUP_IDS)}")
            setup_id = payload.get("setup", active)
            if setup_id not in SETUP_IDS:
                raise RigSetupError(f"setup must be one of {', '.join(SETUP_IDS)}")

            setup = self._setups[setup_id]
            changes: dict = {}
            if "ball_distance_m" in payload:
                changes["ball_distance_m"] = _checked(
                    "ball_distance_m", payload["ball_distance_m"], BALL_DISTANCE_LIMITS_M
                )
                changes["auto_distance"] = False  # adjusting by hand leaves auto mode
            if "auto_distance" in payload:
                changes["auto_distance"] = _checked_flag("auto_distance", payload["auto_distance"])
            if "net_enabled" in payload:
                changes["net_enabled"] = _checked_flag("net_enabled", payload["net_enabled"])
            if "net_distance_m" in payload:
                changes["net_distance_m"] = _checked(
                    "net_distance_m", payload["net_distance_m"], NET_DISTANCE_LIMITS_M
                )
            radar_height = (
                _checked("radar_height_m", payload["radar_height_m"], RADAR_HEIGHT_LIMITS_M)
                if "radar_height_m" in payload
                else self._radar_height_m
            )

            updated = replace(setup, **changes)
            if setup_id == SIMULATOR and updated.ball_distance_m != setup.ball_distance_m:
                # A corrected tape number invalidates what the old one taught.
                self._scale_samples = []
            if active != self._active:
                self._camera_distance_m = None  # a different place, a different ball
                self._pitch_deg = None
            self._active = active
            self._setups[setup_id] = updated
            self._radar_height_m = radar_height
            self._save()
        return self.snapshot()

    def observe(
        self,
        *,
        diameter_px: float | None = None,
        pitch_deg: float | None = None,
    ) -> RigGeometry:
        """Take one swing's camera and tilt readings; return that swing's geometry.

        Readings that make no sense are ignored rather than rejected, so one bad
        frame never costs the shot; the geometry falls back to the last good value.
        """
        with self._lock:
            learning = self._learns_camera_scale()
            setup = self._setups[self._active]
            changed = False
            if _within(diameter_px, BALL_DIAMETER_LIMITS_PX):
                scale = self._camera_scale()
                if learning:
                    sample = float(diameter_px) * setup.ball_distance_m
                    self._scale_samples = [*self._scale_samples, sample][-MAX_SAMPLES:]
                    changed = True
                elif setup.auto_distance and scale is not None:
                    distance = scale / float(diameter_px)
                    if _within(distance, CAMERA_DISTANCE_LIMITS_M):
                        self._camera_distance_m = round(distance, 4)
                    else:
                        logger.info("[RIG] Ignoring camera ball distance %.2f m", distance)
            if _within(pitch_deg, PITCH_LIMITS_DEG):
                self._pitch_deg = float(pitch_deg)
                if learning:
                    self._pitch_samples = [*self._pitch_samples, float(pitch_deg)][-MAX_SAMPLES:]
                    changed = True
            if changed:
                self._save()
            return self._geometry()
