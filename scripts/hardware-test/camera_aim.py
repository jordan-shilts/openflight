#!/usr/bin/env python3
"""Set or check the camera's straight line (the target line in the camera's view).

The swing recorder reads each shot's start direction from the camera relative
to this line, and follows it when the monitor is bumped (openflight.camera.aim).
Set it once per placement of the monitor:

1. Put a ball on the tee and lay two clubs (or alignment sticks) flat on the
   mat, parallel to the target line, one each side of the ball.
2. Restart the recorder (``sudo systemctl restart openflight-swing-recorder``):
   its session folder gets aim.jpg and aim_mean.npy, the scene at start.
3. ``camera_aim.py grid SESSION/aim_mean.npy grid.png`` and read two points
   along each club off grid.png.
4. ``camera_aim.py set-clubs SESSION/aim_mean.npy --line1 X1,Y1,X2,Y2
   --line2 X1,Y1,X2,Y2 --annotate check.png`` and look at check.png: the
   lines should lie on the clubs and meet far down the range.
5. Pick the clubs up. The next swing's clip becomes the reference scene.

Without clubs, ``set-shots SESSION`` calls the median start line of the
session's camera-read shots straight. That assumes your average shot starts on
line, so it is a stopgap only.

``show`` prints the current state; ``clear`` forgets it (sideways then goes
back to the radar's own zero).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import numpy as np  # noqa: E402

from openflight.camera.aim import (  # noqa: E402
    CameraAim,
    target_yaw_from_lines,
    target_yaw_from_shots,
    vanishing_point,
)
from openflight.camera.flight_fit import (  # noqa: E402
    CameraGeometry,
    CameraModel,
    find_resting_ball,
    load_clip,
)

DEFAULT_AIM = Path.home() / ".config" / "openflight" / "camera-aim.json"


def points(value: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """``X1,Y1,X2,Y2`` -> two points."""
    try:
        x1, y1, x2, y2 = (float(v) for v in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("give a line as X1,Y1,X2,Y2") from error
    if (x1, y1) == (x2, y2):
        raise argparse.ArgumentTypeError("a line needs two different points")
    return (x1, y1), (x2, y2)


def load_scene(path: Path) -> np.ndarray:
    """The quiet scene: aim_mean.npy, or the first frames of a clip folder."""
    if path.is_dir():
        frames, _stamps, _trigger = load_clip(path)
        return frames[:12].astype(np.float32).mean(axis=0)
    return np.load(path).astype(np.float32)


def stretched(scene: np.ndarray) -> np.ndarray:
    """The dim garage scene stretched to be visible, as RGB uint8."""
    low, high = np.percentile(scene, (1, 99.7))
    gray = np.clip((scene - low) / max(high - low, 1e-6) * 255, 0, 255).astype(np.uint8)
    return np.stack([gray] * 3, axis=-1)


def save_png(rgb: np.ndarray, path: Path) -> None:
    from PIL import Image  # pylint: disable=import-outside-toplevel

    Image.fromarray(rgb).save(path)


def cmd_grid(args) -> int:
    rgb = stretched(load_scene(args.scene))
    rgb[::20, :, :] = (rgb[::20, :, :] * 0.5 + np.array([0, 90, 0]) * 0.5).astype(np.uint8)
    rgb[:, ::20, :] = (rgb[:, ::20, :] * 0.5 + np.array([0, 90, 0]) * 0.5).astype(np.uint8)
    rgb[::100, :, :] = (0, 200, 0)
    rgb[:, ::100, :] = (0, 200, 0)
    save_png(rgb, args.out)
    print(f"{args.out}: grid every 20 px, bright lines every 100 px")
    return 0


def geometry_from(args) -> CameraGeometry:
    return CameraGeometry(
        focal_px=args.focal_px,
        height_above_ball_m=args.camera_height_above_ball_m,
        ball_distance_m=args.ball_distance_m,
        roll_deg=args.roll_deg,
    )


def cmd_set_clubs(args) -> int:
    scene = load_scene(args.scene)
    ball = find_resting_ball(scene)
    if ball is None:
        print("No resting ball found: put a ball on the tee and restart the recorder.")
        return 1
    height, width = scene.shape
    camera = CameraModel(geometry_from(args), width, height, ball)
    yaw = target_yaw_from_lines(
        args.line1,
        args.line2,
        focal_px=args.focal_px,
        pitch_deg=math.degrees(camera.pitch),
        width=width,
        height=height,
        roll_deg=args.roll_deg,
    )
    vp = vanishing_point(args.line1, args.line2)
    print(
        f"ball at ({ball.u:.1f}, {ball.v:.1f}) px, camera pitch {math.degrees(camera.pitch):+.2f} deg; "
        f"lines meet at ({vp[0]:.1f}, {vp[1]:.1f}) px"
    )
    print(f"target line {yaw:+.2f} deg from the camera axis (+ = right)")
    if args.annotate:
        annotate(scene, args.line1, args.line2, vp, (ball.u, ball.v), args.annotate)
    if args.dry_run:
        return 0
    aim = CameraAim.load(args.aim, focal_px=args.focal_px)
    aim.set_straight(None, yaw, "clubs")
    print(f"saved to {args.aim}; pick the clubs up: the next swing's scene is the reference")
    return 0


def annotate(scene, line1, line2, vp, ball_uv, out: Path) -> None:
    from PIL import Image, ImageDraw  # pylint: disable=import-outside-toplevel

    image = Image.fromarray(stretched(scene))
    draw = ImageDraw.Draw(image)
    for (p1, p2), colour in ((line1, (255, 60, 60)), (line2, (60, 160, 255))):
        draw.line([p1, vp], fill=colour, width=1)
        draw.line([p1, p2], fill=colour, width=3)
    draw.ellipse([vp[0] - 4, vp[1] - 4, vp[0] + 4, vp[1] + 4], outline=(255, 255, 0))
    u, v = ball_uv
    draw.ellipse([u - 9, v - 9, u + 9, v + 9], outline=(0, 255, 0))
    image.save(out)
    print(f"{out}: clubs (thick), their lines to the vanishing point, the ball (green)")


def cmd_set_shots(args) -> int:
    values = []
    for session in args.session:
        path = Path(session) / "radar.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            camera = record.get("camera_launch")
            if (
                isinstance(camera, dict)
                and camera.get("accepted")
                and camera.get("hla_axis_deg") is not None
            ):
                values.append(float(camera["hla_axis_deg"]))
    values = values[-args.last :] if args.last else values
    try:
        yaw = target_yaw_from_shots(values)
    except ValueError as error:
        print(f"Cannot set from shots: {error}")
        return 1
    print(f"median start line of {len(values)} shots: {yaw:+.2f} deg from the camera axis")
    print("STOPGAP: this calls your average start line straight; set it with clubs when you can.")
    if args.dry_run:
        return 0
    aim = CameraAim.load(args.aim, focal_px=args.focal_px)
    aim.set_straight(None, yaw, "shots")
    print(f"saved to {args.aim}")
    return 0


def cmd_show(args) -> int:
    aim = CameraAim.load(args.aim, focal_px=args.focal_px)
    if aim.target_yaw_deg is None:
        print("not set: sideways stays on the radar's own zero")
        return 0
    print(f"target line {aim.target_yaw_deg:+.2f} deg from the camera axis, set by {aim.method}")
    print(
        f"reference scene: {'next swing' if aim.reference_pending else 'saved'}; last bump {aim.last_bump_deg:+.2f} deg"
    )
    offset = aim.radar_offset_deg(aim.radar_zero)
    pairs = len(aim.radar_pairs)
    print(
        "radar offset from the camera: "
        + (
            f"{offset:+.2f} deg over {pairs} shots"
            if offset is not None
            else f"learning ({pairs} shots so far)"
        )
    )
    return 0


def cmd_clear(args) -> int:
    for path in (args.aim, args.aim.with_suffix(".scene.npy")):
        if path.exists():
            path.unlink()
            print(f"removed {path}")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--aim", type=Path, default=DEFAULT_AIM)
    parser.add_argument("--focal-px", type=float, default=535.0)
    parser.add_argument("--camera-height-above-ball-m", type=float, default=0.08)
    parser.add_argument("--ball-distance-m", type=float, default=1.549)
    parser.add_argument("--roll-deg", type=float, default=0.0)
    sub = parser.add_subparsers(dest="command", required=True)
    grid = sub.add_parser("grid", help="a stretched picture of the scene with a pixel grid")
    grid.add_argument("scene", type=Path, help="aim_mean.npy or a clip folder")
    grid.add_argument("out", type=Path)
    clubs = sub.add_parser("set-clubs", help="set the line from two clubs on the mat")
    clubs.add_argument(
        "scene", type=Path, help="aim_mean.npy (clubs and ball in view) or a clip folder"
    )
    clubs.add_argument("--line1", type=points, required=True)
    clubs.add_argument("--line2", type=points, required=True)
    clubs.add_argument("--annotate", type=Path)
    clubs.add_argument("--dry-run", action="store_true")
    shots = sub.add_parser("set-shots", help="stopgap: median start line of a session's shots")
    shots.add_argument("session", nargs="+")
    shots.add_argument("--last", type=int, default=0, help="only the last N shots")
    shots.add_argument("--dry-run", action="store_true")
    sub.add_parser("show")
    sub.add_parser("clear")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    handlers = {
        "grid": cmd_grid,
        "set-clubs": cmd_set_clubs,
        "set-shots": cmd_set_shots,
        "show": cmd_show,
        "clear": cmd_clear,
    }
    try:
        return handlers[args.command](args)
    except ValueError as error:
        print(f"error: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
