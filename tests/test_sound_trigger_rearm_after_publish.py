"""The radar is re-armed after a shot is published, not before it is processed.

Re-arming takes over half a second of fixed waits (a quiet line, then PA,
S#n, PA). It used to run inside the trigger wait, so every shot's first
numbers were held back by it. The radar sits idle after its dump and the next
swing is seconds away, so the re-arm can follow the shot instead of leading it.

What must not change:

- Clock sync still runs before the re-arm, while the radar is idle (the
  net-impact serial deadlock, see test_sound_trigger_serial_deadlock.py).
- The re-arm is the last thing done on the serial port before going back to
  reading.
- The radar is re-armed exactly once after every accepted capture, whatever
  becomes of it: processed, rejected as too slow, failed in processing, or
  failed in the shot callback. A radar left idle never fires again.
- Dumps rejected inside the wait (unparsable, no ball) re-arm at once.
"""

import json

import pytest
from spin_synth import synth_capture

from openflight.rolling_buffer import RollingBufferMonitor, monitor as monitor_module
from openflight.rolling_buffer.processor import RollingBufferProcessor
from openflight.rolling_buffer.trigger import SoundTrigger, TriggerStrategy


def _dump_response(i_samples, q_samples) -> str:
    return "\n".join(
        [
            '{"sample_time": "964.003"}',
            '{"trigger_time": "964.105"}',
            json.dumps({"I": [int(v) for v in i_samples]}),
            json.dumps({"Q": [int(v) for v in q_samples]}),
        ]
    )


def _swing(ball_speed_mph: float = 80.0) -> str:
    # amplitude=400 ADC counts clears the processor's magnitude gate.
    return _dump_response(*synth_capture(rpm=3000, ball_speed_mph=ball_speed_mph, amplitude=400.0))


def _silence() -> str:
    return _dump_response(*synth_capture(rpm=3000, amplitude=0.0, noise_rms=1.0))


class ScriptedRadar:
    """A radar that hands out scripted dumps and records every serial-touching call."""

    def __init__(self, *responses: str, events=None, on_exhausted=None):
        self.responses = list(responses)
        self.events = events if events is not None else []
        self.on_exhausted = on_exhausted
        self.last_clock_sync = None
        self.last_hardware_trigger_first_byte_timestamp = None
        self.rearm_error = None
        self.rearm_failures_left = 0

    def wait_for_hardware_trigger(self, timeout, cancel_event=None, on_first_byte=None):
        self.events.append("wait")
        if not self.responses:
            if self.on_exhausted is not None:
                self.on_exhausted()
            return ""
        if on_first_byte is not None:
            on_first_byte()
        return self.responses.pop(0)

    def rearm_rolling_buffer(self, pre_trigger_segments):
        self.events.append("rearm")
        if self.rearm_failures_left > 0:
            self.rearm_failures_left -= 1
            raise ConnectionError("port busy")
        if self.rearm_error is not None:
            raise self.rearm_error

    def read_clock_sync(self, samples=7, store=True, **kwargs):
        self.events.append("clock_sync")
        return {"clock_sync_method": "no_valid_reads", "valid_samples": 0}


# --- the trigger on its own ------------------------------------------------


def test_an_accepted_capture_is_handed_back_before_the_radar_is_rearmed():
    radar = ScriptedRadar(_swing())
    trigger = SoundTrigger()

    capture = trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1.0)

    assert capture is not None, "synthetic swing should be accepted"
    assert radar.events == ["wait", "clock_sync"], (
        f"the radar must not be re-armed before the capture is handed back; got {radar.events}"
    )


def test_finishing_the_capture_rearms_the_radar_after_the_clock_sync():
    radar = ScriptedRadar(_swing())
    trigger = SoundTrigger(pre_trigger_segments=12)
    trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1.0)

    trigger.finish_capture(radar)

    assert radar.events == ["wait", "clock_sync", "rearm"]


def test_finishing_twice_rearms_once():
    radar = ScriptedRadar(_swing())
    trigger = SoundTrigger()
    trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1.0)

    trigger.finish_capture(radar)
    trigger.finish_capture(radar)

    assert radar.events.count("rearm") == 1


def test_finishing_with_no_capture_outstanding_leaves_the_radar_alone():
    """After a timeout the radar is still armed: re-arming would discard its buffer."""
    radar = ScriptedRadar()
    trigger = SoundTrigger()
    assert trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1.0) is None

    trigger.finish_capture(radar)

    assert radar.events == ["wait"]


def test_a_capture_nobody_finished_is_rearmed_before_the_next_wait():
    """A caller that forgets to finish must not leave the radar idle for good."""
    radar = ScriptedRadar(_swing(), _swing())
    trigger = SoundTrigger()
    processor = RollingBufferProcessor()

    assert trigger.wait_for_trigger(radar, processor, timeout=1.0) is not None
    assert trigger.wait_for_trigger(radar, processor, timeout=1.0) is not None

    assert radar.events == ["wait", "clock_sync", "rearm", "wait", "clock_sync"]


def test_a_false_trigger_still_rearms_inside_the_wait_and_owes_nothing_after():
    radar = ScriptedRadar(_silence())
    trigger = SoundTrigger()

    assert trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1.0) is None
    trigger.finish_capture(radar)

    assert radar.events == ["wait", "rearm"], "one re-arm, at once, and no clock sync"


def test_an_unparsable_dump_still_rearms_inside_the_wait_and_owes_nothing_after():
    radar = ScriptedRadar('{"sample_time": "964.003"}\n{"I": [1, 2')
    trigger = SoundTrigger()

    assert trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1.0) is None
    trigger.finish_capture(radar)

    assert radar.events == ["wait", "rearm"]


def test_a_rearm_that_fails_is_tried_again_before_the_next_wait():
    radar = ScriptedRadar(_swing(), _swing())
    trigger = SoundTrigger()
    processor = RollingBufferProcessor()
    trigger.wait_for_trigger(radar, processor, timeout=1.0)

    radar.rearm_error = ConnectionError("port busy")
    with pytest.raises(ConnectionError):
        trigger.finish_capture(radar)
    radar.rearm_error = None
    trigger.wait_for_trigger(radar, processor, timeout=1.0)

    assert radar.events == ["wait", "clock_sync", "rearm", "rearm", "wait", "clock_sync"]


def test_other_trigger_strategies_have_nothing_to_finish():
    """The base strategy's finish is a no-op: speed and internal triggers re-arm themselves."""

    class Minimal(TriggerStrategy):
        def wait_for_trigger(self, radar, processor, timeout=30.0):
            return None

        def reset(self):
            pass

    radar = ScriptedRadar()
    Minimal().finish_capture(radar)

    assert radar.events == []


# --- the monitor's capture loop --------------------------------------------


def _monitor(radar: ScriptedRadar, monkeypatch) -> RollingBufferMonitor:
    monkeypatch.setattr(monitor_module, "get_session_logger", lambda: None)
    monkeypatch.setattr(monitor_module.time, "sleep", lambda _delay: None)
    monitor = RollingBufferMonitor(port=None, trigger_type="sound")
    monitor.radar = radar
    monitor._diagnostic_callback = None
    monitor._running = True
    radar.on_exhausted = lambda: setattr(monitor, "_running", False)
    return monitor


def test_the_shot_is_published_before_the_radar_is_rearmed(monkeypatch):
    events = []
    radar = ScriptedRadar(_swing(), events=events)
    monitor = _monitor(radar, monkeypatch)
    shots = []

    def publish(shot):
        events.append("shot")
        shots.append(shot)

    monitor._shot_callback = publish

    monitor._capture_loop()

    assert len(shots) == 1
    assert shots[0].ball_speed_mph == pytest.approx(80.0, abs=3.0)
    assert events == ["wait", "clock_sync", "shot", "rearm", "wait"], (
        "the re-arm follows the shot and is the last serial action before the next wait"
    )


def test_every_shot_of_a_session_is_followed_by_exactly_one_rearm(monkeypatch):
    events = []
    radar = ScriptedRadar(_swing(80.0), _swing(95.0), _swing(70.0), events=events)
    monitor = _monitor(radar, monkeypatch)
    monitor._shot_callback = lambda _shot: events.append("shot")

    monitor._capture_loop()

    assert events == ["wait", "clock_sync", "shot", "rearm"] * 3 + ["wait"]


def test_the_radar_is_rearmed_when_processing_raises(monkeypatch):
    radar = ScriptedRadar(_swing())
    monitor = _monitor(radar, monkeypatch)
    real_processor = monitor.processor

    class ExplodingProcessor:
        parse_capture = staticmethod(real_processor.parse_capture)
        process_standard = staticmethod(real_processor.process_standard)

        @staticmethod
        def process_capture(*_args, **_kwargs):
            raise RuntimeError("FFT failed")

    monitor.processor = ExplodingProcessor()

    monitor._capture_loop()

    assert radar.events == ["wait", "clock_sync", "rearm", "wait"]


def test_the_radar_is_rearmed_when_processing_finds_nothing(monkeypatch):
    radar = ScriptedRadar(_swing())
    monitor = _monitor(radar, monkeypatch)
    real_processor = monitor.processor

    class EmptyProcessor:
        parse_capture = staticmethod(real_processor.parse_capture)
        process_standard = staticmethod(real_processor.process_standard)

        @staticmethod
        def process_capture(*_args, **_kwargs):
            return None

    monitor.processor = EmptyProcessor()

    monitor._capture_loop()

    assert radar.events == ["wait", "clock_sync", "rearm", "wait"]


def test_the_radar_is_rearmed_when_the_shot_callback_raises(monkeypatch):
    """A broken UI or simulator hand-off must not cost the next shot."""
    radar = ScriptedRadar(_swing(), _swing())
    monitor = _monitor(radar, monkeypatch)
    published = []

    def publish(shot):
        published.append(shot)
        if len(published) == 1:
            raise RuntimeError("socket gone")

    monitor._shot_callback = publish

    monitor._capture_loop()

    assert len(published) == 2, "the shot after the failure is still captured"
    assert radar.events == ["wait", "clock_sync", "rearm", "wait", "clock_sync", "rearm", "wait"]


def test_the_radar_is_rearmed_when_the_monitor_is_stopped_mid_capture(monkeypatch):
    """Stopping must leave the radar armed, as it always has."""
    radar = ScriptedRadar(_swing())
    monitor = _monitor(radar, monkeypatch)
    published = []
    monitor._shot_callback = published.append
    real_wait = radar.wait_for_hardware_trigger

    def wait_then_stop(*args, **kwargs):
        response = real_wait(*args, **kwargs)
        monitor._running = False  # stop() arrived while the dump was coming in
        return response

    radar.wait_for_hardware_trigger = wait_then_stop

    monitor._capture_loop()

    assert published == [], "a capture that finishes after stop is not published"
    assert radar.events == ["wait", "clock_sync", "rearm"]


def test_a_failing_rearm_does_not_stop_the_capture_loop(monkeypatch):
    """The loop survives, the re-arm is tried again, and the next shot is caught."""
    radar = ScriptedRadar(_swing(), _swing())
    radar.rearm_failures_left = 1
    monitor = _monitor(radar, monkeypatch)
    published = []
    monitor._shot_callback = published.append

    monitor._capture_loop()

    assert len(published) == 2
    # shot 1: the re-arm fails after it, and is paid before the next wait
    assert radar.events == [
        "wait", "clock_sync", "rearm",
        "rearm", "wait", "clock_sync", "rearm",
        "wait",
    ]  # fmt: skip


def test_a_radar_that_cannot_be_rearmed_is_retried_not_abandoned(monkeypatch):
    """Every pass of the loop tries again; the radar is never waited on while idle."""
    radar = ScriptedRadar(_swing())
    radar.rearm_failures_left = 3
    monitor = _monitor(radar, monkeypatch)
    monitor._shot_callback = lambda _shot: None

    monitor._capture_loop()

    assert radar.events == ["wait", "clock_sync", "rearm", "rearm", "rearm", "rearm", "wait"]
