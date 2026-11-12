"""The front-panel status light: pin checks, LED states, and GPIO fallback.

No hardware: every LED here is a fake that records what gpiozero would do.
"""

from __future__ import annotations

import inspect
import logging
import sys
import types

import pytest

from openflight import status_led
from openflight.camera.capture_runtime import CameraCaptureSettings
from openflight.iwr6843.monitor import IWR6843CaptureMonitor
from openflight.measuring_status import MEASURING, OFFLINE, READY
from openflight.power.providers.geekworm import GeekwormPowerReader
from openflight.status_led import (
    BLINK_OFF_S,
    BLINK_ON_S,
    RESERVED_PINS,
    StatusLight,
    check_pin,
    create_status_light,
)


class FakeLed:
    """gpiozero ``LED`` double: records calls and what the pin would show."""

    def __init__(self, pin=None, *, active_high=True, initial_value=False):
        self.pin = pin
        self.active_high = active_high
        self.initial_value = initial_value
        self.calls: list = []
        self.closed = False
        self.fail_next: Exception | None = None

    def _record(self, call):
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        self.calls.append(call)

    def on(self):
        self._record("on")

    def off(self):
        self._record("off")

    def blink(self, on_time=1, off_time=1, n=None, background=True):
        self._record(("blink", on_time, off_time, n, background))

    def close(self):
        self.closed = True


def _light():
    led = FakeLed(23)
    return StatusLight(led), led


# -- pins ------------------------------------------------------------------------------


@pytest.mark.parametrize("pin", sorted(RESERVED_PINS))
def test_every_reserved_pin_is_refused_with_its_use(pin):
    with pytest.raises(ValueError) as refused:
        check_pin(pin)

    message = str(refused.value)
    assert f"BCM{pin} is already used for {RESERVED_PINS[pin]}" in message
    assert "23 (physical pin 16)" in message


def test_the_reserved_list_covers_every_pin_the_project_claims():
    """The I2C, UART, UPS and sound-trigger pins the code and docs wire."""
    assert {2, 3, 6, 14, 15, 16, 17} <= set(RESERVED_PINS)
    assert GeekwormPowerReader.AC_DETECT_PIN in RESERVED_PINS
    iwr_default = inspect.signature(IWR6843CaptureMonitor).parameters["gpio_pin"].default
    assert iwr_default in RESERVED_PINS
    assert CameraCaptureSettings().gpio_pin in RESERVED_PINS


@pytest.mark.parametrize("pin", [-1, 28, 40])
def test_pins_off_the_header_are_refused(pin):
    with pytest.raises(ValueError, match=r"not on the 40-pin header \(BCM 0-27\)"):
        check_pin(pin)


def test_a_moved_sound_trigger_pin_is_refused_too():
    with pytest.raises(ValueError, match="--iwr6843-trigger-pin"):
        check_pin(23, sound_trigger_pin=23)


def test_bcm17_stays_refused_when_the_trigger_pin_moves():
    """The documented builds wire the sound-trigger GATE to BCM17."""
    with pytest.raises(ValueError, match="sound-trigger GATE input"):
        check_pin(17, sound_trigger_pin=27)


@pytest.mark.parametrize("pin", [4, 5, 22, 23, 24, 27])
def test_free_pins_are_accepted(pin):
    check_pin(pin)


# -- LED states ------------------------------------------------------------------------


def test_the_light_starts_off_and_untouched():
    light, led = _light()

    assert light.state == OFFLINE
    assert led.calls == []


def test_ready_is_solid():
    light, led = _light()

    light.report("monitor", READY)

    assert light.state == READY
    assert led.calls == ["on"]


def test_measuring_blinks_at_two_hertz_in_the_background():
    light, led = _light()

    light.report("monitor", MEASURING)

    assert light.state == MEASURING
    assert led.calls == [("blink", BLINK_ON_S, BLINK_OFF_S, None, True)]
    assert 1.0 / (BLINK_ON_S + BLINK_OFF_S) == pytest.approx(2.0)


def test_offline_is_off():
    light, led = _light()
    light.report("monitor", READY)

    light.report("monitor", OFFLINE)

    assert light.state == OFFLINE
    assert led.calls == ["on", "off"]


def test_a_repeated_state_is_not_written_again():
    """gpiozero restarts a blink on every call, which would stutter the light."""
    light, led = _light()

    light.report("monitor", MEASURING)
    light.report("monitor", MEASURING)
    light.report("monitor", READY)
    light.report("monitor", READY)

    assert led.calls == [("blink", BLINK_ON_S, BLINK_OFF_S, None, True), "on"]


@pytest.mark.parametrize(
    ("reports", "shown"),
    [
        ({"monitor": READY}, READY),
        ({"monitor": MEASURING}, MEASURING),
        ({"monitor": OFFLINE}, OFFLINE),
        ({"monitor": READY, "iwr6843": READY}, READY),
        ({"monitor": READY, "iwr6843": MEASURING}, MEASURING),
        ({"monitor": MEASURING, "iwr6843": READY}, MEASURING),
        ({"monitor": READY, "iwr6843": OFFLINE}, OFFLINE),
        ({"monitor": MEASURING, "iwr6843": OFFLINE}, OFFLINE),
        ({"monitor": READY, "recorder": MEASURING}, MEASURING),
        ({"monitor": READY, "recorder": OFFLINE}, OFFLINE),
    ],
)
def test_the_worst_source_wins(reports, shown):
    light, _led = _light()

    for source, state in reports.items():
        light.report(source, state)

    assert light.state == shown


def test_the_light_returns_to_solid_when_the_last_busy_source_is_done():
    light, led = _light()
    light.report("monitor", READY)
    light.report("iwr6843", MEASURING)
    light.report("monitor", MEASURING)
    light.report("monitor", READY)

    assert light.state == MEASURING  # the TI dump is still in flight

    light.report("iwr6843", READY)

    assert light.state == READY
    assert led.calls == ["on", ("blink", BLINK_ON_S, BLINK_OFF_S, None, True), "on"]


def test_an_unknown_state_is_refused_and_changes_nothing():
    light, led = _light()

    with pytest.raises(ValueError, match="state must be one of"):
        light.report("monitor", "armed")

    assert light.state == OFFLINE
    assert led.calls == []


def test_an_led_error_is_logged_not_raised_and_retried(caplog):
    light, led = _light()
    led.fail_next = OSError("GPIO write failed")

    with caplog.at_level(logging.WARNING, logger="openflight.status_led"):
        light.report("monitor", READY)

    assert light.state == OFFLINE
    assert "Could not show ready" in caplog.text

    light.report("monitor", READY)

    assert light.state == READY
    assert led.calls == ["on"]


# -- shutdown --------------------------------------------------------------------------


def test_close_switches_the_light_off_and_releases_the_pin():
    light, led = _light()
    light.report("monitor", MEASURING)

    light.close()

    assert light.state == OFFLINE
    assert led.calls[-1] == "off"
    assert led.closed is True


def test_reports_after_close_are_ignored():
    """A capture thread finishing during shutdown must not relight the LED."""
    light, led = _light()
    light.report("monitor", READY)
    light.close()

    light.report("monitor", READY)
    light.report("iwr6843", MEASURING)

    assert light.state == OFFLINE
    assert led.calls == ["on", "off"]


def test_close_is_idempotent():
    light, led = _light()
    light.report("monitor", READY)

    light.close()
    light.close()

    assert led.calls == ["on", "off"]


def test_close_logs_a_release_failure(caplog):
    class StuckLed(FakeLed):
        def close(self):
            raise OSError("line busy")

    light = StatusLight(StuckLed(23))

    with caplog.at_level(logging.WARNING, logger="openflight.status_led"):
        light.close()

    assert "Could not release the LED pin" in caplog.text


# -- creation and the no-GPIO fallback ----------------------------------------------------


def test_create_claims_the_pin_switched_off():
    created = []

    def factory(pin, **kwargs):
        led = FakeLed(pin, **kwargs)
        created.append(led)
        return led

    light = create_status_light(23, led_factory=factory)

    assert isinstance(light, StatusLight)
    assert [(led.pin, led.active_high, led.initial_value) for led in created] == [(23, True, False)]
    assert light.state == OFFLINE


def test_the_pi5_pin_factory_is_installed_before_the_led(monkeypatch):
    """On a Pi 5 gpiozero's own factory detection fails; see gpio_factory."""
    events = []

    class RecordingLed(FakeLed):
        def __init__(self, *args, **kwargs):
            events.append("led")
            super().__init__(*args, **kwargs)

    fake_gpiozero = types.ModuleType("gpiozero")
    fake_gpiozero.LED = RecordingLed
    monkeypatch.setitem(sys.modules, "gpiozero", fake_gpiozero)
    monkeypatch.setattr(
        status_led, "ensure_lgpio_pin_factory", lambda: events.append("pin factory")
    )

    light = create_status_light(23)

    assert isinstance(light, StatusLight)
    assert events == ["pin factory", "led"]


def test_missing_gpiozero_warns_once_and_runs_without_a_light(monkeypatch, caplog):
    """A dev machine or CI without gpiozero/lgpio must still start the server."""
    monkeypatch.setitem(sys.modules, "gpiozero", None)
    monkeypatch.setitem(sys.modules, "gpiozero.pins.lgpio", None)

    with caplog.at_level(logging.WARNING, logger="openflight.status_led"):
        light = create_status_light(23)

    assert light is None
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "running without the status light" in warnings[0].getMessage()
    assert "GPIO support unavailable" in warnings[0].getMessage()


def test_a_busy_pin_warns_once_and_runs_without_a_light(caplog):
    def busy(_pin, **_kwargs):
        raise OSError("GPIO busy")

    with caplog.at_level(logging.WARNING, logger="openflight.status_led"):
        light = create_status_light(23, led_factory=busy)

    assert light is None
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "BCM23" in warnings[0].getMessage()
    assert "GPIO busy" in warnings[0].getMessage()
