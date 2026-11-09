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
- **Club:** read from the club selected on the screen.
- **Radar height:** carries over as long as the case sits on the ground.

Ball height assumes the ball is on the ground or a mat, so a teed ball reads
slightly off.
