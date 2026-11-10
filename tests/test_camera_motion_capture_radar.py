"""Camera-triggered TI radar capture in the camera-only swing recorder."""

import importlib.util
import io
import json
import math
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from openflight.iwr6843 import Calibration

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


def lcmf(
    *,
    status="accepted",
    angle=16.4,
    std=1.2,
    single=False,
    horizontal=-2.3,
    coherence=0.8,
    phase=None,
):
    """An LCMF-v1 result: the live kiosk's launch method."""
    return SimpleNamespace(
        horizontal_phase_rad=phase,
        to_dict=lambda: {"status": status},
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

    assert "launch needs a ball distance" in line


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


def test_radar_options_default_to_the_fast_ball_profile_and_reference_calibration():
    args = recorder.parse_args(["--iwr6843"])

    assert args.iwr6843 is True
    assert args.iwr6843_config.name == "iwr6843_l3dump_ball_16f3ms_53bin_iq8.cfg"
    assert args.iwr6843_config.exists()
    # Floor reflections defeat the midpoint method on a radar this low.
    assert args.horizontal_method == "colocated"
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
    worker.args = SimpleNamespace(
        tee_m=tee_m,
        club="7i",
        net_m=4.0,
        club_from=None,
        rig_setup_url=None,
        horizontal_method="colocated",
    )
    worker.calibration = SimpleNamespace(tee_range_m=tee_m)
    worker.tx_order = "normal"
    worker.ball_sizes = None
    worker.case_pitch = None
    worker.kiosk_status = None
    return worker


def launch_for(worker, capture, shot, club="7-iron"):
    return worker.measure_launch(
        capture, shot, club=club, calibration=worker.calibration, net_m=4.0
    )


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

    worker = radar_worker(1.4)
    assert launch_for(worker, capture, measurement()) is result
    assert calls == [
        (
            b"dump",
            worker.calibration,
            # The live path feeds LCMF the OPS243's radial speed; the range walk's
            # radial speed (not the cosine-corrected one) stands in for it.
            {
                "ball_speed_mph": 99.0,
                "club": "7-iron",
                "net_range_m": 4.0,
                "tx_order": "normal",
                "horizontal_method": "colocated",
                "horizontal_phase_reference_rad": None,
            },
        )
    ]


def test_the_rigs_straight_line_and_method_reach_the_estimator(monkeypatch):
    import openflight.iwr6843.lcmf as lcmf_module

    seen = {}
    monkeypatch.setattr(
        lcmf_module, "estimate_lcmf_v1", lambda raw, cal, **kwargs: seen.update(kwargs) or lcmf()
    )
    worker = radar_worker(1.4)
    worker.args.horizontal_method = "midpoint"

    worker.measure_launch(
        SimpleNamespace(raw=b"dump", sequence=3),
        measurement(),
        club="7-iron",
        calibration=worker.calibration,
        net_m=4.0,
        horizontal_zero_rad=-0.82,
    )

    assert seen["horizontal_method"] == "midpoint"
    assert seen["horizontal_phase_reference_rad"] == pytest.approx(-0.82)


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

    assert launch_for(radar_worker(tee_m), capture, measurement(**kwargs)) is None


def test_a_launch_analysis_crash_costs_only_the_angle(monkeypatch, capsys):
    import openflight.iwr6843.lcmf as lcmf_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("bad dump")

    monkeypatch.setattr(lcmf_module, "estimate_lcmf_v1", boom)
    capture = SimpleNamespace(raw=b"dump", sequence=3)

    assert launch_for(radar_worker(1.4), capture, measurement()) is None
    assert "launch analysis failed: bad dump" in capsys.readouterr().out


def test_the_kiosk_club_is_read_from_its_api():
    seen = {}

    def opener(url, timeout):
        seen.update(url=url, timeout=timeout)
        return FakeResponse(b'{"club": "7-iron", "status": "current"}')

    assert recorder.kiosk_club("http://127.0.0.1:8080/api/club", opener=opener) == "7-iron"
    assert seen == {"url": "http://127.0.0.1:8080/api/club", "timeout": 2.0}


@pytest.mark.parametrize(
    "reply",
    [b"<html>", b"[]", b"{}", b'{"club": ""}', b'{"club": 7}', b'{"club": null}'],
)
def test_an_unreadable_kiosk_club_is_none(reply):
    def opener(url, timeout):
        return FakeResponse(reply)

    assert recorder.kiosk_club("http://x/api/club", opener=opener) is None


@pytest.mark.parametrize(
    "error", [urllib.error.URLError("refused"), TimeoutError("slow"), OSError("down")]
)
def test_an_unreachable_kiosk_club_is_none(error):
    def opener(url, timeout):
        raise error

    assert recorder.kiosk_club("http://x/api/club", opener=opener) is None


def test_each_swing_uses_the_kiosk_club_and_falls_back_to_the_flag(monkeypatch):
    worker = radar_worker(1.4)
    worker.args.club_from = "http://127.0.0.1:8080/api/club"
    replies = iter(["7-iron", None])
    monkeypatch.setattr(recorder, "kiosk_club", lambda url: next(replies))

    assert worker.current_club() == "7-iron"
    assert worker.current_club() == "7i"  # kiosk unreadable: the --club flag


def test_without_a_kiosk_url_the_flag_club_is_used(monkeypatch):
    monkeypatch.setattr(recorder, "kiosk_club", lambda url: pytest.fail("must not call the kiosk"))

    assert radar_worker(1.4).current_club() == "7i"


GEOMETRY = {
    "setup_id": "course",
    "ball_distance_m": 1.5,
    "distance_source": "camera",
    "tee_range_m": 1.52,
    "net_range_m": None,
    "radar_height_m": 0.2,
    "ball_height_m": 0.021,
    "tilt_offset_deg": 2.5,
}


def test_rig_geometry_sends_the_readings_and_returns_the_setup():
    sent = {}

    def opener(request, timeout):
        sent.update(body=json.loads(request.data), timeout=timeout)
        return FakeResponse(json.dumps(GEOMETRY).encode())

    geometry = recorder.rig_geometry(
        "http://x/obs", diameter_px=15.2, pitch_deg=-3.0, opener=opener
    )

    assert geometry == GEOMETRY
    assert sent == {"body": {"diameter_px": 15.2, "pitch_deg": -3.0}, "timeout": 3.0}


@pytest.mark.parametrize("reply", [b"<html>", b"[]", b"{}"])
def test_an_unusable_setup_reply_is_none(reply):
    def opener(request, timeout):
        return FakeResponse(reply)

    assert (
        recorder.rig_geometry("http://x", diameter_px=None, pitch_deg=None, opener=opener) is None
    )


@pytest.mark.parametrize(
    "error", [urllib.error.URLError("refused"), TimeoutError("slow"), OSError("down")]
)
def test_an_unreachable_kiosk_means_no_setup(error):
    def opener(request, timeout):
        raise error

    assert recorder.rig_geometry("http://x", diameter_px=1, pitch_deg=1, opener=opener) is None


def test_ball_widths_are_matched_to_the_swing_by_trigger_time():
    sizes = recorder.BallSizes()
    sizes.add(100.0, 14.0)
    sizes.add(110.0, 15.0)
    sizes.add(120.0, None)

    assert sizes.near(100.1) == 14.0
    assert sizes.near(109.5) == 15.0
    assert sizes.near(120.0) is None  # that clip saw no ball leave
    assert sizes.near(150.0) is None  # no clip near this swing
    assert recorder.BallSizes().near(1.0) is None


class FakeSensor:
    def __init__(self, x=0.0, fail=False):
        self.x = x
        self.fail = fail

    def read(self):
        if self.fail:
            raise OSError("bus gone")
        return SimpleNamespace(x_g=self.x, y_g=0.0, z_g=1.0)


def test_the_case_tilt_is_read_on_the_configured_axis():
    pitch = recorder.CasePitch("x", sensor=FakeSensor(x=0.1), samples=3).read()

    assert pitch == pytest.approx(math.degrees(math.atan2(0.1, 1.0)))


def test_a_failing_tilt_sensor_means_no_reading(capsys):
    assert recorder.CasePitch("x", sensor=FakeSensor(fail=True), samples=3).read() is None
    assert "Case tilt read failed" in capsys.readouterr().out


def test_a_missing_tilt_sensor_means_no_reading(monkeypatch, capsys):
    import openflight.inclinometer as inclinometer

    def absent(**_kwargs):
        raise OSError("no i2c")

    monkeypatch.setattr(inclinometer, "detect_accelerometer", absent)

    assert recorder.CasePitch("x").read() is None
    assert "Case tilt sensor unavailable" in capsys.readouterr().out


def real_calibration(tee=None):
    return Calibration(
        elem_correction=np.ones(8, dtype=complex),
        tilt_rad=math.radians(10.0),
        range_bias_m=0.0,
        tee_range_m=tee,
        tee_ball_height_m=0.04,
    )


def test_a_swing_uses_the_kiosks_distance_tilt_and_heights():
    worker = radar_worker(None)
    worker.calibration = real_calibration()

    calibration = worker.calibration_for(GEOMETRY)

    assert calibration.tee_range_m == pytest.approx(1.52)
    assert math.degrees(calibration.tilt_rad) == pytest.approx(12.5)
    assert calibration.radar_height_m == pytest.approx(0.2)
    assert calibration.tee_ball_height_m == pytest.approx(0.021)
    assert worker.calibration.tee_range_m is None  # the base is not overwritten


def test_with_no_answer_from_the_kiosk_the_flags_apply():
    worker = radar_worker(1.4)
    worker.calibration = real_calibration(1.4)

    assert worker.calibration_for(None) is worker.calibration


def test_the_kiosk_hears_each_swings_camera_and_tilt_readings(monkeypatch):
    worker = radar_worker(None)
    worker.args.rig_setup_url = "http://x/obs"
    worker.ball_sizes = recorder.BallSizes()
    worker.ball_sizes.add(50.0, 14.5)
    worker.case_pitch = SimpleNamespace(read=lambda: -2.5)
    seen = {}

    def fake(url, *, diameter_px, pitch_deg):
        seen.update(url=url, diameter_px=diameter_px, pitch_deg=pitch_deg)
        return GEOMETRY

    monkeypatch.setattr(recorder, "rig_geometry", fake)

    geometry = worker.swing_geometry(SimpleNamespace(trigger_timestamp=50.1))

    assert geometry is GEOMETRY
    assert seen == {"url": "http://x/obs", "diameter_px": 14.5, "pitch_deg": -2.5}


def test_without_a_kiosk_url_nothing_is_sent(monkeypatch):
    monkeypatch.setattr(recorder, "rig_geometry", lambda *a, **k: pytest.fail("must not post"))

    assert radar_worker(1.4).swing_geometry(SimpleNamespace(trigger_timestamp=1.0)) is None


def worker_for_handle(tmp_path, geometry, monkeypatch):
    import openflight.iwr6843.lcmf as lcmf_module
    import openflight.iwr6843.shot as shot_module

    worker = radar_worker(None)
    worker.calibration = real_calibration()
    worker.results_path = tmp_path / "radar.jsonl"
    worker.args.post_to = None
    worker.args.tee_m = None
    worker.args.rig_setup_url = "http://x/obs" if geometry is not None else None
    used = {}

    def fake_dump(raw, calibration, **kwargs):
        used.update(calibration=calibration, net=kwargs["net_range_m"])
        shot = measurement()
        shot.to_dict = lambda: {"ball_found": True}
        return shot

    monkeypatch.setattr(shot_module, "process_dump", fake_dump)
    monkeypatch.setattr(lcmf_module, "estimate_lcmf_v1", lambda *a, **k: None)
    monkeypatch.setattr(recorder, "rig_geometry", lambda *a, **k: geometry)
    return worker, used


def capture_for_handle():
    return SimpleNamespace(
        sequence=4,
        trigger_timestamp=10.0,
        dump_duration_s=7.1,
        path=None,
        valid=True,
        raw=b"dump",
        error=None,
    )


def test_a_swing_runs_with_the_setups_geometry_and_no_net(tmp_path, monkeypatch):
    worker, used = worker_for_handle(tmp_path, GEOMETRY, monkeypatch)

    worker.handle(capture_for_handle())

    assert used["calibration"].tee_range_m == pytest.approx(1.52)
    assert used["net"] is None
    logged = json.loads((tmp_path / "radar.jsonl").read_text())
    assert logged["rig"]["distance_source"] == "camera"


def test_a_swing_without_a_kiosk_falls_back_to_the_flags(tmp_path, monkeypatch):
    worker, used = worker_for_handle(tmp_path, None, monkeypatch)

    worker.handle(capture_for_handle())

    assert used["calibration"] is worker.calibration
    assert used["net"] == pytest.approx(4.0)


# -- telling the kiosk ready / measuring ----------------------------------


def status_reporter(**kwargs):
    sent = []

    def opener(request, timeout):
        sent.append({"url": request.full_url, "body": json.loads(request.data), "timeout": timeout})
        return FakeResponse(b"{}")

    reporter = recorder.KioskStatus(
        "http://127.0.0.1:8080/api/shots/measuring", opener=opener, **kwargs
    )
    return reporter, sent


def run_until(reporter, sent, count, timeout_s=2.0):
    """Run the reporter thread until it has posted ``count`` reports."""
    import time

    reporter.start()
    deadline = time.monotonic() + timeout_s
    while len(sent) < count and time.monotonic() < deadline:
        time.sleep(0.005)
    reporter.stop_event.set()
    reporter.join(timeout=2.0)


def test_reports_reach_the_kiosk_in_order_with_the_expected_wait():
    reporter, sent = status_reporter()
    reporter.ready()
    reporter.measuring()
    reporter.ready("no_ball")

    run_until(reporter, sent, 3)

    assert [item["body"] for item in sent] == [
        {"state": "ready"},
        {"state": "measuring", "expected_s": 7.5},
        {"state": "ready", "result": "no_ball"},
    ]
    assert sent[0]["url"] == "http://127.0.0.1:8080/api/shots/measuring"
    assert sent[0]["timeout"] == 2.0


def test_the_countdown_follows_the_last_real_dump():
    reporter, sent = status_reporter()
    reporter.learn_wait(5.3)
    reporter.measuring()

    run_until(reporter, sent, 1)

    assert sent[0]["body"] == {"state": "measuring", "expected_s": 5.8}


def test_the_first_countdown_comes_from_the_capture_profile():
    reporter, sent = status_reporter(dump_s=2.34)
    reporter.measuring()

    run_until(reporter, sent, 1)

    assert sent[0]["body"] == {"state": "measuring", "expected_s": 2.8}


@pytest.mark.parametrize("dump_s", [0.0, 0.2, 45.0])
def test_an_implausible_dump_time_does_not_move_the_countdown(dump_s):
    reporter, _sent = status_reporter()

    reporter.learn_wait(dump_s)

    assert reporter.expected_s == pytest.approx(recorder.KioskStatus.DEFAULT_EXPECTED_S)


def test_the_current_state_is_repeated_as_a_heartbeat_without_the_result():
    reporter, sent = status_reporter(heartbeat_s=0.02)
    reporter.ready("shot")

    run_until(reporter, sent, 3)

    assert sent[0]["body"] == {"state": "ready", "result": "shot"}
    assert all(item["body"] == {"state": "ready"} for item in sent[1:3])


def test_nothing_is_sent_before_the_first_report():
    reporter, sent = status_reporter(heartbeat_s=0.01)

    run_until(reporter, sent, 1, timeout_s=0.1)

    assert sent == []


@pytest.mark.parametrize(
    "error",
    [urllib.error.URLError("refused"), TimeoutError("timed out"), ConnectionResetError("reset")],
)
def test_an_unreachable_kiosk_does_not_stop_the_recorder(error):
    def opener(request, timeout):
        raise error

    reporter = recorder.KioskStatus("http://127.0.0.1:8080/x", opener=opener)

    assert reporter.post({"state": "ready"}) is False


def test_closing_says_offline_before_stopping():
    reporter, sent = status_reporter()
    reporter.ready()
    reporter.start()

    reporter.close()

    assert [item["body"]["state"] for item in sent] == ["ready", "offline"]
    assert not reporter.is_alive()


class FakeStatus:
    def __init__(self):
        self.calls = []

    def measuring(self):
        self.calls.append("measuring")

    def ready(self, result=None):
        self.calls.append(("ready", result))

    def learn_wait(self, dump_s):
        self.calls.append(("learn", dump_s))


def test_a_swing_the_radar_takes_starts_the_wait_on_screen():
    status = FakeStatus()
    monitor = SimpleNamespace(notify_trigger=lambda timestamp: True)

    assert recorder.radar_trigger(monitor, status)(12.5) is True

    assert status.calls == ["measuring"]


def test_a_swing_during_a_dump_changes_nothing_on_screen():
    status = FakeStatus()
    monitor = SimpleNamespace(notify_trigger=lambda timestamp: False)

    assert recorder.radar_trigger(monitor, status)(12.5) is False

    assert status.calls == []


def test_the_trigger_works_without_a_kiosk_to_tell():
    seen = []
    monitor = SimpleNamespace(notify_trigger=lambda timestamp: seen.append(timestamp) or True)

    assert recorder.radar_trigger(monitor, None)(12.5) is True
    assert seen == [12.5]


def test_a_measured_swing_ends_the_wait_with_a_shot(tmp_path, monkeypatch):
    worker, _used = worker_for_handle(tmp_path, None, monkeypatch)
    worker.kiosk_status = FakeStatus()

    worker.handle(capture_for_handle())

    assert worker.kiosk_status.calls == [("learn", 7.1), ("ready", "shot")]


def test_a_swing_with_no_ball_ends_the_wait_saying_so(tmp_path, monkeypatch):
    import openflight.iwr6843.shot as shot_module

    worker, _used = worker_for_handle(tmp_path, None, monkeypatch)
    worker.kiosk_status = FakeStatus()

    def no_ball(raw, calibration, **kwargs):
        shot = measurement(found=False)
        shot.to_dict = lambda: {"ball_found": False}
        return shot

    monkeypatch.setattr(shot_module, "process_dump", no_ball)

    worker.handle(capture_for_handle())

    assert worker.kiosk_status.calls[-1] == ("ready", "no_ball")


def test_a_failed_dump_still_frees_the_screen(tmp_path, monkeypatch):
    worker, _used = worker_for_handle(tmp_path, None, monkeypatch)
    worker.kiosk_status = FakeStatus()
    capture = capture_for_handle()
    capture.valid = False
    capture.error = "short dump"

    worker.handle(capture)

    # A failed dump's duration says nothing about the next wait.
    assert worker.kiosk_status.calls == [("ready", "failed")]


def test_a_crash_while_measuring_still_frees_the_screen(tmp_path, monkeypatch):
    worker, _used = worker_for_handle(tmp_path, None, monkeypatch)
    worker.kiosk_status = FakeStatus()
    monkeypatch.setattr(worker, "current_club", lambda: (_ for _ in ()).throw(RuntimeError("x")))

    with pytest.raises(RuntimeError):
        worker.handle(capture_for_handle())

    assert worker.kiosk_status.calls[-1] == ("ready", "failed")


def test_the_status_url_is_optional():
    assert recorder.parse_args([]).status_to is None
    args = recorder.parse_args(["--status-to", "http://127.0.0.1:8080/api/shots/measuring"])
    assert args.status_to == "http://127.0.0.1:8080/api/shots/measuring"


# -- horizontal launch and the rig's straight line ------------------------


def test_an_unzeroed_horizontal_is_held_back_but_its_phase_goes_along():
    payload = recorder.shot_payload(
        measurement(), launch=lcmf(horizontal=15.2, phase=-0.82), horizontal_zeroed=False
    )

    # About 15 degrees off until the straight line is set, so not shown.
    assert "launch_angle_horizontal" not in payload
    assert "horizontal_coherence" not in payload
    # The kiosk sets that line from the raw phase.
    assert payload["horizontal_phase_rad"] == pytest.approx(-0.82)
    assert payload["launch_angle_vertical"] == 16.4


def test_a_zeroed_horizontal_is_sent_with_its_phase():
    payload = recorder.shot_payload(measurement(), launch=lcmf(horizontal=-1.4, phase=-0.79))

    assert payload["launch_angle_horizontal"] == -1.4
    assert payload["horizontal_phase_rad"] == pytest.approx(-0.79)


def test_no_phase_is_sent_without_a_launch():
    payload = recorder.shot_payload(
        measurement(), launch=lcmf(status="rejected_track", angle=None, phase=-0.8)
    )

    assert payload == {"ball_speed_mph": 101.4}


def test_the_log_line_does_not_print_an_unzeroed_angle():
    line = recorder.radar_line(
        measurement(), launch=lcmf(horizontal=15.2), tee_known=True, horizontal_zeroed=False
    )

    assert "15.2" not in line
    assert "straight line is not set" in line


def handled_payload(tmp_path, monkeypatch, geometry, launch):
    """Run one swing through handle() and return what would be posted."""
    worker, _used = worker_for_handle(tmp_path, geometry, monkeypatch)
    posted = []
    worker.args.post_to = "http://kiosk/api/shots/measured"
    monkeypatch.setattr(worker, "measure_launch", lambda *a, **k: launch)
    monkeypatch.setattr(recorder, "post_shot", lambda url, payload: posted.append(payload) or "ok")
    worker.handle(capture_for_handle())
    return posted[0]


def test_a_swing_before_the_straight_line_is_set_shows_no_horizontal(tmp_path, monkeypatch):
    payload = handled_payload(tmp_path, monkeypatch, GEOMETRY, lcmf(horizontal=15.2, phase=-0.82))

    assert "launch_angle_horizontal" not in payload
    assert payload["horizontal_phase_rad"] == pytest.approx(-0.82)


def test_a_swing_after_the_straight_line_is_set_shows_horizontal(tmp_path, monkeypatch):
    geometry = {**GEOMETRY, "horizontal_phase_reference_rad": -0.8}

    payload = handled_payload(tmp_path, monkeypatch, geometry, lcmf(horizontal=-1.1, phase=-0.78))

    assert payload["launch_angle_horizontal"] == -1.1


def test_the_midpoint_method_needs_no_zero_from_the_kiosk(tmp_path, monkeypatch):
    worker, _used = worker_for_handle(tmp_path, None, monkeypatch)
    posted = []
    worker.args.post_to = "http://kiosk/api/shots/measured"
    worker.args.horizontal_method = "midpoint"
    monkeypatch.setattr(worker, "measure_launch", lambda *a, **k: lcmf(horizontal=-2.3))
    monkeypatch.setattr(recorder, "post_shot", lambda url, payload: posted.append(payload) or "ok")

    worker.handle(capture_for_handle())

    assert posted[0]["launch_angle_horizontal"] == -2.3


# -- capture profiles and the wait ----------------------------------------

CONFIG_DIR = SCRIPT.parents[2] / "config"


@pytest.mark.parametrize(
    ("name", "nbytes"),
    [
        ("iwr6843_l3dump_wide_24f3ms_53bin_iq16.cfg", 732_672),
        ("iwr6843_l3dump_dense_36f2ms_53bin_iq8.cfg", 549_504),
        ("iwr6843_l3dump_ball_16f3ms_53bin_iq16.cfg", 488_448),
        ("iwr6843_l3dump_ball_16f3ms_53bin_iq8.cfg", 244_224),
    ],
)
def test_the_wait_is_the_profiles_sample_bytes_at_the_uart_rate(name, nbytes):
    seconds = recorder.dump_seconds_from_config(CONFIG_DIR / name)

    assert seconds == pytest.approx(nbytes * 10 / 1_041_667, rel=1e-6)


def test_the_fast_profile_is_a_third_of_the_default_wait():
    fast = recorder.dump_seconds_from_config(
        CONFIG_DIR / "iwr6843_l3dump_ball_16f3ms_53bin_iq8.cfg"
    )
    default = recorder.dump_seconds_from_config(
        CONFIG_DIR / "iwr6843_l3dump_wide_24f3ms_53bin_iq16.cfg"
    )

    assert fast == pytest.approx(default / 3, rel=0.01)
    assert fast < 2.5


def test_the_ball_profiles_keep_the_default_profiles_flight_frames():
    def capture_plan(name):
        for line in (CONFIG_DIR / name).read_text().splitlines():
            if line.startswith("phaseCaptureCfg"):
                return [int(value) for value in line.split()[1:]]
        raise AssertionError("no phaseCaptureCfg")

    default = capture_plan("iwr6843_l3dump_wide_24f3ms_53bin_iq16.cfg")
    for name in (
        "iwr6843_l3dump_ball_16f3ms_53bin_iq8.cfg",
        "iwr6843_l3dump_ball_16f3ms_53bin_iq16.cfg",
    ):
        plan = capture_plan(name)
        # Only the pre-trigger frame count differs; the firmware needs at least one.
        assert plan[2] == 1
        assert plan[:2] + plan[3:] == default[:2] + default[3:]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "frameCfg 0 2 12 0 3 1 0\n",
        "phaseCaptureCfg 20 53\nframeCfg 0 2 12 0 3 1 0\n",
        "frameCfg x\n",
    ],
)
def test_an_unreadable_profile_gives_no_wait_estimate(tmp_path, text):
    path = tmp_path / "profile.cfg"
    path.write_text(text)

    assert recorder.dump_seconds_from_config(path) is None


def test_a_missing_profile_gives_no_wait_estimate(tmp_path):
    assert recorder.dump_seconds_from_config(tmp_path / "missing.cfg") is None
