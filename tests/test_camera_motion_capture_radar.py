"""Camera-triggered TI radar capture in the camera-only swing recorder."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "hardware-test" / "camera_motion_capture.py"
)
spec = importlib.util.spec_from_file_location("camera_motion_capture", SCRIPT)
recorder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recorder)


def measurement(*, found=True, angle=12.3, corrected=101.4, low=False, quality="ok"):
    track = SimpleNamespace(n_inliers=41, low_confidence=low, speed_mph=99.0)
    return SimpleNamespace(
        ball_found=found,
        track=track if found else None,
        ball_speed_mph=99.0,
        ball_speed_corrected_mph=corrected,
        launch_angle_deg=angle,
        quality=quality,
    )


def test_a_measured_tee_reports_speed_and_launch():
    line = recorder.radar_line(measurement(), launch_trusted=True)

    assert line.startswith("ball 101.4 mph, launch 12.3 deg")
    assert "41 points" in line


def test_without_a_tee_range_launch_angle_is_withheld():
    """The reference calibration's geometry is not this rig's, so its angle is not shown."""
    line = recorder.radar_line(measurement(), launch_trusted=False)

    assert "12.3" not in line
    assert "launch needs --tee-m" in line


def test_raw_speed_is_used_when_there_is_no_corrected_speed():
    assert recorder.radar_line(measurement(corrected=None), launch_trusted=True).startswith(
        "ball 99.0 mph"
    )


def test_no_ball_and_low_confidence_are_said_plainly():
    assert recorder.radar_line(measurement(found=False), launch_trusted=True) == (
        "no ball in the radar data"
    )
    assert "LOW CONFIDENCE" in recorder.radar_line(measurement(low=True), launch_trusted=True)


def test_the_stand_in_trigger_input_accepts_the_monitor_calls():
    button = recorder.NoTriggerInput(17, pull_up=False, bounce_time=None)
    button.when_pressed = print
    button.when_pressed = None
    button.close()


def test_radar_options_default_to_the_wide_config_and_reference_calibration():
    args = recorder.parse_args(["--iwr6843"])

    assert args.iwr6843 is True
    assert args.iwr6843_config.name == "iwr6843_l3dump_wide_24f3ms_53bin_iq16.cfg"
    assert args.iwr6843_cal.name == "iwr6843_calibration_reference.json"
    assert args.tee_m is None


@pytest.mark.parametrize(
    "argv",
    [
        ["--tee-m", "0"],
        ["--net-m", "-1"],
        ["--radar-height-m", "0"],
        ["--ball-height-m", "-0.1"],
    ],
)
def test_impossible_geometry_is_refused(argv):
    with pytest.raises(SystemExit):
        recorder.parse_args(argv)
