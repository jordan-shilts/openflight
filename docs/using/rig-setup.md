# Rig Setup

Launch angle depends on how far the ball sits from the TI radar. The **Setup**
tab on the touchscreen keeps two saved locations so that number is entered
once, not every time you hit.

## Simulator

The simulator doesn't move, so measure it once with a tape:

- **Ball distance:** from the face of the red radar board to the ball, along
  the floor.
- **Net:** from the radar board to the net.
- **Radar height:** from the floor to the middle of the red board.

Use the minus and plus buttons; hold one to keep going. Changes apply on the
next swing with no restart.

The next three swings in Simulator also teach the camera how big a ball looks
at that distance. If you correct the ball distance later, the camera relearns
from the following three swings.

## Course

On the course the distance changes each time you set the case down, so
**Auto** measures it. A golf ball is always the same size, so the camera reads
the distance from how big the ball looks before each swing. The net is off by
default.

The label under **Ball distance** says where the number came from:

| Label | Meaning |
| --- | --- |
| From camera | Measured from this swing's ball |
| Set by you | You adjusted it, or Auto has nothing better |
| Waiting for a swing | Auto is on and hasn't seen a ball yet |

To correct it, press minus or plus. That switches to your number until you
press **Auto** again. A hand adjustment never changes what the camera learned
in Simulator.

## What stays automatic

- **Tilt:** the case's tilt sensor compares each swing with the simulator, so
  sloped ground is corrected.
- **Club:** read from the club selected on the screen. The screen remembers
  that club, so the next start opens on it instead of Driver.
- **Radar height:** carries over as long as the case sits on the ground.

Ball height assumes the ball is on the ground or a mat, so a teed ball reads
slightly off.

## Straight line

Sideways launch needs to know which way is straight. Until that is set, the
H. launch tile stays empty.

1. Aim the case at your target.
2. Hit at least three shots at the target. Five is better.
3. On the **Setup** tab, press **Set** beside **Straight line**.

The average start line of your last five shots becomes zero, and it is saved.
Press **Set** again after moving the case in the simulator, or **Clear** to go
back to no sideways reading. The careful way to set it is with shots you can
see start on the line, such as putts rolled along a string.

The radar sits a hand's width above the floor, so it hears each ball twice:
directly, and off the floor. The recorder's default sideways method
(`--horizontal-method colocated`) compares only antenna positions that hear
both paths the same way, and reads the flight out to 3.4 m, before the net.

## Ready and Measuring

Reading the radar after a swing takes about two and a half seconds with the
recorder's default capture profile (seven with the full one), and a swing made
during that read is not measured. With the camera-triggered recorder running,
the Live screen shows a line under the header:

| Line | Meaning |
| --- | --- |
| **Ready** | Swing when you like |
| **Measuring**, filling left to right | The last swing is being read. Wait for Ready |
| **Ready**, "No ball seen on that one" | The read finished and found no ball |

The line disappears when the recorder stops. Radar-only swings show five tiles
(ball speed, carry, vertical launch, horizontal launch, spin). Club speed,
smash, spin axis, club path and attack angle return with the OPS243.

The recorder reports its state with `--status-to
http://127.0.0.1:8080/api/shots/measuring`.

## Capture profiles

The wait is the radar's data crossing a serial cable, so it follows the size of
the capture. The recorder takes `--iwr6843-config`:

| Profile | Frames and samples | Wait |
| --- | --- | --- |
| `iwr6843_l3dump_ball_16f3ms_53bin_iq8.cfg` (default) | Flight frames only, 8-bit | about 2.4 s |
| `iwr6843_l3dump_ball_16f3ms_53bin_iq16.cfg` | Flight frames only, 16-bit | about 4.7 s |
| `iwr6843_l3dump_wide_24f3ms_53bin_iq16.cfg` | Everything, 16-bit | about 7.0 s |

The ball profiles drop the frames from before the camera trigger, which hold
no flight the ball estimators keep. Use the 16-bit ball profile to check
whether 8-bit samples cost any accuracy on your rig.
