"""The front-panel status light: tells the golfer at the ball whether to swing.

One LED on a Pi GPIO pin (``--status-led-gpio``):

* solid on -- ready: a swing now is measured
* blinking (2 Hz) -- the last swing is still being measured; a swing now is not
* off -- not ready: server starting or stopped, radar not armed, or an error

Several parts of the server each know part of that answer: the OPS243 capture
loop, the IWR6843 dump, and in ``--mock`` the swing recorder. Each reports its
own state as a *source*, in the ``measuring_status`` vocabulary (offline,
ready, measuring), and the light shows the worst of them. So it is solid only
when every source is ready, and off until the first source says it is.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Mapping

from .gpio_factory import ensure_lgpio_pin_factory
from .measuring_status import MEASURING, OFFLINE, READY, STATES

logger = logging.getLogger(__name__)

# 2 Hz: a quarter second on, a quarter second off.
BLINK_ON_S = 0.25
BLINK_OFF_S = 0.25

# Free in every documented build, and pulled down at boot like every pin from
# BCM 9 up, so the LED stays dark until the server drives it. (BCM 0-8 boot
# pulled up, which can make an LED glow faintly before the server starts.)
SUGGESTED_PIN = 23
SUGGESTED_PIN_PHYSICAL = 16

# The 40-pin header carries BCM 0-27.
HEADER_PINS = range(28)

# Pins the project already wires, or that must not be driven by an LED.
RESERVED_PINS: Mapping[int, str] = {
    0: "the HAT ID EEPROM (ID_SD)",
    1: "the HAT ID EEPROM (ID_SC)",
    2: "I2C SDA (inclinometer, UPS fuel gauge)",
    3: "I2C SCL (inclinometer, UPS fuel gauge)",
    6: "the Geekworm UPS external-power detect",
    14: "the UART TX to the OPS243",
    15: "the UART RX from the OPS243",
    16: "the Geekworm UPS charging control",
    17: "the sound-trigger GATE input",
}

# Worst first: the light shows the first of these that any source reports.
_PRECEDENCE = (OFFLINE, MEASURING, READY)


def check_pin(pin: int, *, sound_trigger_pin: int = 17) -> None:
    """Refuse a pin that is off the header or already has a job.

    ``sound_trigger_pin`` is the configured ``--iwr6843-trigger-pin``, which a
    builder may have moved off BCM 17.

    Raises:
        ValueError: naming the pin, why it is taken, and a free pin to use.
    """
    suggestion = f"use a free pin such as {SUGGESTED_PIN} (physical pin {SUGGESTED_PIN_PHYSICAL})"
    if pin not in HEADER_PINS:
        raise ValueError(f"BCM{pin} is not on the 40-pin header (BCM 0-27); {suggestion}")
    reserved = dict(RESERVED_PINS)
    reserved.setdefault(sound_trigger_pin, "the sound-trigger GATE input (--iwr6843-trigger-pin)")
    if pin in reserved:
        raise ValueError(f"BCM{pin} is already used for {reserved[pin]}; {suggestion}")


class StatusLight:
    """Shows the worst state reported by any source on one LED.

    Thread-safe: the capture loop, the IWR6843 GPIO callback, Flask request
    threads and the recorder's stale timer all report. A state that is already
    showing is not written again, so a blink is never restarted mid-cycle, and
    an LED error is logged rather than raised into whoever reported.
    """

    def __init__(self, led, *, blink_on_s: float = BLINK_ON_S, blink_off_s: float = BLINK_OFF_S):
        """``led`` is a gpiozero ``LED`` (or a fake) created switched off."""
        self._led = led
        self._blink_on_s = blink_on_s
        self._blink_off_s = blink_off_s
        self._lock = threading.Lock()
        self._sources: dict[str, str] = {}
        self._shown = OFFLINE
        self._closed = False

    @property
    def state(self) -> str:
        """What the LED shows now: ``ready`` (on), ``measuring`` (blink) or ``offline`` (off)."""
        with self._lock:
            return self._shown

    def report(self, source: str, state: str) -> None:
        """Record ``source``'s state and update the LED; ignored once closed."""
        if state not in STATES:
            raise ValueError(f"state must be one of: {', '.join(STATES)}")
        with self._lock:
            if self._closed:
                return
            self._sources[source] = state
            self._show(self._combined())

    def close(self) -> None:
        """Switch the LED off and release its pin; later reports are ignored."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._show(OFFLINE)
            try:
                self._led.close()
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning("[STATUS LED] Could not release the LED pin", exc_info=True)

    def _combined(self) -> str:
        reported = set(self._sources.values())
        for state in _PRECEDENCE:
            if state in reported:
                return state
        return OFFLINE

    def _show(self, state: str) -> None:
        if state == self._shown:
            return
        try:
            if state == READY:
                self._led.on()
            elif state == MEASURING:
                self._led.blink(
                    on_time=self._blink_on_s, off_time=self._blink_off_s, background=True
                )
            else:
                self._led.off()
        except Exception:  # pylint: disable=broad-exception-caught
            # Leave _shown alone so the next report retries the write.
            logger.warning("[STATUS LED] Could not show %s", state, exc_info=True)
            return
        self._shown = state


def _gpiozero_led() -> Callable[..., object]:
    """gpiozero's LED class, after installing the Pi 5 pin factory (see gpio_factory)."""
    ensure_lgpio_pin_factory()
    from gpiozero import LED  # pylint: disable=import-outside-toplevel

    return LED


def create_status_light(
    pin: int, *, led_factory: Callable[..., object] | None = None
) -> StatusLight | None:
    """Claim ``pin`` for the status light, switched off.

    Returns None, after one warning, when the light cannot be driven: gpiozero
    or lgpio missing (a dev machine or CI), no GPIO chip, or the pin busy. The
    server then runs without a light; it never fails to start over one.
    Call ``check_pin`` first: this does not refuse reserved pins.
    """
    try:
        factory = led_factory if led_factory is not None else _gpiozero_led()
        led = factory(pin, active_high=True, initial_value=False)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "[STATUS LED] Cannot drive BCM%d (%s); running without the status light", pin, exc
        )
        return None
    logger.info("[STATUS LED] Status light on BCM%d", pin)
    return StatusLight(led)


__all__ = [
    "BLINK_OFF_S",
    "BLINK_ON_S",
    "RESERVED_PINS",
    "SUGGESTED_PIN",
    "StatusLight",
    "check_pin",
    "create_status_light",
]
