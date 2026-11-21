"""The recorder's camera launch: merging the camera's angles into the kiosk payload."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from openflight.camera.aim import AimReading, CameraAim
from openflight.camera.flight_fit import CameraLaunch

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "hardware-test" / "camera_motion_capture.py"
)
spec = importlib.util.spec_from_file_location("camera_motion_capture_cl", SCRIPT)
recorder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recorder)

RADAR_PAYLOAD = {
    "ball_speed_mph": 120.0,
    "horizontal_phase_rad": -0.71,
    "launch_angle_vertical": 19.5,
    "vertical_single_channel": True,
    "vertical_component_std_deg": 4.1,
    "launch_angle_horizontal": -3.2,
    "horizontal_coherence": 0.85,
}


def radar_launch(horizontal=-3.2):
    return SimpleNamespace(horizontal_deg=horizontal)


def camera(accepted=True, hla_axis=2.5, vla=12.3, status="ok"):
    return CameraLaunch(
        accepted=accepted,
        status=status,
        hla_axis_deg=hla_axis,
        vla_deg=vla,
        impact_ms=1.8,
        hits=11,
        moving_frames=12,
        score=120.0,
    )


AIM_SET = AimReading(status="set", target_yaw_deg=1.0, bump_deg=0.4)
AIM_UNSET = AimReading(status="unset")
AIM_MOVED = AimReading(status="moved", bump_deg=7.0)
AIM_LOST = AimReading(status="scene_lost", target_yaw_deg=1.0, bump_deg=0.4)


def merge(cam, aim, *, payload=None, launch=None, offset=None):
    return recorder.merge_camera_launch(
        dict(RADAR_PAYLOAD if payload is None else payload),
        launch=radar_launch() if launch is None else launch,
        camera=cam,
        aim=aim,
        radar_offset_deg=offset,
    )


def test_camera_replaces_both_angles_when_the_line_is_set():
    out = merge(camera(), AIM_SET)
    assert out["launch_angle_vertical"] == 12.3
    assert out["launch_angle_vertical_source"] == "camera"
    assert out["launch_angle_horizontal"] == pytest.approx(1.5)  # 2.5 axis - 1.0 target
    assert out["launch_angle_horizontal_source"] == "camera"
    assert "vertical_single_channel" not in out
    assert "vertical_component_std_deg" not in out
    assert "horizontal_coherence" not in out
    assert out["horizontal_phase_rad"] == -0.71  # still feeds the kiosk's radar zero
    assert out["radar_launch"] == {
        "vla_deg": 19.5,
        "single_channel": True,
        "component_std_deg": 4.1,
        "hla_deg": -3.2,
        "horizontal_coherence": 0.85,
    }
    diag = out["camera_launch"]
    assert diag["accepted"] is True and diag["status"] == "ok"
    assert diag["hla_deg"] == pytest.approx(1.5)
    assert (
        diag["hla_axis_deg"] == 2.5 and diag["aim_yaw_deg"] == 1.0 and diag["bump_yaw_deg"] == 0.4
    )
    assert diag["aim_status"] == "set" and diag["hits"] == 11 and diag["frames"] == 12


def test_a_hidden_scene_uses_the_last_bump_like_set():
    out = merge(camera(), AIM_LOST)
    assert out["launch_angle_horizontal_source"] == "camera"
    assert out["camera_launch"]["aim_status"] == "scene_lost"


def test_unset_line_keeps_the_radars_sideways():
    out = merge(camera(), AIM_UNSET)
    assert out["launch_angle_vertical_source"] == "camera"
    assert out["launch_angle_horizontal"] == -3.2
    assert out["horizontal_coherence"] == 0.85
    assert "launch_angle_horizontal_source" not in out
    assert out["camera_launch"]["hla_deg"] is None


def test_moved_monitor_sends_no_sideways_at_all():
    out = merge(camera(), AIM_MOVED)
    assert "launch_angle_horizontal" not in out
    assert "horizontal_coherence" not in out
    assert out["radar_launch"]["hla_deg"] == -3.2
    assert out["launch_angle_vertical_source"] == "camera"


def test_a_camera_start_line_beyond_the_limit_is_dropped():
    out = merge(camera(hla_axis=30.0), AIM_SET)
    assert "launch_angle_horizontal" not in out
    assert out["camera_launch"]["hla_deg"] == pytest.approx(29.0)


def test_rejected_fit_keeps_the_radar_vertical():
    out = merge(camera(accepted=False, status="ball_not_seen"), AIM_UNSET)
    assert out["launch_angle_vertical"] == 19.5
    assert out["vertical_single_channel"] is True
    assert "launch_angle_vertical_source" not in out
    assert out["camera_launch"]["accepted"] is False
    assert out["camera_launch"]["status"] == "ball_not_seen"
    assert "radar_launch" not in out


def test_rejected_fit_points_the_radar_by_its_learned_offset():
    # radar -3.2 in its own frame, offset +2.0 from the camera axis, target 1.0
    out = merge(camera(accepted=False), AIM_SET, offset=2.0)
    assert out["launch_angle_horizontal"] == pytest.approx(-6.2)
    assert out["launch_angle_horizontal_source"] == "radar"
    assert out["horizontal_coherence"] == 0.85


def test_rejected_fit_without_an_offset_sends_no_sideways_once_the_line_is_set():
    out = merge(camera(accepted=False), AIM_SET)
    assert "launch_angle_horizontal" not in out


def test_pointed_radar_still_obeys_the_radar_limit():
    out = merge(camera(accepted=False), AIM_SET, launch=radar_launch(15.0), offset=-2.0)
    assert "launch_angle_horizontal" not in out  # 15 + 2 - 1 = 16, beyond MAX_SIDEWAYS_DEG


def test_camera_sideways_when_the_radar_had_none():
    payload = {"ball_speed_mph": 98.0}
    out = merge(camera(), AIM_SET, payload=payload, launch=radar_launch(None))
    assert out["launch_angle_vertical_source"] == "camera"
    assert out["launch_angle_horizontal"] == pytest.approx(1.5)
    assert "radar_launch" not in out


def test_radar_reading_that_was_gated_out_still_shows_in_radar_launch():
    payload = {"ball_speed_mph": 98.0}
    out = merge(camera(), AIM_SET, payload=payload, launch=radar_launch(-40.0))
    assert out["radar_launch"] == {"hla_deg": -40.0}


def test_camera_line_describes_each_case():
    assert "from the target line" in recorder.camera_line(merge(camera(), AIM_SET))
    assert "camera_aim.py" in recorder.camera_line(merge(camera(), AIM_UNSET))
    assert "moved" in recorder.camera_line(merge(camera(), AIM_MOVED))
    assert "not read" in recorder.camera_line(
        merge(camera(accepted=False, status="ball_stayed"), AIM_SET)
    )


# --- the reader, end to end on a synthetic clip ------------------------------


def _write_clip(directory: Path) -> Path:
    from tests.test_camera_flight_fit import SPEED_MPS, make_clip  # noqa: PLC0415

    frames, stamps, trig = make_clip(4.0, 15.0)
    directory.mkdir(parents=True)
    np.savez(
        directory / "frames.npz",
        frames=frames,
        sensor_timestamp_ns=stamps,
        pre_trigger_count=trig + 1,
    )
    (directory / "metadata.json").write_text(json.dumps({"trigger_timestamp": 100.0}))
    return directory, SPEED_MPS


def reader_args(tmp_path, **overrides):
    args = recorder.parse_args(["--iwr6843", "--camera-aim", str(tmp_path / "aim.json")])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_reader_fits_the_swing_clip_and_learns_the_radar_offset(tmp_path):
    clip, speed_mps = _write_clip(tmp_path / "clip")
    sizes = recorder.BallSizes()
    sizes.add(100.0, 15.0, clip_path=clip, ball_xy=(380.0, 232.0))
    aim = CameraAim.load(tmp_path / "aim.json")
    aim.set_straight(None, 1.0, "clubs")
    reader = recorder.CameraLaunchReader(reader_args(tmp_path), sizes, aim=aim)
    measurement = SimpleNamespace(
        ball_speed_corrected_mph=speed_mps / 0.44704, ball_speed_mph=speed_mps / 0.44704
    )
    record: dict = {}
    out = reader.apply(
        SimpleNamespace(trigger_timestamp=100.2, sequence=7),
        measurement,
        radar_launch(-1.0),
        dict(RADAR_PAYLOAD),
        {"tee_range_m": 1.549, "horizontal_phase_reference_rad": -0.8},
        record,
    )
    assert out["launch_angle_vertical"] == pytest.approx(15.0, abs=0.4)
    assert out["launch_angle_horizontal"] == pytest.approx(3.0, abs=0.3)  # 4 axis - 1 target
    assert record["camera_launch"]["accepted"] is True
    assert aim.radar_pairs == [pytest.approx(-5.0, abs=0.3)]
    assert aim.radar_zero == -0.8
    assert json.loads((tmp_path / "aim.json").read_text())["radar_pairs"]


def test_reader_without_a_clip_leaves_the_payload_alone(tmp_path):
    reader = recorder.CameraLaunchReader(reader_args(tmp_path), recorder.BallSizes())
    record: dict = {}
    out = reader.apply(
        SimpleNamespace(trigger_timestamp=5.0, sequence=1),
        None,
        None,
        dict(RADAR_PAYLOAD),
        None,
        record,
    )
    assert out == RADAR_PAYLOAD
    assert record["camera_launch"] == "no clip"


def test_reader_failure_never_costs_the_shot(tmp_path, capsys):
    sizes = recorder.BallSizes()
    sizes.add(5.0, 15.0, clip_path=tmp_path / "missing", ball_xy=None)
    reader = recorder.CameraLaunchReader(reader_args(tmp_path), sizes)
    record: dict = {}
    measurement = SimpleNamespace(ball_speed_corrected_mph=100.0, ball_speed_mph=100.0)
    out = reader.apply(
        SimpleNamespace(trigger_timestamp=5.0, sequence=2),
        measurement,
        None,
        dict(RADAR_PAYLOAD),
        None,
        record,
    )
    assert out == RADAR_PAYLOAD
    assert record["camera_launch"].startswith("failed")
    assert "launch fit failed" in capsys.readouterr().out


def test_geometry_follows_the_kiosk_distance_then_the_flag(tmp_path):
    reader = recorder.CameraLaunchReader(
        reader_args(tmp_path, tee_m=1.7, camera_height_above_ball_m=0.09), recorder.BallSizes()
    )
    assert reader.geometry({"tee_range_m": 1.6}).ball_distance_m == 1.6
    assert reader.geometry(None).ball_distance_m == 1.7
    assert reader.geometry(None).height_above_ball_m == 0.09


def test_flags():
    args = recorder.parse_args(["--iwr6843"])
    assert args.no_camera_launch is False
    assert args.camera_height_above_ball_m == 0.08
    with pytest.raises(SystemExit):
        recorder.parse_args(["--camera-focal-px", "0"])


def test_ball_sizes_keep_the_clip_and_answer_by_time(tmp_path):
    sizes = recorder.BallSizes()
    sizes.add(10.0, None, clip_path=tmp_path / "a")
    sizes.add(20.0, 14.0, clip_path=tmp_path / "b", ball_xy=(1.0, 2.0))
    assert sizes.clip_near(19.6).clip_path == tmp_path / "b"
    assert sizes.clip_near(19.6).ball_xy == (1.0, 2.0)
    assert sizes.left_near(10.2) is False
    assert sizes.left_near(20.3) is True
    assert sizes.near(20.3) == 14.0
    assert sizes.clip_near(40.0) is None
    assert sizes.left_near(40.0) is None


def test_club_path_is_logged_only_never_posted(tmp_path, monkeypatch):
    clip, speed_mps = _write_clip(tmp_path / "clip2")
    sizes = recorder.BallSizes()
    sizes.add(100.0, 15.0, clip_path=clip, ball_xy=(380.0, 232.0))
    reader = recorder.CameraLaunchReader(reader_args(tmp_path), sizes)
    calls = []

    def fake_estimate(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="ok",
            path_deg=2.0,
            attack_angle_deg=-4.0,
            confidence="low",
            raw_confidence="high",
            n_pre=3,
            n_post=6,
            rms_px=1.1,
            elapsed_ms=52.0,
        )

    monkeypatch.setattr(recorder, "estimate_club_delivery", fake_estimate)
    measurement = SimpleNamespace(
        ball_speed_corrected_mph=speed_mps / 0.44704, ball_speed_mph=speed_mps / 0.44704
    )
    record: dict = {}
    payload = reader.apply(
        SimpleNamespace(trigger_timestamp=100.0, sequence=4),
        measurement,
        None,
        dict(RADAR_PAYLOAD),
        None,
        record,
    )
    assert not any(key.startswith("club_") for key in payload)
    assert calls == []  # nothing before the shot is posted
    reader.log_club(SimpleNamespace(sequence=4), record)
    assert record["camera_club"]["path_axis_deg"] == 2.0
    assert record["camera_club"]["confidence"] == "low"
    assert calls and calls[0]["ball_hla_deg"] == pytest.approx(4.0, abs=0.3)
    reader.log_club(SimpleNamespace(sequence=4), record := {})
    assert "camera_club" not in record  # once per swing


def test_club_log_can_be_turned_off_and_failures_stay_in_the_record(tmp_path, monkeypatch):
    clip, speed_mps = _write_clip(tmp_path / "clip3")
    sizes = recorder.BallSizes()
    sizes.add(100.0, 15.0, clip_path=clip, ball_xy=(380.0, 232.0))
    measurement = SimpleNamespace(
        ball_speed_corrected_mph=speed_mps / 0.44704, ball_speed_mph=speed_mps / 0.44704
    )
    capture = SimpleNamespace(trigger_timestamp=100.0, sequence=5)

    off = recorder.CameraLaunchReader(reader_args(tmp_path, no_camera_club_log=True), sizes)
    off.apply(capture, measurement, None, dict(RADAR_PAYLOAD), None, {})
    record: dict = {}
    off.log_club(capture, record)
    assert "camera_club" not in record

    def boom(*_args, **_kwargs):
        raise RuntimeError("bad clip")

    monkeypatch.setattr(recorder, "estimate_club_delivery", boom)
    on = recorder.CameraLaunchReader(reader_args(tmp_path), sizes)
    on.apply(capture, measurement, None, dict(RADAR_PAYLOAD), None, {})
    on.log_club(capture, record)
    assert record["camera_club"] == "failed: bad clip"
