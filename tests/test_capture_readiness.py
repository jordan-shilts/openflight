"""Readiness reports that drive the status light, from the OPS243 and IWR6843 monitors.

The light is solid only while a swing now would be measured. These tests pin
the transitions each capture path reports: listening to an armed radar
(ready), a dump or its processing in flight (measuring), and stopped, faulted
or unarmed (offline).
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openflight.iwr6843.monitor import IWR6843CaptureMonitor
from openflight.measuring_status import MEASURING, OFFLINE, READY
from openflight.rolling_buffer import RollingBufferMonitor, monitor as monitor_module
from openflight.rolling_buffer.types import IQCapture

_CAPTURE = IQCapture(
    sample_time=0.0,
    trigger_time=0.068,
    i_samples=[2048] * 16,
    q_samples=[2048] * 16,
)


class ScriptedTrigger:
    """Plays one scripted step per wait, then stops the capture loop."""

    def __init__(self, monitor, steps):
        self.monitor = monitor
        self.steps = list(steps)

    def wait_for_trigger(self, **kwargs):
        if not self.steps:
            self.monitor._running = False
            return None
        return self.steps.pop(0)(kwargs)

    @staticmethod
    def drain_diagnostics():
        return []

    @staticmethod
    def reset():
        return None


def timeout(_kwargs):
    """No swing within the wait."""
    return None


def dump(*, rearmed=None, radar=None):
    """A swing: the first byte arrives, then the trigger re-arms (or fails to)."""

    def step(kwargs):
        if "capture_started_callback" in kwargs:
            kwargs["capture_started_callback"]()
        if rearmed is not None:
            radar.rolling_buffer_armed = rearmed
        return _CAPTURE

    return step


def radar_error(_kwargs):
    """The serial link fails mid-wait, as when the USB cable is pulled."""
    raise OSError("device reports readiness to read but returned no data")


def _monitor(monkeypatch, *, trigger_type="sound", armed=True, processor=None):
    monitor = RollingBufferMonitor(port=None, trigger_type=trigger_type)
    monitor.radar = SimpleNamespace(rolling_buffer_armed=armed)
    monitor.processor = processor or MagicMock(process_capture=MagicMock(return_value=None))
    monkeypatch.setattr(monitor_module, "get_session_logger", lambda: None)
    monkeypatch.setattr(monitor_module, "log_session_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(monitor_module.time, "sleep", lambda _seconds: None)
    events = []
    monitor._readiness_callback = lambda state: events.append(("light", state))
    monitor._processing_callback = lambda state: events.append(("ui", state))
    monitor._diagnostic_callback = None
    return monitor, events


def _run(monitor, *steps):
    monitor.trigger = ScriptedTrigger(monitor, steps)
    monitor._running = True
    monitor._capture_loop()


def _light(events):
    return [state for kind, state in events if kind == "light"]


class TestRollingBufferReadiness:
    def test_a_swing_blinks_from_the_first_byte_until_the_loop_listens_again(self, monkeypatch):
        monitor, events = _monitor(monkeypatch)

        _run(monitor, dump())

        assert events == [
            ("light", READY),
            ("light", MEASURING),
            ("ui", "capturing"),
            ("light", MEASURING),
            ("ui", "calculating"),
            ("ui", "failed"),
            ("light", READY),
        ]

    def test_an_idle_wait_stays_ready(self, monkeypatch):
        monitor, events = _monitor(monkeypatch)

        _run(monitor, timeout, timeout)

        assert _light(events) == [READY, READY, READY]

    def test_a_failed_rearm_turns_the_light_off_after_the_swing(self, monkeypatch):
        """The re-arm write timed out: the board may sit idle, so do not invite a swing."""
        monitor, events = _monitor(monkeypatch)

        _run(monitor, dump(rearmed=False, radar=monitor.radar), timeout)

        assert _light(events) == [READY, MEASURING, MEASURING, OFFLINE, OFFLINE]

    def test_the_next_successful_rearm_relights_it(self, monkeypatch):
        """A busy radar's pending dump arrives, and its re-arm goes through."""
        monitor, events = _monitor(monkeypatch, armed=False)

        _run(monitor, dump(rearmed=True, radar=monitor.radar))

        assert _light(events) == [OFFLINE, MEASURING, MEASURING, READY]

    def test_a_radar_error_stays_dark_until_a_wait_returns(self, monkeypatch):
        """No flash of ready on each one-second retry while the radar is unplugged."""
        monitor, events = _monitor(monkeypatch)

        _run(monitor, radar_error, radar_error, timeout)

        assert _light(events) == [READY, OFFLINE, OFFLINE, OFFLINE, OFFLINE, READY]

    def test_a_processing_error_does_not_blame_the_radar(self, monkeypatch):
        """The board was re-armed before processing, so the next wait is ready."""
        processor = MagicMock(process_capture=MagicMock(side_effect=RuntimeError("FFT failed")))
        monitor, events = _monitor(monkeypatch, processor=processor)

        _run(monitor, dump())

        assert _light(events) == [READY, MEASURING, MEASURING, OFFLINE, READY]

    def test_the_speed_trigger_ignores_the_rolling_buffer_flag(self, monkeypatch):
        """It arms the board inside its own wait, from speed-streaming mode."""
        monitor, events = _monitor(monkeypatch, trigger_type="speed", armed=False)

        _run(monitor, timeout)

        assert _light(events) == [READY, READY]

    def test_the_internal_hardware_trigger_follows_the_flag(self, monkeypatch):
        monitor, events = _monitor(monkeypatch, trigger_type="hardware", armed=False)

        _run(monitor, timeout)

        assert _light(events) == [OFFLINE, OFFLINE]

    def test_a_failing_readiness_callback_never_breaks_capture(self, monkeypatch, caplog):
        monitor, events = _monitor(monkeypatch)

        def broken(_state):
            raise RuntimeError("light unplugged")

        monitor._readiness_callback = broken

        _run(monitor, dump())

        assert [state for kind, state in events if kind == "ui"] == [
            "capturing",
            "calculating",
            "failed",
        ]
        assert "Readiness callback failed" in caplog.text

    def test_stop_goes_dark_and_silences_late_reports(self, monkeypatch):
        """A dump allowed to finish during shutdown must not relight the LED."""
        monitor, events = _monitor(monkeypatch)
        monitor._running = True

        monitor.stop()
        monitor._report_readiness(MEASURING)
        monitor._report_readiness(READY)

        assert _light(events) == [OFFLINE]

    def test_start_passes_readiness_to_the_capture_thread(self, monkeypatch):
        monitor, _events = _monitor(monkeypatch)
        monitor._radar_fault = True  # left over from before a reinitialisation
        reported = []
        done = threading.Event()

        def readiness(state):
            reported.append(state)
            done.set()

        monitor.trigger = ScriptedTrigger(monitor, [])
        monitor.start(readiness_callback=readiness)
        assert done.wait(timeout=2.0)
        monitor.stop()

        assert reported == [READY, OFFLINE]


# -- IWR6843 ------------------------------------------------------------------------------


class FakeTiRadar:
    """TI transport double whose dump blocks until released."""

    port = "/dev/fake-iwr6843"

    def __init__(self):
        self.release = threading.Event()

    def send_config(self, _path):
        return None

    def read_dump(self):
        self.release.wait(timeout=2.0)
        raise ValueError("short IWR6843 dump: 0 bytes")

    def stop_sensor(self):
        return None

    def close(self):
        return None


class FakeButton:
    def __init__(self, pin, pull_up, bounce_time):
        self.pin = pin
        self.pull_up = pull_up
        self.bounce_time = bounce_time
        self.when_pressed = None

    def close(self):
        return None


@pytest.fixture
def ti_monitor(tmp_path):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    radar = FakeTiRadar()
    reported = []
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=radar,
        button_factory=FakeButton,
        status_callback=reported.append,
    )
    yield monitor, radar, reported
    radar.release.set()
    monitor.stop()


def _wait_for(predicate, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestIwr6843Readiness:
    def test_unarmed_start_holds_the_light_dark_until_armed(self, ti_monitor):
        """OPS startup can pulse the shared gate, so TI waits for arm()."""
        monitor, _radar, reported = ti_monitor

        monitor.start(armed=False)
        assert reported == [OFFLINE]

        monitor.arm()
        assert reported == [OFFLINE, READY]

    def test_an_accepted_edge_blinks_until_its_dump_completes(self, ti_monitor):
        """A swing during the ~7 s TI dump would get no launch angle."""
        monitor, radar, reported = ti_monitor
        monitor.start()

        assert monitor.notify_trigger(time.time())
        assert reported == [READY, MEASURING]
        assert monitor.notify_trigger(time.time() + 1.0) is False  # busy: no new report

        radar.release.set()
        assert _wait_for(lambda: reported[-1] == READY)
        assert reported == [READY, MEASURING, READY]

    def test_stop_goes_dark(self, ti_monitor):
        monitor, _radar, reported = ti_monitor
        monitor.start()

        monitor.stop()

        assert reported == [READY, OFFLINE]
        assert monitor.notify_trigger(time.time()) is False
        assert reported == [READY, OFFLINE]

    def test_a_dump_finishing_after_stop_does_not_relight(self, ti_monitor):
        monitor, radar, reported = ti_monitor
        monitor.start()
        assert monitor.notify_trigger(time.time())

        stopper = threading.Thread(target=monitor.stop)
        stopper.start()
        assert _wait_for(lambda: OFFLINE in reported)
        radar.release.set()
        stopper.join(timeout=5.0)

        assert reported == [READY, MEASURING, OFFLINE]

    def test_a_failing_status_callback_never_breaks_capture(self, tmp_path, caplog):
        config = tmp_path / "radar.cfg"
        config.write_text("sensorStart\n", encoding="utf-8")
        radar = FakeTiRadar()
        radar.release.set()

        def broken(_state):
            raise RuntimeError("light unplugged")

        monitor = IWR6843CaptureMonitor(
            config_path=config,
            output_dir=tmp_path / "dumps",
            radar=radar,
            button_factory=FakeButton,
            status_callback=broken,
        )
        monitor.start()
        try:
            edge = time.time()
            assert monitor.notify_trigger(edge)
            capture = monitor.capture_for_shot(edge, timeout_s=2.0)
        finally:
            monitor.stop()

        assert capture is not None
        assert "Status callback failed" in caplog.text
