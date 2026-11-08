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


def measurement(*, found=True, corrected=101.4, low=False, quality="ok"):
    track = SimpleNamespace(n_inliers=41, low_confidence=low, speed_mph=99.0)
    return SimpleNamespace(
        ball_found=found,
        track=track if found else None,
        ball_speed_mph=99.0,
        ball_speed_corrected_mph=corrected,
        quality=quality,
    )


def lcmf(*, status="accepted", angle=16.4, std=1.2, single=False, horizontal=-2.3, coherence=0.8):
    """An LCMF-v1 result: the live kiosk's launch method."""
    return SimpleNamespace(
        status=status,
        accepted=status.startswith("accepted") and angle is not None,
        angle_deg=angle,
        component_std_deg=std,
        single_channel=single,
        horizontal_deg=horizontal,
        horizontal_confidence=coherence,
    )


def test_an_accepted_launch_is_shown_up_and_sideways():
    line = recorder.radar_line(measurement(), launch=lcmf(), tee_known=True)

    assert line.startswith("ball 101.4 mph, launch 16.4 deg up, -2.3 deg sideways")
    assert "41 points" in line


def test_a_withheld_launch_says_why():
    line = recorder.radar_line(
        measurement(), launch=lcmf(status="rejected_track", angle=None), tee_known=True
    )

    assert "launch withheld (rejected_track)" in line


def test_without_a_tee_range_the_line_asks_for_one():
    line = recorder.radar_line(measurement(), tee_known=False)

    assert "launch needs --tee-m" in line


def test_raw_speed_is_used_when_there_is_no_corrected_speed():
    assert recorder.radar_line(measurement(corrected=None), tee_known=True).startswith(
        "ball 99.0 mph"
    )


def test_no_ball_and_low_confidence_are_said_plainly():
    assert recorder.radar_line(measurement(found=False), tee_known=True) == (
        "no ball in the radar data"
    )
    assert "LOW CONFIDENCE" in recorder.radar_line(measurement(low=True), tee_known=True)


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


def test_a_credible_ball_without_a_launch_is_sent_as_speed_only():
    assert recorder.shot_payload(measurement()) == {"ball_speed_mph": 101.4}


def test_an_accepted_launch_is_sent_with_its_evidence():
    assert recorder.shot_payload(measurement(), launch=lcmf()) == {
        "ball_speed_mph": 101.4,
        "launch_angle_vertical": 16.4,
        "vertical_single_channel": False,
        "vertical_component_std_deg": 1.2,
        "launch_angle_horizontal": -2.3,
        "horizontal_coherence": 0.8,
    }


def test_a_withheld_launch_sends_speed_only():
    payload = recorder.shot_payload(measurement(), launch=lcmf(status="rejected_track", angle=None))

    assert payload == {"ball_speed_mph": 101.4}


def test_missing_launch_evidence_is_left_out_not_invented():
    payload = recorder.shot_payload(
        measurement(), launch=lcmf(std=None, single=True, horizontal=None, coherence=None)
    )

    assert payload == {
        "ball_speed_mph": 101.4,
        "launch_angle_vertical": 16.4,
        "vertical_single_channel": True,
    }


@pytest.mark.parametrize("horizontal", [45.1, -60.0])
def test_a_horizontal_the_kiosk_would_refuse_is_dropped_not_the_shot(horizontal):
    payload = recorder.shot_payload(measurement(), launch=lcmf(horizontal=horizontal))

    assert "launch_angle_horizontal" not in payload
    assert "horizontal_coherence" not in payload
    assert payload["launch_angle_vertical"] == 16.4


@pytest.mark.parametrize(
    "kwargs",
    [{"found": False}, {"quality": "reject"}, {"low": True}],
)
def test_doubtful_readings_never_reach_the_screen(kwargs):
    assert recorder.shot_payload(measurement(**kwargs), launch=lcmf()) is None


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


def radar_worker(tee_m):
    """A RadarWorker without a radar: only what measure_launch reads."""
    worker = object.__new__(recorder.RadarWorker)
    worker.args = SimpleNamespace(tee_m=tee_m, club="7i", net_m=4.0)
    worker.calibration = "calibration"
    worker.tx_order = "normal"
    return worker


def test_launch_is_measured_with_the_radial_track_speed(monkeypatch):
    import openflight.iwr6843.lcmf as lcmf_module

    calls = []
    result = lcmf()
    monkeypatch.setattr(
        lcmf_module,
        "estimate_lcmf_v1",
        lambda raw, cal, **kwargs: calls.append((raw, cal, kwargs)) or result,
    )
    capture = SimpleNamespace(raw=b"dump", sequence=3)

    assert radar_worker(1.4).measure_launch(capture, measurement()) is result
    assert calls == [
        (
            b"dump",
            "calibration",
            # The live path feeds LCMF the OPS243's radial speed; the range walk's
            # radial speed (not the cosine-corrected one) stands in for it.
            {"ball_speed_mph": 99.0, "club": "7i", "net_range_m": 4.0, "tx_order": "normal"},
        )
    ]


@pytest.mark.parametrize(
    ("tee_m", "kwargs"),
    [(None, {}), (1.4, {"found": False}), (1.4, {"quality": "reject"}), (1.4, {"low": True})],
)
def test_launch_is_not_measured_without_a_tee_range_or_a_credible_ball(monkeypatch, tee_m, kwargs):
    import openflight.iwr6843.lcmf as lcmf_module

    monkeypatch.setattr(
        lcmf_module, "estimate_lcmf_v1", lambda *a, **k: pytest.fail("LCMF must not run")
    )
    capture = SimpleNamespace(raw=b"dump", sequence=3)

    assert radar_worker(tee_m).measure_launch(capture, measurement(**kwargs)) is None


def test_a_launch_analysis_crash_costs_only_the_angle(monkeypatch, capsys):
    import openflight.iwr6843.lcmf as lcmf_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("bad dump")

    monkeypatch.setattr(lcmf_module, "estimate_lcmf_v1", boom)
    capture = SimpleNamespace(raw=b"dump", sequence=3)

    assert radar_worker(1.4).measure_launch(capture, measurement()) is None
    assert "launch analysis failed: bad dump" in capsys.readouterr().out
