"""The swing recorder's sound trigger: the SEN-14262 GATE on GPIO 17 starts each reading.

The camera trigger needs a lot of light and fires on anything moving in the
hitting zone; the sound sensor fires on the strike itself. These tests drive the
recorder's sound path with a fake GPIO input and a fake TI radar.
"""

import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from openflight.iwr6843.dump import pack_dump
from openflight.iwr6843.monitor import IWR6843CaptureMonitor

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "hardware-test" / "camera_motion_capture.py"
)
spec = importlib.util.spec_from_file_location("camera_motion_capture_sound", SCRIPT)
recorder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recorder)


class FakeGate:
    """A gpiozero Button on the sound sensor's GATE line."""

    instances: list["FakeGate"] = []

    def __init__(self, pin, pull_up, bounce_time, *, high=False):
        self.pin = pin
        self.pull_up = pull_up
        self.bounce_time = bounce_time
        self.when_pressed = None
        self.is_pressed = high
        self.closed = False
        FakeGate.instances.append(self)

    def strike(self):
        """A rising edge, as the impact sound produces."""
        if self.when_pressed is not None:
            self.when_pressed()

    def close(self):
        self.closed = True


class FakeRadar:
    port = "/dev/fake-iwr6843"

    def __init__(self):
        cube = np.zeros((2, 4, 4, 8), dtype=complex)
        self.raw = pack_dump(cube, n_tx=2, version=3, frame_period_us=6000)
        self.reads = 0

    def send_config(self, path):
        pass

    def read_dump(self):
        self.reads += 1
        time.sleep(0.05)
        return self.raw

    def stop_sensor(self):
        pass

    def close(self):
        pass


class FakeRuntime:
    def __init__(self, accept=True):
        self.accept = accept
        self.triggers = []

    def notify_trigger(self, timestamp=None):
        self.triggers.append(timestamp)
        return self.accept


class FakeKiosk:
    def __init__(self):
        self.reports = []

    def measuring(self):
        self.reports.append("measuring")


def _worker(runtime, *, motion_triggers, on_trigger=None, gate_state=None, tmp_path):
    trigger = SimpleNamespace(settings=SimpleNamespace(min_fraction=0.008))
    return recorder.TriggerWorker(
        runtime,
        trigger,
        roi=(0.2, 0.45, 0.6, 0.45),
        outdir=tmp_path,
        calibrate=True,
        on_trigger=on_trigger,
        motion_triggers=motion_triggers,
        gate_state=gate_state,
    )


def _sample(triggered=True):
    return SimpleNamespace(
        triggered=triggered, fraction=0.03, threshold=18, diff_noise=3.0, armed=True
    )


# --- options -----------------------------------------------------------------


def test_the_camera_still_triggers_by_default():
    args = recorder.parse_args(["--iwr6843"])
    assert args.trigger == "camera"
    assert args.sound_pin == 17


def test_sound_trigger_is_chosen_with_its_pin():
    args = recorder.parse_args(["--iwr6843", "--trigger", "sound", "--sound-pin", "27"])
    assert args.trigger == "sound"
    assert args.sound_pin == 27


def test_sound_trigger_needs_the_radar():
    with pytest.raises(SystemExit):
        recorder.parse_args(["--trigger", "sound"])


# --- the camera never triggers in sound mode ---------------------------------


def test_camera_motion_starts_nothing_in_sound_mode(tmp_path):
    runtime = FakeRuntime()
    fired = []
    worker = _worker(runtime, motion_triggers=False, on_trigger=fired.append, tmp_path=tmp_path)
    worker.handle_sample(_sample(triggered=True))
    assert fired == [] and runtime.triggers == [] and worker.triggers == 0


def test_camera_motion_still_triggers_in_camera_mode(tmp_path, capsys):
    runtime = FakeRuntime()
    fired = []
    worker = _worker(runtime, motion_triggers=True, on_trigger=fired.append, tmp_path=tmp_path)
    worker.handle_sample(_sample(triggered=True))
    assert len(fired) == 1 and len(runtime.triggers) == 1 and worker.triggers == 1
    assert "3.0% of the zone changed" in capsys.readouterr().out


def test_a_sound_edge_saves_the_camera_clip_around_it(tmp_path, capsys):
    runtime = FakeRuntime()
    worker = _worker(runtime, motion_triggers=False, tmp_path=tmp_path)
    worker.sound_trigger(1234.5)
    assert runtime.triggers == [1234.5]
    assert worker.triggers == 1
    assert "TRIGGER #1: sound" in capsys.readouterr().out


def test_a_busy_camera_costs_the_clip_not_the_reading(tmp_path, capsys):
    worker = _worker(FakeRuntime(accept=False), motion_triggers=False, tmp_path=tmp_path)
    worker.sound_trigger(1.0)
    assert worker.triggers == 1
    assert "camera busy, no clip" in capsys.readouterr().out


def test_the_stats_line_says_sound_and_flags_a_stuck_gate(tmp_path):
    worker = _worker(
        FakeRuntime(), motion_triggers=False, gate_state=lambda: True, tmp_path=tmp_path
    )
    line = worker.stats_line(230.0, 0.004, _sample(triggered=False))
    assert "(sound triggers)" in line and "fires at" not in line
    assert "GATE stuck high" in line
    worker.gate_state = lambda: False
    assert "GATE" not in worker.stats_line(230.0, 0.004, _sample(triggered=False))


# --- the observer the radar calls on each accepted edge -----------------------


def test_each_accepted_edge_shows_measuring_and_saves_a_clip(tmp_path):
    runtime = FakeRuntime()
    kiosk = FakeKiosk()
    holder = {"worker": _worker(runtime, motion_triggers=False, tmp_path=tmp_path)}
    recorder.sound_trigger(holder, kiosk)(99.0)
    assert kiosk.reports == ["measuring"]
    assert runtime.triggers == [99.0]


def test_an_edge_before_the_camera_is_up_still_shows_measuring():
    kiosk = FakeKiosk()
    recorder.sound_trigger({}, kiosk)(1.0)
    assert kiosk.reports == ["measuring"]


# --- start_radar picks the input ----------------------------------------------


@pytest.fixture
def captured_monitor(monkeypatch):
    import openflight.iwr6843.monitor as monitor_module

    made = {}

    class Recording:
        port = "/dev/fake"

        def __init__(self, **kwargs):
            made.update(kwargs)

        def start(self, armed=True):
            made["armed"] = armed

    monkeypatch.setattr(monitor_module, "IWR6843CaptureMonitor", Recording)
    return made


def test_sound_mode_opens_the_real_gpio_input(captured_monitor, tmp_path):
    args = recorder.parse_args(["--iwr6843", "--trigger", "sound"])
    observer = object()
    recorder.start_radar(args, tmp_path, trigger_observers=[observer])
    assert captured_monitor["button_factory"] is None
    assert captured_monitor["gpio_pin"] == 17
    assert captured_monitor["trigger_observers"] == [observer]
    assert captured_monitor["armed"] is True


def test_camera_mode_keeps_the_stand_in_input(captured_monitor, tmp_path):
    args = recorder.parse_args(["--iwr6843"])
    recorder.start_radar(args, tmp_path, trigger_observers=[object()])
    assert captured_monitor["button_factory"] is recorder.NoTriggerInput
    assert captured_monitor["trigger_observers"] is None


def test_a_missing_gpio_library_says_so_plainly(monkeypatch, tmp_path):
    import openflight.iwr6843.monitor as monitor_module

    class Broken:
        port = "/dev/fake"

        def __init__(self, **kwargs):
            pass

        def start(self, armed=True):
            raise ImportError("No module named 'lgpio'")

    monkeypatch.setattr(monitor_module, "IWR6843CaptureMonitor", Broken)
    args = recorder.parse_args(["--iwr6843", "--trigger", "sound"])
    with pytest.raises(SystemExit, match="Sound trigger unavailable on GPIO 17"):
        recorder.start_radar(args, tmp_path)


# --- end to end with the real monitor -----------------------------------------


def _config(tmp_path):
    config = tmp_path / "ti.cfg"
    config.write_text("sensorStop\n")
    return config


def test_a_strike_freezes_the_radar_once_and_ignores_the_net_echo(tmp_path):
    FakeGate.instances.clear()
    radar = FakeRadar()
    runtime = FakeRuntime()
    kiosk = FakeKiosk()
    holder = {"worker": _worker(runtime, motion_triggers=False, tmp_path=tmp_path)}
    monitor = IWR6843CaptureMonitor(
        config_path=_config(tmp_path),
        output_dir=tmp_path / "dumps",
        radar=radar,
        button_factory=FakeGate,
        trigger_observers=[recorder.sound_trigger(holder, kiosk)],
    )
    monitor.start(armed=True)
    try:
        gate = FakeGate.instances[-1]
        assert gate.pin == 17 and gate.pull_up is False and gate.bounce_time is None
        gate.strike()  # club on ball
        gate.strike()  # ringing, then the ball hitting the net
        capture = monitor.capture_for_shot(None, timeout_s=2.0)
        assert capture is not None and capture.valid
        time.sleep(0.15)
        assert monitor.capture_for_shot(None, timeout_s=0.2) is None
        assert radar.reads == 1
        assert kiosk.reports == ["measuring"]
        assert len(runtime.triggers) == 1
        # After the reading, the next strike is taken.
        time.sleep(0.12)
        gate.strike()
        assert monitor.capture_for_shot(None, timeout_s=2.0) is not None
        assert radar.reads == 2 and len(runtime.triggers) == 2
    finally:
        monitor.stop()


def test_the_monitor_reports_a_gate_stuck_high(tmp_path):
    monitor = IWR6843CaptureMonitor(
        config_path=_config(tmp_path),
        output_dir=tmp_path / "dumps",
        radar=FakeRadar(),
        button_factory=lambda pin, pull_up, bounce_time: FakeGate(
            pin, pull_up, bounce_time, high=True
        ),
    )
    monitor.start(armed=True)
    try:
        assert monitor.trigger_input_active is True
    finally:
        monitor.stop()


def test_the_stand_in_input_has_no_line_to_read(tmp_path):
    monitor = IWR6843CaptureMonitor(
        config_path=_config(tmp_path),
        output_dir=tmp_path / "dumps",
        radar=FakeRadar(),
        button_factory=recorder.NoTriggerInput,
    )
    monitor.start(armed=True)
    try:
        assert monitor.trigger_input_active is None
    finally:
        monitor.stop()
