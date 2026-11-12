# Status Light

*Optional.* One green LED on the front of the case tells the golfer at the ball
whether to swing, without looking at the screen.

| Light | Meaning |
|-------|---------|
| **Solid** | Ready. A swing now is measured. |
| **Blinking** (twice a second) | Still measuring the last swing. Wait: a swing now is not measured. |
| **Off** | Not ready: OpenFlight is starting or stopped, the radar is not connected or not armed, or an error. |

## Parts

- One 3 mm or 5 mm green LED
- One 330 Ω resistor (about 4 mA from the 3.3 V pin, well inside a Pi GPIO's limit)
- Two female jumper wires to reach the Pi header

## Wiring

| From | To |
|------|----|
| Pi physical pin **16** (BCM 23) | 330 Ω resistor, then the LED **+** (anode, the longer leg) |
| LED **−** (cathode, the shorter leg, flat side of the rim) | Pi physical pin **25** (GND) |

The resistor can go on either side of the LED; it must be in series. Pin 25 is
a ground that the [sound trigger](sound-trigger.md), [OPS243 UART](ops243-uart.md)
and [inclinometer](inclinometer.md) wiring leave free. BCM 23 is suggested
because nothing else uses it and, like every pin from BCM 9 up, it is pulled
low at boot, so the LED stays dark until OpenFlight drives it.

> [!WARNING]
> Physical pin numbers and BCM numbers are different numbering systems. The
> flag takes the **BCM** number: 23, not 16.

## Turn It On

```bash
scripts/start-kiosk.sh --status-led-gpio 23
```

Without `--status-led-gpio` nothing changes. OpenFlight refuses, at startup,
a pin that is not on the header or already has a job: BCM 0 and 1 (HAT ID
EEPROM), 2 and 3 (I2C: inclinometer, UPS fuel gauge), 6 and 16 (Geekworm UPS
power detect and charging control), 14 and 15 (UART to the OPS243), 17 (sound
trigger `GATE`), and whatever `--iwr6843-trigger-pin` is set to.

If the Pi cannot drive the pin (no `gpiozero`/`lgpio`, or another program owns
the pin) OpenFlight logs one warning and runs without the light.

## What Drives It

- **OPS243 kiosk (the default).** Solid while the radar is armed and OpenFlight
  is listening for the next swing. Blinks from the first byte of a radar dump
  until the shot is analysed and OpenFlight is listening again. Off while
  starting or stopping, after a radar read error (until a read succeeds again),
  and after a failed re-arm (until the next re-arm succeeds; **Reinitialise
  Radar** in the Debug panel forces one).
- **With `--iwr6843`.** Also off until the angle radar is armed, and keeps
  blinking until its dump (about 7 s) is done: a swing during that dump gets no
  launch angle.
- **`--mock`.** Solid, because **Simulate shot** is always available. A swing
  recorder that reports to the kiosk (`/api/shots/measuring`) adds its own
  state: blinking while it reads its radar, off when it stops or has not
  reported for 30 s.
- **Swing-speed mode.** Solid while running. The radar streams continuously, so
  there is no dump to wait out; a radar that stops answering is not detected.

A normal shutdown (the menu's Shut down, Ctrl-C, closing `start-kiosk.sh`)
switches the light off first. If the server is killed outright it can keep its
last state until OpenFlight starts again.
