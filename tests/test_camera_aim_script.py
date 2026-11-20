"""scripts/hardware-test/camera_aim.py: setting the camera's straight line."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from openflight.camera.aim import CameraAim
from openflight.camera.flight_fit import CameraGeometry, CameraModel, RestingBall
from tests.test_camera_aim import _ground_line
from tests.test_camera_flight_fit import make_clip

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hardware-test" / "camera_aim.py"
spec = importlib.util.spec_from_file_location("camera_aim_script", SCRIPT)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def scene_with_ball(tmp_path) -> Path:
    frames, _stamps, _trig = make_clip()
    path = tmp_path / "aim_mean.npy"
    np.save(path, frames[:12].astype(np.float32).mean(axis=0))
    return path


def fmt(line):
    (x1, y1), (x2, y2) = line
    return f"{x1},{y1},{x2},{y2}"


def test_set_clubs_saves_the_target_yaw_with_the_reference_pending(tmp_path, capsys):
    pytest.importorskip("PIL")  # the annotation picture; Pillow comes with Pi OS
    scene = scene_with_ball(tmp_path)
    camera = CameraModel(CameraGeometry(), 640, 400, RestingBall(380.0, 232.0, 15.0, 30.0))
    mat = camera.ball_z - 0.0213
    left = _ground_line(camera, camera.ball_x - 0.35, 2.5, mat)
    right = _ground_line(camera, camera.ball_x + 0.3, 2.5, mat)
    aim_path = tmp_path / "aim.json"
    code = tool.main(
        [
            "--aim",
            str(aim_path),
            "set-clubs",
            str(scene),
            "--line1",
            fmt(left),
            "--line2",
            fmt(right),
            "--annotate",
            str(tmp_path / "check.png"),
        ]
    )
    assert code == 0
    aim = CameraAim.load(aim_path)
    assert aim.target_yaw_deg == pytest.approx(2.5, abs=0.15)
    assert aim.reference_pending and aim.method == "clubs"
    assert (tmp_path / "check.png").exists()
    assert "target line +2." in capsys.readouterr().out


def test_set_clubs_needs_a_ball(tmp_path, capsys):
    path = tmp_path / "empty.npy"
    np.save(path, np.full((400, 640), 20.0, dtype=np.float32))
    assert (
        tool.main(
            [
                "--aim",
                str(tmp_path / "a.json"),
                "set-clubs",
                str(path),
                "--line1",
                "0,0,1,1",
                "--line2",
                "5,0,6,2",
            ]
        )
        == 1
    )
    assert "No resting ball" in capsys.readouterr().out


def test_dry_run_saves_nothing(tmp_path):
    scene = scene_with_ball(tmp_path)
    assert (
        tool.main(
            [
                "--aim",
                str(tmp_path / "a.json"),
                "set-clubs",
                str(scene),
                "--line1",
                "300,300,330,250",
                "--line2",
                "500,300,460,250",
                "--dry-run",
            ]
        )
        == 0
    )
    assert not (tmp_path / "a.json").exists()


def test_parallel_lines_are_an_error_not_a_crash(tmp_path, capsys):
    scene = scene_with_ball(tmp_path)
    assert (
        tool.main(
            [
                "--aim",
                str(tmp_path / "a.json"),
                "set-clubs",
                str(scene),
                "--line1",
                "0,0,10,10",
                "--line2",
                "5,0,15,10",
            ]
        )
        == 1
    )
    assert "parallel" in capsys.readouterr().out


def test_bad_line_argument():
    with pytest.raises(SystemExit):
        tool.parse_args(["set-clubs", "x.npy", "--line1", "1,2,3", "--line2", "1,2,3,4"])
    with pytest.raises(SystemExit):
        tool.parse_args(["set-clubs", "x.npy", "--line1", "1,2,1,2", "--line2", "1,2,3,4"])


def test_set_shots_uses_accepted_camera_readings(tmp_path, capsys):
    session = tmp_path / "s"
    session.mkdir()
    lines = [
        {"camera_launch": {"accepted": True, "hla_axis_deg": v}}
        for v in (1, 2, 3, 4, 5, 6, 7, 8, 9)
    ]
    lines.append({"camera_launch": {"accepted": False, "hla_axis_deg": 40}})
    lines.append({"camera_launch": "no clip"})
    (session / "radar.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    assert tool.main(["--aim", str(tmp_path / "a.json"), "set-shots", str(session)]) == 0
    assert CameraAim.load(tmp_path / "a.json").target_yaw_deg == 5.0
    assert "STOPGAP" in capsys.readouterr().out
    assert (
        tool.main(["--aim", str(tmp_path / "b.json"), "set-shots", str(session), "--last", "3"])
        == 1
    )


def test_show_and_clear(tmp_path, capsys):
    aim_path = tmp_path / "a.json"
    assert tool.main(["--aim", str(aim_path), "show"]) == 0
    assert "not set" in capsys.readouterr().out
    CameraAim.load(aim_path).set_straight(None, 1.5, "clubs")
    tool.main(["--aim", str(aim_path), "show"])
    out = capsys.readouterr().out
    assert "+1.50" in out and "next swing" in out and "learning" in out
    assert tool.main(["--aim", str(aim_path), "clear"]) == 0
    assert CameraAim.load(aim_path).target_yaw_deg is None


def test_grid_picture(tmp_path):
    pytest.importorskip("PIL")
    scene = scene_with_ball(tmp_path)
    assert tool.main(["grid", str(scene), str(tmp_path / "g.png")]) == 0
    assert (tmp_path / "g.png").stat().st_size > 0
