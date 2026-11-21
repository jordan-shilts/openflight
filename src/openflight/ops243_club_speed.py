"""OPS243 club speed on the camera trigger: ``S!`` into the rolling buffer.

The swing recorder (``scripts/hardware-test/camera_motion_capture.py``)
measures ball speed with the TI radar, triggered by the camera seeing the
resting ball vanish. Its OPS243-A sits unused because the OPS's own trigger, a
sound sensor on HOST_INT, is faulty, and its internal speed trigger misfires on
this rig (2026-11-19: the ball's return is too weak for SM25). Here the same
camera trigger sends the OPS a software trigger instead. A worker thread owns
the OPS243 serial port in rolling-buffer mode: ``trigger(timestamp)`` only
queues the camera's time, the thread writes ``S!``, reads the dump, runs the
existing ``RollingBufferProcessor`` (ball speed, ``find_club_speed``), re-arms,
and keeps each result by trigger time for the TI radar's worker to collect.

Where the swing lands in the buffer
-----------------------------------
The rolling buffer is a fixed 4,096 samples in 32 segments of 128 (AN-027-A
p2). ``S#n`` keeps n segments from before the trigger and samples 32 - n after
it; at 30 ksps a segment is 4.27 ms, so the buffer is 136.5 ms and the default
``S#16`` gives 68.3 ms before the trigger and 68.3 ms after. (AN-027's own
example, S#8 at 10 ksps, puts the trigger 102 ms = 8 x 12.8 ms after the first
sample; the committed TrackMan session at S#16 logs trigger_time - sample_time
= 68.0 ms on every capture.)

The camera's trigger comes a little after impact. The recorder's
``BallLeavesTrigger`` fires on the first frame in which the ball is gone; on
the 62 swings that reached the screen on 2026-11-19/20 the trigger callback
ran 5.0-5.8 ms (median 5.4, worst 10.9) after that frame's sensor timestamp,
and the clip's flight fit puts impact a median 2 ms after the same timestamp
(-7 to +7). So the camera hands over its trigger about 3.5 ms after impact.
Waking this thread costs well under a millisecond (5 ms at worst, Python's
thread switch interval), ``S!\\r`` is 0.13 ms on the wire at 230,400 baud, and
the firmware takes a trigger at a segment boundary, up to 4.3 ms later. Impact
therefore sits about 4-10 ms (worst ~20 ms) before the OPS's trigger point,
48-64 ms into the buffer. That leaves 45+ ms of club approach, where the club
picker's main branch looks back 30 ms (``CLUB_BRANCH_HISTORY_MS``; its plateau
is the 18 ms up to 4 ms before the ball's onset; only its magnitude fallback
reaches further), and 72-88 ms of ball, more than a
110 mph ball needs for the ~3 m from tee to net (~60 ms). It is also where
the sound trigger put impact when the processor was tuned: the strike's sound
takes ~4.4 ms to cross 1.5 m, and the committed TrackMan session's impacts
sit 2-10 ms before its S#16 trigger point. So S#16 stays. Trigger lag can grow
to ~38 ms before the club's window starts to fall off the front of the buffer;
past the whole 68 ms the impact is not in the buffer at all, and such a late
trigger is not sent (status "late"). ``pre_trigger_segments`` moves the split
if the hardware shows a longer ``S!`` latency; every capture logs the numbers
to judge it by (``send_delay_ms``, ``ops_trigger_lag_ms``,
``impact_before_trigger_ms``).

What gets reported
------------------
A club speed is reported only when the OPS's ball speed agrees with the TI
radar's and the smash factor is physical; otherwise nothing, and the kiosk
keeps no club speed rather than a wrong one:

* Ball agreement: within ``max(4 mph, 5%)`` of the TI ball speed the kiosk
  shows. The OPS reads radial speed, about 2% under the true speed (committed
  TrackMan session: -2.7 mph), while the TI speed is cosine-corrected; the two
  agreed within 1.2-4.7 mph (1-4%) in the 2026-11-19 garage comparison. The
  failures this guards against are far bigger: the OPS reading only the club
  (75-81 mph against a ~110 mph ball that night), or the club approach taken
  for the ball (65 against 162 mph in the TrackMan session).
* Smash factor, TI ball speed over OPS club speed (what the kiosk shows),
  between 1.0 and 1.6. A ball can't leave slower than the club that hit it
  squarely enough to fly, so under 1.0 the pick is the ball itself or a
  stray; the legal limit is about 1.50-1.52, and 1.6 leaves room for the
  OPS's ~2% radial underread of the club plus the 5% ball tolerance.

The club speed itself is the production ``find_club_speed`` (fastest
pre-impact branch, 70th percentile of its plateau). The median pick that
``scripts/analysis/replay_club_speed.py`` proposes for a behind-the-ball
setup beat the old magnitude pick it was written against, but not the
current picker: on the committed 22-shot TrackMan session (OPS behind the
ball) the RMSE is 5.47 mph for production, 5.81 for median and 6.67 for
magnitude, and the median's bias is twice production's (-4.6 against -2.4
mph). Club speed is radial too and needs no cosine correction
(``speed_correction``: the club travels almost along the line of sight).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .clubs import ClubType
from .measured_shot import CLUB_SPEED_MPH
from .rolling_buffer.processor import RollingBufferProcessor
from .rolling_buffer.types import IQCapture, ProcessedCapture

logger = logging.getLogger("openflight.ops243_club_speed")

SAMPLE_RATE_KSPS = 30
SEGMENT_SAMPLES = 128
SEGMENTS = 32
DEFAULT_PORT = "/dev/ttyAMA0"
DEFAULT_PRE_TRIGGER_SEGMENTS = 16
DEFAULT_TRANSMIT_POWER = 3

# The kiosk's label for this sensor (measured_shot.CLUB_SPEED_SOURCES).
CLUB_SPEED_SOURCE = "ops243"

# Gates (see the module docstring).
BALL_AGREEMENT_MPH = 4.0
BALL_AGREEMENT_FRACTION = 0.05
SMASH_RANGE = (1.0, 1.6)

# A dump is ~40.6 KB: 1.8 s at 230,400 baud. A board that will only talk at
# 57,600 or less needs 7+ s per swing, so its club speed would arrive long
# after the shot; it is turned off instead.
MAX_DUMP_S = 4.0
DUMP_TIMEOUT_S = 4.0
# Captures a swing waits for, at most, once its own capture is in flight.
RESULT_WAIT_S = 1.0
# The TI radar and the OPS get the same trigger timestamp; any slack covers rounding.
MATCH_TOLERANCE_S = 0.5
# This many empty or unreadable dumps in a row and the OPS is presumed gone.
MAX_CONSECUTIVE_FAILURES = 3


def split_ms(pre_trigger_segments: int) -> tuple[float, float]:
    """Milliseconds of buffer before and after a trigger for ``S#n``."""
    segment_ms = SEGMENT_SAMPLES / (SAMPLE_RATE_KSPS * 1000.0) * 1000.0
    pre = max(0, min(SEGMENTS, pre_trigger_segments))
    return pre * segment_ms, (SEGMENTS - pre) * segment_ms


def club_type_of(club: Optional[str]) -> ClubType:
    """The recorder's club name ("7-iron") as a ClubType; UNKNOWN when unrecognised."""
    if not club:
        return ClubType.UNKNOWN
    try:
        return ClubType(club.strip().lower())
    except ValueError:
        return ClubType.UNKNOWN


def _rounded(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(value, digits)


@dataclass
class OPSCapture:
    """What one camera trigger got from the OPS243.

    ``status`` is "ok" (a dump with a ball), "no_ball" (a dump with no
    outbound reading), "refused" (the OPS was starting, re-arming or still
    dumping), "late" (the trigger reached the OPS after the impact had left
    the buffer, so no ``S!`` was sent), "no_dump" (``S!`` got no or a short
    reply) or "unparseable". Times: ``send_delay_ms`` is camera
    trigger to ``S!`` written; ``ops_trigger_lag_ms`` is ``S!`` written to the
    board's own trigger point, inferred from the dump's first byte (an upper
    bound: it includes the board's start of output); ``trigger_offset_ms``
    is the board's trigger point in its buffer and ``impact_ms`` the
    processor's club-to-ball transition there.
    """

    trigger_timestamp: float
    status: str
    detail: str = ""
    send_delay_ms: Optional[float] = None
    ops_trigger_lag_ms: Optional[float] = None
    dump_s: Optional[float] = None
    response_bytes: int = 0
    ball_speed_mph: Optional[float] = None
    club_speed_mph: Optional[float] = None
    trigger_offset_ms: Optional[float] = None
    impact_ms: Optional[float] = None
    impact_source: Optional[str] = None
    processed: Optional[ProcessedCapture] = field(default=None, repr=False, compare=False)

    @property
    def impact_before_trigger_ms(self) -> Optional[float]:
        """How long before the board's trigger point the impact was, from the dump."""
        if self.trigger_offset_ms is None or self.impact_ms is None:
            return None
        if self.impact_source != "ops_transition":
            return None
        return self.trigger_offset_ms - self.impact_ms

    def club_speed_for(
        self, club: Optional[str], processor: Optional[RollingBufferProcessor] = None
    ) -> Optional[float]:
        """Club speed for the club in play.

        The worker processes before the club is known; only the wedges' smooth
        club-to-ball merge picks differently, from the same timeline, without
        another FFT.
        """
        club_type = club_type_of(club)
        if club_type is ClubType.UNKNOWN or self.processed is None:
            return self.club_speed_mph
        speed, _timestamp = (processor or RollingBufferProcessor()).find_club_speed(
            self.processed.timeline,
            self.processed.ball_speed_mph,
            self.processed.ball_timestamp_ms,
            club_type=club_type,
        )
        return speed

    def to_dict(self) -> dict:
        """JSON-ready summary for the session logs."""
        out = asdict(self)
        out.pop("processed")
        out["impact_before_trigger_ms"] = self.impact_before_trigger_ms
        return {
            key: round(value, 2)
            if isinstance(value, float) and key != "trigger_timestamp"
            else value
            for key, value in out.items()
        }


@dataclass(frozen=True)
class ClubSpeedVerdict:
    """The OPS club speed for one swing, or why there is none."""

    club_speed_mph: Optional[float]
    reason: str
    ops_ball_mph: Optional[float] = None
    ti_ball_mph: Optional[float] = None
    smash: Optional[float] = None
    capture: Optional[dict] = None

    def to_record(self) -> dict:
        """The swing's ``radar.jsonl`` entry."""
        return {
            "club_speed_mph": self.club_speed_mph,
            "reason": self.reason,
            "ops_ball_mph": _rounded(self.ops_ball_mph, 2),
            "ti_ball_mph": _rounded(self.ti_ball_mph, 2),
            "smash": _rounded(self.smash, 3),
            "capture": self.capture,
        }

    def add_to(self, payload: dict) -> dict:
        """The kiosk payload with this club speed, when there is one: both
        fields or neither, as the kiosk insists."""
        if self.club_speed_mph is None:
            return payload
        out = dict(payload)
        out["club_speed_mph"] = self.club_speed_mph
        out["club_speed_source"] = CLUB_SPEED_SOURCE
        return out

    def line(self) -> str:
        """One log line."""
        if self.club_speed_mph is None:
            return f"no club speed ({self.reason})"
        return (
            f"club {self.club_speed_mph:.1f} mph (OPS ball {self.ops_ball_mph:.1f} vs TI "
            f"{self.ti_ball_mph:.1f} mph, smash {self.smash:.2f})"
        )


def ball_tolerance_mph(ti_ball_mph: float) -> float:
    """How far the OPS ball speed may sit from the TI's and still be the same ball."""
    return max(BALL_AGREEMENT_MPH, BALL_AGREEMENT_FRACTION * ti_ball_mph)


def judge_club_speed(  # pylint: disable=too-many-return-statements
    capture: Optional[OPSCapture],
    ti_ball_mph: Optional[float],
    *,
    club: Optional[str] = None,
    processor: Optional[RollingBufferProcessor] = None,
) -> ClubSpeedVerdict:
    """The club speed to show for this swing, if the gates pass (module docstring)."""
    if capture is None:
        return ClubSpeedVerdict(None, "no OPS capture for this swing", ti_ball_mph=ti_ball_mph)
    summary = capture.to_dict()
    ops_ball = capture.ball_speed_mph

    def refuse(reason: str, smash: Optional[float] = None) -> ClubSpeedVerdict:
        return ClubSpeedVerdict(
            None,
            reason,
            ops_ball_mph=ops_ball,
            ti_ball_mph=ti_ball_mph,
            smash=smash,
            capture=summary,
        )

    if capture.status != "ok":
        return refuse(
            f"OPS capture {capture.status}" + (f": {capture.detail}" if capture.detail else "")
        )
    if ti_ball_mph is None or ti_ball_mph <= 0:
        return refuse("no TI ball speed to check the OPS against")
    if ops_ball is None:
        return refuse("OPS saw no ball")
    tolerance = ball_tolerance_mph(ti_ball_mph)
    if abs(ops_ball - ti_ball_mph) > tolerance:
        return refuse(
            f"ball speeds disagree: OPS {ops_ball:.1f} vs TI {ti_ball_mph:.1f} mph "
            f"(more than {tolerance:.1f})"
        )
    club_speed = capture.club_speed_for(club, processor)
    if club_speed is None or club_speed <= 0:
        return refuse("OPS found no club")
    smash = ti_ball_mph / club_speed
    low, high = SMASH_RANGE
    if not low <= smash <= high:
        return refuse(
            f"smash {smash:.2f} outside {low:.2f}-{high:.2f} (club {club_speed:.1f} mph)",
            smash,
        )
    if not CLUB_SPEED_MPH[0] <= club_speed <= CLUB_SPEED_MPH[1]:
        return refuse(f"club {club_speed:.1f} mph outside what the kiosk takes", smash)
    return ClubSpeedVerdict(
        round(club_speed, 1),
        "ok",
        ops_ball_mph=ops_ball,
        ti_ball_mph=ti_ball_mph,
        smash=smash,
        capture=summary,
    )


def _default_radar(port: str):
    from .ops243 import OPS243Radar  # pylint: disable=import-outside-toplevel

    return OPS243Radar(port=port)


class OPS243ClubSpeed(threading.Thread):
    """Owns the OPS243 in rolling-buffer mode and dumps it on each camera trigger.

    Start it, call ``trigger(timestamp)`` from the trigger path (never blocks:
    it only queues the time), and collect with ``verdict_for``. A missing or
    unresponsive radar prints one warning and turns the feature off; the
    recorder carries on without club speed.
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(  # pylint: disable=too-many-arguments
        self,
        port: str = DEFAULT_PORT,
        *,
        pre_trigger_segments: int = DEFAULT_PRE_TRIGGER_SEGMENTS,
        transmit_power: int = DEFAULT_TRANSMIT_POWER,
        save_path: Optional[Path] = None,
        radar_factory: Callable[[str], object] = _default_radar,
        processor: Optional[RollingBufferProcessor] = None,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = print,
        keep: int = 16,
    ):
        super().__init__(name="ops243-club-speed", daemon=True)
        if not 1 <= pre_trigger_segments <= SEGMENTS:
            # S#0 keeps nothing from before the trigger, and the impact is before it.
            raise ValueError(f"pre_trigger_segments must be 1-{SEGMENTS}")
        if not 0 <= transmit_power <= 7:
            raise ValueError("transmit_power must be 0 (max) to 7 (min)")
        self.port = port
        self.pre_trigger_segments = pre_trigger_segments
        self.transmit_power = transmit_power
        self.pre_ms, self.post_ms = split_ms(pre_trigger_segments)
        self.save_path = save_path
        self.radar_factory = radar_factory
        self.processor = processor or RollingBufferProcessor()
        self.clock = clock
        self.log = log
        self.radar = None
        self.enabled = True
        self.off_reason: Optional[str] = None
        self.refused = 0
        self.unsolicited = 0
        self._saved = 0
        self._triggers: queue.Queue[Optional[float]] = queue.Queue(maxsize=1)
        self._results: deque[OPSCapture] = deque(maxlen=keep)
        self._condition = threading.Condition()
        # _armed: a trigger now gets a dump (the trigger path reads it).
        # _needs_rearm: the worker owes the board a re-arm (its own state).
        self._armed = False
        self._needs_rearm = False
        self._in_flight: Optional[float] = None
        self._failures = 0
        self._stop_event = threading.Event()

    # -- the trigger path ---------------------------------------------------

    @property
    def armed(self) -> bool:
        """Whether a trigger now would get a dump."""
        with self._condition:
            return self._armed and self.enabled

    def trigger(self, timestamp: float) -> bool:
        """Queue a camera trigger for ``S!``; False when the OPS can't take it now.

        Called on the camera's trigger path: it takes a lock and queues a
        float, never touching the serial port.
        """
        timestamp = float(timestamp)
        with self._condition:
            if not self.enabled:
                return False
            why = None
            if self.radar is None:
                why = "the OPS243 is still starting"
            elif self._in_flight is not None:
                why = "the last dump is still in flight"
            elif not self._armed:
                why = "the OPS243 is re-arming after the last dump"
            else:
                try:
                    self._triggers.put_nowait(timestamp)
                except queue.Full:
                    why = "the last trigger is still queued"
            if why is not None:
                self.refused += 1
                self._results.append(OPSCapture(timestamp, "refused", detail=why))
                self._condition.notify_all()
                return False
            self._in_flight = timestamp
            self._armed = False
        return True

    # -- results by trigger time --------------------------------------------

    def near(
        self, timestamp: float, tolerance_s: float = MATCH_TOLERANCE_S
    ) -> Optional[OPSCapture]:
        """The capture triggered closest to ``timestamp``, within the tolerance."""
        with self._condition:
            return self._closest(timestamp, tolerance_s)

    def _closest(self, timestamp: float, tolerance_s: float) -> Optional[OPSCapture]:
        items = [
            item for item in self._results if abs(item.trigger_timestamp - timestamp) <= tolerance_s
        ]
        if not items:
            return None
        return min(items, key=lambda item: abs(item.trigger_timestamp - timestamp))

    def wait_near(
        self,
        timestamp: float,
        timeout_s: float = RESULT_WAIT_S,
        tolerance_s: float = MATCH_TOLERANCE_S,
    ) -> Optional[OPSCapture]:
        """``near``, waiting up to ``timeout_s`` only while this swing's own capture
        is still in flight. A swing the OPS never took returns at once."""
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while True:
                found = self._closest(timestamp, tolerance_s)
                if found is not None:
                    return found
                pending = self._in_flight
                if pending is None or abs(pending - timestamp) > tolerance_s:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def verdict_for(
        self,
        timestamp: float,
        ti_ball_mph: Optional[float],
        *,
        club: Optional[str] = None,
        wait_s: float = RESULT_WAIT_S,
    ) -> ClubSpeedVerdict:
        """This swing's club speed, or why there is none."""
        capture = self.wait_near(timestamp, wait_s)
        if capture is None and not self.enabled:
            return ClubSpeedVerdict(
                None, f"OPS243 off: {self.off_reason or 'stopped'}", ti_ball_mph=ti_ball_mph
            )
        if capture is None and self.near_in_flight(timestamp):
            return ClubSpeedVerdict(
                None, "OPS dump still in flight (waited too long)", ti_ball_mph=ti_ball_mph
            )
        return judge_club_speed(capture, ti_ball_mph, club=club, processor=self.processor)

    def near_in_flight(self, timestamp: float) -> bool:
        """Whether this swing's capture is still being read from the OPS."""
        with self._condition:
            pending = self._in_flight
        return pending is not None and abs(pending - timestamp) <= MATCH_TOLERANCE_S

    # -- the worker thread --------------------------------------------------

    def run(self) -> None:
        try:
            self.radar = self._open()
        except Exception as error:  # pylint: disable=broad-exception-caught
            self._turn_off(f"no OPS243 on {self.port} ({error})")
            return
        with self._condition:
            self._armed = True
        self.log(
            f"OPS243 club speed on {self.port} at {self.radar.baud} baud: S#"
            f"{self.pre_trigger_segments} keeps {self.pre_ms:.0f} ms before each camera "
            f"trigger and {self.post_ms:.0f} ms after"
        )
        try:
            while not self._stop_event.is_set():
                try:
                    timestamp = self._triggers.get(timeout=0.25)
                except queue.Empty:
                    self._idle()
                    continue
                if timestamp is None:
                    break
                self._take(timestamp)
        except Exception as error:  # pylint: disable=broad-exception-caught
            self._turn_off(f"OPS243 stopped: {error}")
        finally:
            self._close()

    def stop(self, timeout_s: float = 6.0) -> None:
        """Stop the thread; a dump in flight is allowed to finish."""
        self._stop_event.set()
        try:
            self._triggers.put_nowait(None)
        except queue.Full:
            pass
        if self.is_alive():
            self.join(timeout=timeout_s)

    def _open(self):
        radar = self.radar_factory(self.port)
        try:
            radar.connect(idle_first=True)
            dump_s = radar.DUMP_BYTES / radar.bytes_per_second
            if dump_s > MAX_DUMP_S:
                raise ConnectionError(
                    f"its UART only reached {radar.baud} baud, {dump_s:.0f} s per dump"
                )
            radar.configure_for_rolling_buffer(
                pre_trigger_segments=self.pre_trigger_segments,
                sample_rate_ksps=SAMPLE_RATE_KSPS,
                transmit_power=self.transmit_power,
            )
        except Exception:
            radar.disconnect()
            raise
        return radar

    def _close(self) -> None:
        with self._condition:
            self._armed = False
            self._condition.notify_all()
        if self.radar is not None:
            try:
                self.radar.disconnect()
            except Exception:  # pylint: disable=broad-exception-caught
                logger.debug("[OPS-CLUB] disconnect failed", exc_info=True)

    def _turn_off(self, reason: str) -> None:
        """One warning, then the feature is off for the rest of the run."""
        with self._condition:
            already_off = not self.enabled
            self.enabled = False
            self.off_reason = reason
            self._armed = False
            self._in_flight = None
            self._condition.notify_all()
        self._stop_event.set()
        if not already_off:
            self.log(f"WARNING: OPS243 club speed off: {reason}. Swings go on without club speed.")
            logger.warning("[OPS-CLUB] off: %s", reason)

    def _idle(self) -> None:
        """Between swings: retry a failed re-arm; clear a dump nobody asked for."""
        if self._needs_rearm:
            self._rearm()
            return
        serial_port = getattr(self.radar, "serial", None)
        if serial_port is None or not serial_port.in_waiting:
            return
        with self._condition:
            if self._in_flight is not None:
                return  # a trigger just arrived; its S! comes next
            self._armed = False
        # HOST_INT still wired to the faulty sound sensor, or line noise: the
        # board dumped on its own and now idles until re-armed.
        self.unsolicited += 1
        if self.unsolicited == 1 or self.unsolicited % 10 == 0:
            self.log(
                f"OPS243 sent a dump nobody asked for ({self.unsolicited} so far; "
                "HOST_INT?); re-arming"
            )
        self._rearm()

    def _take(self, timestamp: float) -> None:
        """One trigger: S!, read, process, keep the result, re-arm."""
        before_send = self.clock()
        send_delay_ms = (before_send - timestamp) * 1000.0
        if send_delay_ms > self.pre_ms:
            # The impact has already left the buffer; a dump would only make
            # the OPS deaf for the next swing. It is still armed.
            self._keep(
                OPSCapture(
                    timestamp,
                    "late",
                    detail=(
                        f"S! would go {send_delay_ms:.0f} ms after the camera trigger; "
                        f"the buffer holds {self.pre_ms:.0f} ms before it"
                    ),
                    send_delay_ms=send_delay_ms,
                ),
                rearmed=True,
            )
            return
        try:
            response = self.radar.trigger_capture(timeout=DUMP_TIMEOUT_S)
            result = self._process(timestamp, response, sent_by=before_send)
        except Exception as error:  # pylint: disable=broad-exception-caught
            result = OPSCapture(timestamp, "no_dump", detail=f"{type(error).__name__}: {error}")
        # S! is written microseconds after this thread picks the trigger up.
        result.send_delay_ms = send_delay_ms
        self._keep(result, rearmed=False)
        if result.status in ("no_dump", "unparseable"):
            self._failures += 1
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                self._turn_off(
                    f"{self._failures} triggers in a row got no readable dump ({result.detail})"
                )
                return
        else:
            self._failures = 0
        self._rearm()

    def _process(self, timestamp: float, response: str, *, sent_by: float) -> OPSCapture:
        """Parse and process one S! reply into the swing's OPSCapture."""
        sent = getattr(self.radar, "last_software_trigger_timestamp", None) or sent_by
        first_byte = getattr(self.radar, "last_software_trigger_first_byte_timestamp", None)
        dump_s = self.clock() - sent
        if len(response) < 1000:
            return OPSCapture(
                timestamp,
                "no_dump",
                detail=f"{len(response)} bytes back from S!",
                dump_s=dump_s,
                response_bytes=len(response),
            )
        capture = self.processor.parse_capture(response, first_byte_timestamp=first_byte)
        if capture is None:
            return OPSCapture(
                timestamp,
                "unparseable",
                detail="dump incomplete or I/Q lengths differ",
                dump_s=dump_s,
                response_bytes=len(response),
            )
        lag_ms = None
        if capture.trigger_timestamp is not None:
            lag_ms = (capture.trigger_timestamp - sent) * 1000.0
        result = OPSCapture(
            timestamp,
            "no_ball",
            ops_trigger_lag_ms=lag_ms,
            dump_s=dump_s,
            response_bytes=len(response),
            trigger_offset_ms=capture.trigger_offset_ms,
        )
        processed = self.processor.process_capture(capture)
        if processed is not None:
            result.status = "ok"
            result.processed = processed
            result.ball_speed_mph = processed.ball_speed_mph
            result.club_speed_mph = processed.club_speed_mph
            result.impact_ms = processed.impact.timestamp_ms if processed.impact else None
            result.impact_source = processed.impact.source if processed.impact else None
        self._save(result, capture)
        return result

    def _save(self, result: OPSCapture, capture: IQCapture) -> None:
        """Every dump, replayable by scripts/analysis/replay_captures.py."""
        if self.save_path is None:
            return
        self._saved += 1
        processed = result.processed
        entry = {
            "type": "rolling_buffer_capture",
            "source": "ops243_club_speed",
            "shot_number": self._saved,
            "pre_trigger_segments": self.pre_trigger_segments,
            "sample_time": capture.sample_time,
            "trigger_time": capture.trigger_time,
            "trigger_offset_ms": capture.trigger_offset_ms,
            "sample_count": capture.num_samples,
            "ball_timestamp_ms": processed.ball_timestamp_ms if processed else None,
            "club_timestamp_ms": processed.club_timestamp_ms if processed else None,
            **result.to_dict(),
            "i_samples": list(capture.i_samples),
            "q_samples": list(capture.q_samples),
        }
        try:
            self.save_path.parent.mkdir(parents=True, exist_ok=True)
            with self.save_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError as error:
            logger.warning("[OPS-CLUB] could not save the dump: %s", error)

    def _keep(self, result: OPSCapture, *, rearmed: bool) -> None:
        with self._condition:
            self._results.append(result)
            self._in_flight = None
            if rearmed:
                self._armed = True
            self._condition.notify_all()

    def _rearm(self) -> None:
        """PA, S#n, PA after a dump; twice before counting it a failure."""
        for _attempt in range(2):
            if self.radar.rearm_rolling_buffer(pre_trigger_segments=self.pre_trigger_segments):
                self._needs_rearm = False
                with self._condition:
                    self._armed = True
                return
        self._needs_rearm = True
        self._failures += 1
        if self._failures >= MAX_CONSECUTIVE_FAILURES:
            self._turn_off("the OPS243 stopped taking re-arm commands")
