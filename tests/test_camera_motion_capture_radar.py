"""Camera-triggered TI radar capture in the camera-only swing recorder."""

import importlib.util
import io
import json
import urllib.error
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


def test_a_credible_ball_is_sent_as_its_corrected_speed():
    assert recorder.shot_payload(measurement(), launch_trusted=False) == {"ball_speed_mph": 101.4}


def test_launch_angle_is_sent_only_with_a_measured_tee_range():
    assert recorder.shot_payload(measurement(), launch_trusted=True) == {
        "ball_speed_mph": 101.4,
        "launch_angle_vertical": 12.3,
    }
    assert recorder.shot_payload(measurement(angle=None), launch_trusted=True) == {
        "ball_speed_mph": 101.4
    }


@pytest.mark.parametrize(
    "kwargs",
    [{"found": False}, {"quality": "reject"}, {"low": True}],
)
def test_doubtful_readings_never_reach_the_screen(kwargs):
    assert recorder.shot_payload(measurement(**kwargs), launch_trusted=True) is None


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_post_shot_sends_json_and_reports_the_shot_number():
    sent = {}

    def opener(request, timeout):
        sent.update(
            url=request.full_url,
            method=request.get_method(),
            body=json.loads(request.data),
            content_type=request.get_header("Content-type"),
            timeout=timeout,
        )
        return FakeResponse(b'{"shot_number": 7, "ball_speed_mph": 118.2}')

    status = recorder.post_shot("http://127.0.0.1:8080/api/shots/measured", {"x": 1}, opener=opener)

    assert status == "on screen as shot 7"
    assert sent == {
        "url": "http://127.0.0.1:8080/api/shots/measured",
        "method": "POST",
        "body": {"x": 1},
        "content_type": "application/json",
        "timeout": 3.0,
    }


def test_post_shot_reports_a_refusal_without_raising():
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 409, "Conflict", {}, io.BytesIO(b'{"error": "only in --mock"}')
        )

    status = recorder.post_shot("http://127.0.0.1:8080/x", {}, opener=opener)

    assert status.startswith("kiosk refused it (409")
    assert "only in --mock" in status


@pytest.mark.parametrize(
    "error",
    [urllib.error.URLError("connection refused"), TimeoutError("timed out"), OSError("down")],
)
def test_post_shot_reports_an_unreachable_kiosk_without_raising(error):
    def opener(request, timeout):
        raise error

    assert recorder.post_shot("http://127.0.0.1:8080/x", {}, opener=opener).startswith(
        "kiosk unreachable"
    )


def test_post_shot_survives_a_garbled_reply():
    status = recorder.post_shot(
        "http://127.0.0.1:8080/x", {}, opener=lambda request, timeout: FakeResponse(b"<html>")
    )

    assert status.startswith("kiosk reply unreadable")


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
