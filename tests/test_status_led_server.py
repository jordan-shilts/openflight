"""How the server feeds the front-panel status light: flag, sources, recorder, shutdown."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from openflight import server as server_module
from openflight.iwr6843 import Calibration
from openflight.measuring_status import MEASURING, OFFLINE, READY, MeasuringStatus
from openflight.status_led import StatusLight
from tests.test_measuring_status import FakeTimer
from tests.test_status_led import FakeLed

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def light(monkeypatch):
    """A fitted light on a fake LED, and the socket events the server sends."""
    led = FakeLed(23)
    fitted = StatusLight(led)
    monkeypatch.setattr(server_module, "status_light", fitted)
    monkeypatch.setattr(server_module.socketio, "emit", lambda *_args, **_kwargs: None)
    return fitted, led


# -- the flag ---------------------------------------------------------------------------


def _stub_startup(monkeypatch, *argv):
    """Stub what main() starts after parsing ``argv``; returns a record of serving."""
    served = []
    monkeypatch.setattr(sys, "argv", ["openflight-server", *argv])
    monkeypatch.setattr(server_module, "status_light", None)
    monkeypatch.setattr(server_module, "start_monitor", lambda **_kwargs: None)
    monkeypatch.setattr(server_module, "load_sim_config", lambda: [])
    monkeypatch.setattr(server_module, "build_connectors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(server_module, "init_session_logger", lambda **_kwargs: None)
    monkeypatch.setattr(server_module, "_cleanup_hardware_for_shutdown", lambda: None)
    monkeypatch.setattr(
        server_module.socketio, "run", lambda *_args, **_kwargs: served.append(True)
    )
    return served


def _run_main(monkeypatch, *argv):
    """Run main() up to the web server, recording the pins a light was created on."""
    created = []

    def create(pin):
        created.append(pin)
        return StatusLight(FakeLed(pin))

    _stub_startup(monkeypatch, *argv)
    monkeypatch.setattr(server_module, "create_status_light", create)
    server_module.main()
    return created


def test_the_flag_claims_a_light_on_that_pin(monkeypatch):
    created = _run_main(monkeypatch, "--mock", "--no-logging", "--status-led-gpio", "23")

    assert created == [23]
    assert isinstance(server_module.status_light, StatusLight)


def test_without_the_flag_there_is_no_light(monkeypatch):
    created = _run_main(monkeypatch, "--mock", "--no-logging")

    assert created == []
    assert server_module.status_light is None


@pytest.mark.parametrize(
    ("pin", "reason"),
    [
        ("17", "BCM17 is already used for the sound-trigger GATE input"),
        ("14", "BCM14 is already used for the UART TX to the OPS243"),
        ("15", "BCM15 is already used for the UART RX from the OPS243"),
        ("2", "BCM2 is already used for I2C SDA"),
        ("3", "BCM3 is already used for I2C SCL"),
        ("6", "BCM6 is already used for the Geekworm UPS external-power detect"),
        ("16", "BCM16 is already used for the Geekworm UPS charging control"),
        ("28", "BCM28 is not on the 40-pin header"),
    ],
)
def test_a_taken_pin_is_a_clear_startup_error(monkeypatch, capsys, pin, reason):
    """argparse's exit 2 and a message, before any GPIO is touched -- not a traceback."""
    with pytest.raises(SystemExit) as exit_info:
        _run_main(monkeypatch, "--mock", "--no-logging", "--status-led-gpio", pin)

    assert exit_info.value.code == 2
    error = capsys.readouterr().err
    assert f"--status-led-gpio: {reason}" in error
    assert "physical pin 16" in error


def test_the_configured_sound_trigger_pin_is_refused(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exit_info:
        _run_main(
            monkeypatch,
            "--mock",
            "--no-logging",
            "--iwr6843-trigger-pin",
            "23",
            "--status-led-gpio",
            "23",
        )

    assert exit_info.value.code == 2
    assert "--iwr6843-trigger-pin" in capsys.readouterr().err


def test_a_pin_must_be_a_number(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exit_info:
        _run_main(monkeypatch, "--mock", "--status-led-gpio", "GPIO23")

    assert exit_info.value.code == 2
    assert "--status-led-gpio: invalid int value" in capsys.readouterr().err


def test_no_gpio_on_this_machine_still_starts_the_server(monkeypatch, capsys):
    """The real factory, with gpiozero missing: one warning, then on without a light."""
    monkeypatch.setitem(sys.modules, "gpiozero", None)
    monkeypatch.setitem(sys.modules, "gpiozero.pins.lgpio", None)
    served = _stub_startup(monkeypatch, "--mock", "--no-logging", "--status-led-gpio", "23")

    server_module.main()

    assert served == [True]
    assert server_module.status_light is None
    assert "running without it" in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("bash") is None, reason="start-kiosk.sh needs bash")
def test_start_kiosk_hands_the_flag_to_the_server(monkeypatch):
    """`scripts/start-kiosk.sh --status-led-gpio 23` reaches the server's parser."""
    result = subprocess.run(
        ["bash", "scripts/start-kiosk.sh", "--mock", "--status-led-gpio", "23", "--dry-run"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    command = shlex.split(result.stdout)
    assert command[0] == "openflight-server"

    created = _run_main(monkeypatch, *command[1:], "--no-logging")

    assert created == [23]


# -- what each run path reports -----------------------------------------------------------


def test_the_production_monitor_reports_its_own_readiness(monkeypatch, light):
    fitted, _led = light
    started = {}

    class FakeRollingBufferMonitor:
        def __init__(self, **_kwargs):
            pass

        def connect(self):
            return True

        def start(self, **kwargs):
            started.update(kwargs)

        def stop(self):
            return None

        def disconnect(self):
            return None

    monkeypatch.setattr("openflight.rolling_buffer.RollingBufferMonitor", FakeRollingBufferMonitor)
    monkeypatch.setattr(server_module, "monitor", None)
    monkeypatch.setattr(server_module, "iwr6843_runtime", None)
    monkeypatch.setattr(server_module, "get_session_logger", lambda: None)

    server_module.start_monitor(port="/dev/ops", trigger_type="sound")

    assert started["readiness_callback"] is server_module.on_capture_readiness
    assert fitted.state == OFFLINE  # nothing claimed ready on the monitor's behalf

    started["readiness_callback"](READY)
    assert fitted.state == READY
    started["readiness_callback"](MEASURING)
    assert fitted.state == MEASURING

    server_module.stop_monitor()
    assert fitted.state == OFFLINE


@pytest.mark.parametrize("swing_speed_mode", [False, True])
def test_a_mock_kiosk_is_solid_until_stopped(monkeypatch, light, swing_speed_mode):
    """Simulate shot is always available, so plain --mock shows ready."""
    fitted, led = light
    monkeypatch.setattr(server_module, "monitor", None)
    monkeypatch.setattr(server_module, "get_session_logger", lambda: None)

    server_module.start_monitor(mock=True, swing_speed_mode=swing_speed_mode)
    assert fitted.state == READY
    assert led.calls == ["on"]

    server_module.stop_monitor()
    assert fitted.state == OFFLINE


def test_the_iwr6843_holds_the_light_dark_until_armed(monkeypatch, tmp_path, light):
    """OPS ready is not enough while the TI radar is not yet taking edges."""
    fitted, _led = light
    captured = {}

    class FakeCaptureMonitor:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.port = "/dev/ttyUSB0"

        def start(self, *, armed=True):
            captured["status_callback"](READY if armed else OFFLINE)

        def stop(self):
            return None

    monkeypatch.setattr(
        server_module, "PHONE_ORIENTATION_CALIBRATION_PATH", tmp_path / "missing.json"
    )
    monkeypatch.setattr(Calibration, "load", lambda _path: Calibration.identity())
    monkeypatch.setattr("openflight.iwr6843.monitor.IWR6843CaptureMonitor", FakeCaptureMonitor)
    monkeypatch.setattr("openflight.iwr6843.monitor.tx_order_from_config", lambda _path: "normal")
    monkeypatch.setattr(server_module, "iwr6843_runtime", None)
    monkeypatch.setattr(server_module, "iwr6843_runtime_config", {"enabled": False})

    assert server_module.init_iwr6843(
        port="/dev/ttyUSB0",
        config_path="snapshot.cfg",
        calibration_path="cal.json",
        output_dir=tmp_path,
        trigger_pin=17,
        tee_range_m=1.575,
        net_range_m=4.6,
        tx_order="auto",
        capture_timeout_s=12.0,
    )

    assert captured["status_callback"] is server_module.on_iwr6843_status
    server_module.on_capture_readiness(READY)
    assert fitted.state == OFFLINE

    server_module.on_iwr6843_status(READY)
    assert fitted.state == READY
    server_module.on_iwr6843_status(MEASURING)  # the ~7 s TI dump
    assert fitted.state == MEASURING


# -- the swing recorder feeding a --mock kiosk ---------------------------------------------


@pytest.fixture
def recorder_kiosk(monkeypatch, light):
    """A --mock kiosk whose recorder status goes stale only when a test says so."""
    FakeTimer.created = []
    tracker = MeasuringStatus(server_module._on_measuring_status_change, timer_factory=FakeTimer)
    monkeypatch.setattr(server_module, "measuring_status", tracker)
    monkeypatch.setattr(server_module, "monitor", None)
    monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
    server_module.start_monitor(mock=True)
    yield light
    server_module.stop_monitor()


def _post(payload):
    response = server_module.app.test_client().post(
        "/api/shots/measuring", json=payload, environ_base={"REMOTE_ADDR": "127.0.0.1"}
    )
    assert response.status_code == 200
    return response


def test_the_recorder_drives_the_light(recorder_kiosk):
    fitted, led = recorder_kiosk
    assert fitted.state == READY  # no recorder yet: Simulate shot is available

    _post({"state": "ready"})
    assert fitted.state == READY
    _post({"state": "measuring", "expected_s": 7.5})
    assert fitted.state == MEASURING
    _post({"state": "ready", "result": "shot"})
    assert fitted.state == READY
    _post({"state": "offline"})
    assert fitted.state == OFFLINE

    assert led.calls == ["on", ("blink", 0.25, 0.25, None, True), "on", "off"]


def test_a_recorder_that_goes_quiet_turns_the_light_off(recorder_kiosk):
    fitted, _led = recorder_kiosk
    _post({"state": "ready"})

    FakeTimer.created[-1].function()  # 30 s without a heartbeat

    assert fitted.state == OFFLINE


def test_a_reconnecting_screen_does_not_touch_the_light(recorder_kiosk):
    """A new client gets the recorder's snapshot (offline here); the light must not."""
    fitted, _led = recorder_kiosk

    server_module.handle_connect()

    assert fitted.state == READY


def test_the_server_tracker_is_wired_to_the_light(monkeypatch, light):
    """The module's own tracker (not a test double) reaches the light."""
    fitted, _led = light
    monkeypatch.setattr(server_module, "monitor", server_module.MockLaunchMonitor())
    try:
        _post({"state": "measuring", "expected_s": 7.5})
        assert fitted.state == MEASURING
    finally:
        _post({"state": "offline"})  # back to how the module started; cancels its timer
        server_module.measuring_status.stop()


# -- shutdown ---------------------------------------------------------------------------


def test_shutdown_turns_the_light_off_first(monkeypatch, light):
    fitted, led = light
    fitted.report(server_module.STATUS_SOURCE_MONITOR, READY)
    seen_by_monitor_stop = []
    monkeypatch.setattr(server_module, "shutdown_cleanup_started", False)
    monkeypatch.setattr(
        server_module, "stop_monitor", lambda: seen_by_monitor_stop.append(led.closed)
    )
    monkeypatch.setattr(server_module, "sim_connectors", [])

    assert server_module._cleanup_hardware_for_shutdown() is True

    assert seen_by_monitor_stop == [True]
    assert led.calls == ["on", "off"]
    assert fitted.state == OFFLINE


def test_shutdown_without_a_light_is_unchanged(monkeypatch):
    stopped = []
    monkeypatch.setattr(server_module, "status_light", None)
    monkeypatch.setattr(server_module, "shutdown_cleanup_started", False)
    monkeypatch.setattr(server_module, "stop_monitor", lambda: stopped.append(True))
    monkeypatch.setattr(server_module, "sim_connectors", [])

    assert server_module._cleanup_hardware_for_shutdown() is True
    assert stopped == [True]
