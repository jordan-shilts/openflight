#!/bin/bash
# Swing recorder: camera motion trigger (dark-room settings, upside-down mount) plus the TI angle radar.
# Each credible swing goes to the kiosk with its launch angles. On every swing it reads the kiosk for
# the selected club and the Setup tab geometry, and the case tilt sensor (board axis x), and it tells
# the kiosk when the radar is ready or measuring. Uses its own Python setup so kiosk restarts cannot
# break it. Extra arguments are passed through.
cd ~/openflight-motion-test || exit 1
K=http://127.0.0.1:8080
exec ~/openflight-motion-test/.venv/bin/python -u scripts/hardware-test/camera_motion_capture.py \
  --rotate-180 --calibrate --min-delta 15 --min-fraction 0.015 --iwr6843 --duration 7200 \
  --post-to $K/api/shots/measured --status-to $K/api/shots/measuring \
  --club-from $K/api/club --rig-setup-url $K/api/rig-setup/observation --pitch-axis x "$@"
