"""Whether the swing recorder is ready for a swing or still reading the radar.

The camera-triggered recorder takes about seven seconds to pull a radar dump
over UART, and a swing made in that window is not measured. It reports
"measuring" when a swing freezes the radar and "ready" when it can take the
next one, so the kiosk can say so. The recorder repeats its state every few
seconds; when it goes quiet the status falls back to "offline", so the screen
never shows Ready for a recorder that has stopped.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable

OFFLINE = "offline"
READY = "ready"
MEASURING = "measuring"
STATES = (OFFLINE, READY, MEASURING)
# What the last wait ended with; sent once, alongside the "ready" that follows it.
RESULTS = ("shot", "no_ball", "failed")
STALE_AFTER_S = 30.0
MAX_EXPECTED_S = 60.0


def parse_report(payload) -> dict:
    """Validated keyword arguments for ``MeasuringStatus.report``; ValueError names the field."""
    if not isinstance(payload, dict):
        raise ValueError("Send a JSON object")
    state = payload.get("state")
    if state not in STATES:
        raise ValueError(f"state must be one of: {', '.join(STATES)}")
    result = payload.get("result")
    if result is not None and result not in RESULTS:
        raise ValueError(f"result must be one of: {', '.join(RESULTS)}")
    if result is not None and state != READY:
        raise ValueError("result only comes with state ready")
    expected_s = payload.get("expected_s")
    if expected_s is not None:
        if (
            isinstance(expected_s, bool)
            or not isinstance(expected_s, (int, float))
            or not math.isfinite(expected_s)
            or not 0 < expected_s <= MAX_EXPECTED_S
        ):
            raise ValueError(f"expected_s must be a number up to {MAX_EXPECTED_S:g}")
        expected_s = float(expected_s)
    return {"state": state, "result": result, "expected_s": expected_s}


class MeasuringStatus:
    """The recorder's last reported state, forgotten once it stops reporting."""

    def __init__(
        self,
        on_change: Callable[[dict], None],
        *,
        stale_after_s: float = STALE_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
        timer_factory: Callable[..., threading.Timer] = threading.Timer,
    ):
        self._on_change = on_change
        self._stale_after_s = stale_after_s
        self._clock = clock
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._state = OFFLINE
        self._expected_s: float | None = None
        self._started: float | None = None
        self._timer: threading.Timer | None = None

    def snapshot(self) -> dict:
        """State for a client that just connected, with how far into a wait it is."""
        with self._lock:
            return self._payload()

    def report(
        self, state: str, *, expected_s: float | None = None, result: str | None = None
    ) -> dict:
        """Record what the recorder said; tell the screen only when it is news.

        A repeated state is a heartbeat: it keeps the status alive without
        restarting the countdown on screen.
        """
        with self._lock:
            changed = state != self._state or result is not None
            if changed:
                self._state = state
                self._expected_s = expected_s if state == MEASURING else None
                self._started = self._clock() if state == MEASURING else None
            self._restart_timer(state)
            payload = self._payload()
        if result is not None:
            payload["result"] = result
        if changed:
            self._on_change(payload)
        return payload

    def stop(self) -> None:
        """Cancel the stale timer (shutdown and tests)."""
        with self._lock:
            self._cancel_timer()

    def _payload(self) -> dict:
        payload: dict = {"state": self._state}
        if self._state == MEASURING:
            payload["expected_s"] = self._expected_s
            payload["elapsed_s"] = round(max(0.0, self._clock() - (self._started or 0.0)), 2)
        return payload

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _restart_timer(self, state: str) -> None:
        self._cancel_timer()
        if state == OFFLINE:
            return
        timer = self._timer_factory(self._stale_after_s, self._expire)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _expire(self) -> None:
        with self._lock:
            if self._state == OFFLINE:
                return
            self._state = OFFLINE
            self._expected_s = None
            self._started = None
            self._timer = None
            payload = self._payload()
        self._on_change(payload)
