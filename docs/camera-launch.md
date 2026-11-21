# Camera launch angles (garage rig)

The swing recorder fits each swing's camera clip for the ball's launch, once
the TI radar has its ball speed. On the garage rig this replaces the radar's
vertical launch (it had no shot-to-shot relation to the ball's flight on
2026-11-19) and gives each shot's start direction relative to the target line.

| What | Where it comes from |
| --- | --- |
| Ball speed | TI radar (unchanged) |
| Vertical launch | Camera fit when accepted, else the radar's, else the club table |
| Start direction | Camera fit minus the camera's straight line (below) |
| Spin | Club-anchored estimate, adjusted by the measured launch |
| Club speed | OPS243, once `--ops243-club-speed` is tested; else none |
| Club path / curve | Not yet: needs the camera club tracker |

Code: `src/openflight/camera/flight_fit.py` (the fit), `src/openflight/camera/aim.py`
(the straight line and bumps), `scripts/hardware-test/camera_motion_capture.py`
(`CameraLaunchReader`, `merge_camera_launch`), kiosk side in
`src/openflight/measured_shot.py`.

## One-time setup (no hitting)

Do this whenever the monitor is placed somewhere new.

### 1. Camera height

Vertical launch errs about 0.4 deg per centimetre of camera-height error, so
measure it once with a tape:

- `L` = height of the lens centre above the floor;
- `B` = height of the ball's centre above the same floor = mat surface + tee
  height + 2.1 cm (half a ball).

Then `--camera-height-above-ball-m` = (L - B) in metres. The default 0.08 came
from the tilt sensor and the mat edge in the image (+-2 deg of launch). Set it
in the recorder's systemd unit (`ExecStart=.../record.sh ... --camera-height-above-ball-m 0.07`)
and restart the recorder.

### 2. Straight line (two clubs on the mat)

1. Ball on the tee; lay two clubs (or alignment sticks) flat on the mat,
   parallel to the target line, one each side of the ball.
2. `sudo systemctl restart openflight-swing-recorder`. The new session folder
   (`~/openflight_sessions/camera_motion/<newest>/`) gets `aim.jpg` and
   `aim_mean.npy`.
3. `cd ~/openflight-motion-test && .venv/bin/python scripts/hardware-test/camera_aim.py grid <session>/aim_mean.npy /tmp/grid.png`
   and read two points along each club off the grid (every 20 px).
4. `.venv/bin/python scripts/hardware-test/camera_aim.py set-clubs <session>/aim_mean.npy --line1 X1,Y1,X2,Y2 --line2 X1,Y1,X2,Y2 --annotate /tmp/check.png`
   and look at `check.png`: the thick lines must lie on the clubs and their
   thin extensions meet far down the range.
5. Pick the clubs up. The next swing's clip becomes the reference scene.

`camera_aim.py show` prints the state; `clear` forgets it. `set-shots SESSION`
calls the session's median start line straight; it is a stopgap only, because
it assumes your average shot starts on line.

Until the line is set, sideways stays on the radar's own zero (as before).

## While playing

Each swing prints one `CAMERA #n:` line in
`~/openflight_sessions/camera_motion/live-service.log`, for example
`launch 12.4 deg up, -1.3 deg from the target line, 11 frames, aim set`.

- **aim set**: the scene right of the ball matched; a bump of the monitor is
  corrected automatically (2026-11-19: both mid-session turns, 2.9 and 4.3 deg).
- **aim scene_lost**: the golfer hid that scene for this clip; the last bump stands.
- **monitor moved**: three clips in a row without a match, or a turn over 6 deg.
  No sideways is sent until the line is set again (step 2).
- **launch not read (reason)**: the radar's angles are kept. Reasons:
  `ball_not_seen` (fewer than 7 frames saw the ball), `ball_stayed` (the tee
  spot never went dark: the clip missed the strike), `frames_dropped`,
  `edge_of_search` (impact outside -6..+10 ms of the trigger, or a start line
  beyond +-25 deg / launch beyond -4..50 deg), `no_resting_ball`.

When the camera misses but the radar read a sideways, the radar's reading is
carried into the target frame by its offset from the camera, learned over the
last 40 shots both measured (8 needed; reset when the radar's zero changes).

## 300 fps test

The fit's window is in time (53 ms after the trigger), so a faster camera fits
more frames with no code change. Exposure (1250 us) fits inside a 3.3 ms frame.

1. Light the mat as for play; no swing needed at first.
2. Add `--fps 300` to the recorder's ExecStart, restart, and check the
   `--calibrate` lines in live-service.log: about 300 fps delivered and
   `dropped` not climbing. Then check `Camera armed at ... 300 fps requested`.
3. Hit 10 balls. Each `clip n:` line should say about 300 fps and `gaps 0`;
   `CAMERA #n` should read `ok` on full swings with more frames (about 16).
4. Revert to 240 if frames drop or the ball-leaves trigger misfires (it was
   tuned at 230 fps).

## Deploy checklist (needs the go-ahead)

The Pi runs two copies: the kiosk from `~/openflight` (git) and the recorder
from `~/openflight-motion-test` (a plain copy with its own `.venv`).

1. Back up both: `tar czf ~/config-backups/pre-camera-launch-$(date +%Y%m%d-%H%M).tgz ~/openflight/src ~/openflight-motion-test/src ~/openflight-motion-test/scripts /etc/systemd/system/openflight*.service*`.
2. Kiosk: check out the branch in `~/openflight` (from a bundle or the fork), keeping its uncommitted changes committed first.
3. Recorder: copy `src/openflight/` and `scripts/hardware-test/camera_motion_capture.py`,
   `scripts/hardware-test/camera_aim.py` into `~/openflight-motion-test/` (same paths);
   `config/` files too.
4. `sudo systemctl restart openflight openflight-swing-recorder`; watch
   live-service.log for `Camera launch on; straight line ...` and `Swing away`.
5. Hit a few balls: `CAMERA #n` lines, and the kiosk shows the shots.
6. Rollback: untar the backup over the same paths and restart both services.

Kiosk start flags stay `--mock --sim --calculated-spin --spin-model club-anchored --max-single-channel-spread 3`
(the spread gate now applies to radar verticals only).

## Offline replay

`scripts/analysis/replay_recorder_session.py SESSION... --out DIR [--post-to URL]`
runs recorded swings (radar dumps, clips, the rig geometry the kiosk gave
each) through `RadarWorker.measure` with the current code. Post only to a test
kiosk (`openflight-server --mock --web-port 8090` with `HOME` pointed at a
scratch folder), never the live one.

On the 2026-11-19 swings (21:44 onwards): 78 shots posted; the camera read 73
(both angles), 6-iron median launch 10.5 deg at 116 mph, start lines 74% left
(median -1.1 deg against the 21:44 aim), carry 158 yd in OpenFlight and in
Open-Birdie's physics alike (within 1 yd per shot).
