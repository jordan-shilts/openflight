"""The swing recorder's OPS243 club speed hook (--ops243-club-speed).

Every trigger the TI radar takes also goes to the OPS243 worker; each swing's
TI result then picks up the OPS club speed when its gates pass, logs the
outcome in radar.jsonl, and never waits long or loses the shot over it.
"""

import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from openflight.iwr6843 import Calibration
from openflight.measured_shot import parse_measured_shot
from openflight.ops243_club_speed import ClubSpeedVerdict, OPS243ClubSpeed, OPSCapture

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "hardware-test" / "camera_motion_capture.py"
)
spec = importlib.util.spec_from_file_location("camera_motion_capture_ops243", SCRIPT)
recorder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recorder)


# -- options -----------------------------------------------------------------------


def test_club_speed_is_off_by_default_on_the_header_uart():
    args = recorder.parse_args(["--iwr6843"])

    assert args.ops243_club_speed is False
    assert args.ops243_port == "/dev/ttyAMA0"
    assert args.ops243_pre_trigger_segments == 16
    assert args.ops243_transmit_power == 3


def test_club_speed_is_switched_on_with_its_port():
    args = recorder.parse_args(
        ["--iwr6843", "--ops243-club-speed", "--ops243-port", "/dev/ttyACM0"]
    )

    assert args.ops243_club_speed is True
    assert args.ops243_port == "/dev/ttyACM0"


@pytest.mark.parametrize(
    "argv",
    [
        ["--ops243-club-speed"],  # needs the TI ball to check against
        ["--iwr6843", "--ops243-pre-trigger-segments", "0"],
        ["--iwr6843", "--ops243-pre-trigger-segments", "33"],
        ["--iwr6843", "--ops243-transmit-power", "8"],
    ],
)
def test_impossible_club_speed_options_are_refused(argv):
    with pytest.raises(SystemExit):
        recorder.parse_args(argv)


# -- the trigger path ---------------------------------------------------------------


class FakeOps:
    def __init__(self, verdict=None, error=None):
        self.triggers = []
        self.asked = []
        self.verdict = verdict or ClubSpeedVerdict(None, "no OPS capture for this swing")
        self.error = error

    def trigger(self, timestamp):
        self.triggers.append(timestamp)
        return True

    def verdict_for(self, timestamp, ti_ball_mph, *, club=None, wait_s=1.0):
        self.asked.append(
            {"timestamp": timestamp, "ti_ball": ti_ball_mph, "club": club, "wait_s": wait_s}
        )
        if self.error is not None:
            raise self.error
        return self.verdict


def test_a_trigger_the_ti_takes_also_goes_to_the_ops():
    ops = FakeOps()
    monitor = SimpleNamespace(notify_trigger=lambda timestamp: True)

    assert recorder.radar_trigger(monitor, None, ops)(12.5) is True

    assert ops.triggers == [12.5]


def test_a_trigger_the_ti_refuses_leaves_the_ops_armed_for_the_next_swing():
    ops = FakeOps()
    monitor = SimpleNamespace(notify_trigger=lambda timestamp: False)

    assert recorder.radar_trigger(monitor, None, ops)(12.5) is False

    assert ops.triggers == []


def test_without_the_ops_the_trigger_is_as_before():
    seen = []
    monitor = SimpleNamespace(notify_trigger=lambda timestamp: seen.append(timestamp) or True)

    assert recorder.radar_trigger(monitor, None)(12.5) is True
    assert seen == [12.5]


def test_a_sound_edge_also_goes_to_the_ops():
    ops = FakeOps()
    camera = SimpleNamespace(sound_trigger=lambda timestamp: None)

    recorder.sound_trigger({"worker": camera}, None, ops)(7.25)

    assert ops.triggers == [7.25]


def test_the_camera_loop_hands_the_trigger_over_without_waiting_on_the_ops():
    """The real worker, never started: trigger() only takes a lock."""
    ops = OPS243ClubSpeed("/dev/ttyAMA0", radar_factory=lambda port: None)
    monitor = SimpleNamespace(notify_trigger=lambda timestamp: True)
    on_trigger = recorder.radar_trigger(monitor, None, ops)

    began = time.perf_counter()
    assert on_trigger(time.time()) is True
    assert time.perf_counter() - began < 0.01


# -- each swing's result ------------------------------------------------------------


def measurement(*, found=True, corrected=120.4, low=False, quality="ok"):
    track = SimpleNamespace(n_inliers=41, low_confidence=low, speed_mph=117.0)
    shot = SimpleNamespace(
        ball_found=found,
        track=track if found else None,
        ball_speed_mph=117.0,
        ball_speed_corrected_mph=corrected,
        quality=quality,
    )
    shot.to_dict = lambda: {"ball_found": found}
    return shot


def radar_worker(tmp_path, monkeypatch, ops, *, shot=None):
    """A RadarWorker without a radar, posting to a list."""
    import openflight.iwr6843.lcmf as lcmf_module
    import openflight.iwr6843.shot as shot_module

    worker = object.__new__(recorder.RadarWorker)
    worker.args = SimpleNamespace(
        tee_m=None,
        club="7-iron",
        net_m=4.0,
        club_from=None,
        rig_setup_url=None,
        horizontal_method="colocated",
        post_to="http://kiosk/api/shots/measured",
        no_camera_confirm=False,
    )
    worker.calibration = Calibration(
        elem_correction=np.ones(8, dtype=complex),
        tilt_rad=0.17,
        range_bias_m=0.0,
        tee_range_m=None,
        tee_ball_height_m=0.04,
    )
    worker.tx_order = "normal"
    worker.ball_sizes = None
    worker.case_pitch = None
    worker.kiosk_status = None
    worker.camera_launch = None
    worker.ops_club_speed = ops
    worker.results_path = tmp_path / "radar.jsonl"
    posted = []
    monkeypatch.setattr(
        shot_module, "process_dump", lambda raw, calibration, **kw: shot or measurement()
    )
    monkeypatch.setattr(lcmf_module, "estimate_lcmf_v1", lambda *a, **k: None)
    monkeypatch.setattr(recorder, "post_shot", lambda url, payload: posted.append(payload) or "ok")
    return worker, posted


def swing_capture(timestamp=1795149156.926):
    return SimpleNamespace(
        sequence=3,
        trigger_timestamp=timestamp,
        dump_duration_s=2.4,
        path=None,
        valid=True,
        raw=b"dump",
        error=None,
    )


def logged(tmp_path):
    return json.loads((tmp_path / "radar.jsonl").read_text())


GOOD = ClubSpeedVerdict(
    86.2, "ok", ops_ball_mph=118.9, ti_ball_mph=120.4, smash=120.4 / 86.2, capture={"status": "ok"}
)


def test_an_agreeing_ops_puts_its_club_speed_in_the_kiosk_payload(tmp_path, monkeypatch, capsys):
    ops = FakeOps(GOOD)
    worker, posted = radar_worker(tmp_path, monkeypatch, ops)

    assert worker.measure(swing_capture()) == "shot"

    assert posted == [
        {"ball_speed_mph": 120.4, "club_speed_mph": 86.2, "club_speed_source": "ops243"}
    ]
    parse_measured_shot(posted[0])  # the kiosk takes it
    assert ops.asked == [
        {
            "timestamp": 1795149156.926,
            "ti_ball": 120.4,
            "club": "7-iron",
            "wait_s": recorder.OPS_RESULT_WAIT_S,
        }
    ]
    record = logged(tmp_path)["ops243"]
    assert record["club_speed_mph"] == 86.2
    assert record["reason"] == "ok"
    assert "OPS #3: club 86.2 mph (OPS ball 118.9 vs TI 120.4 mph, smash 1.40)" in (
        capsys.readouterr().out
    )


def test_a_refused_club_speed_still_posts_the_shot_and_logs_why(tmp_path, monkeypatch):
    refused = ClubSpeedVerdict(
        None, "ball speeds disagree: OPS 80.9 vs TI 120.4 mph (more than 6.0)"
    )
    worker, posted = radar_worker(tmp_path, monkeypatch, FakeOps(refused))

    assert worker.measure(swing_capture()) == "shot"

    assert posted == [{"ball_speed_mph": 120.4}]
    assert logged(tmp_path)["ops243"]["reason"].startswith("ball speeds disagree")


def test_an_ops_failure_never_costs_the_shot(tmp_path, monkeypatch, capsys):
    worker, posted = radar_worker(tmp_path, monkeypatch, FakeOps(error=RuntimeError("wedged")))

    assert worker.measure(swing_capture()) == "shot"

    assert posted == [{"ball_speed_mph": 120.4}]
    assert logged(tmp_path)["ops243"] == {"club_speed_mph": None, "reason": "failed: wedged"}
    assert "club speed check failed: wedged" in capsys.readouterr().out


def test_a_swing_with_no_ball_does_not_wait_for_the_ops_but_logs_it(tmp_path, monkeypatch):
    ops = FakeOps(ClubSpeedVerdict(None, "no TI ball speed to check the OPS against"))
    worker, posted = radar_worker(tmp_path, monkeypatch, ops, shot=measurement(found=False))

    assert worker.measure(swing_capture()) == "no_ball"

    assert posted == []
    assert ops.asked[0]["wait_s"] == 0.0
    assert ops.asked[0]["ti_ball"] is None
    assert logged(tmp_path)["ops243"]["reason"] == "no TI ball speed to check the OPS against"


def test_without_the_ops_nothing_changes(tmp_path, monkeypatch):
    worker, posted = radar_worker(tmp_path, monkeypatch, None)
    del worker.ops_club_speed  # as the older tests build it

    assert worker.measure(swing_capture()) == "shot"

    assert posted == [{"ball_speed_mph": 120.4}]
    assert "ops243" not in logged(tmp_path)


def test_the_ops_result_for_this_swing_reaches_the_kiosk_through_the_real_worker(
    tmp_path, monkeypatch
):
    """The OPS worker's own matching and gates, fed a finished capture."""
    ops = OPS243ClubSpeed("/dev/ttyAMA0", radar_factory=lambda port: None)
    swing = 1795149156.926
    ops._keep(  # pylint: disable=protected-access
        OPSCapture(swing, "ok", ball_speed_mph=118.9, club_speed_mph=86.24), rearmed=True
    )
    worker, posted = radar_worker(tmp_path, monkeypatch, ops)

    worker.measure(swing_capture(swing))

    assert posted[0]["club_speed_mph"] == 86.2
    assert posted[0]["club_speed_source"] == "ops243"


# -- a missing OPS243 ---------------------------------------------------------------


def test_a_missing_ops_warns_once_and_the_recorder_runs_on(tmp_path, monkeypatch, capsys):
    """No OPS on the port: one warning, then every swing still posts its ball."""
    args = recorder.parse_args(
        ["--iwr6843", "--ops243-club-speed", "--ops243-port", str(tmp_path / "no-such-tty")]
    )
    ops = recorder.start_ops243(args, tmp_path)
    try:
        ops.join(timeout=10.0)
        assert not ops.is_alive()
        assert not ops.enabled
        monitor = SimpleNamespace(notify_trigger=lambda timestamp: True)
        assert recorder.radar_trigger(monitor, None, ops)(1795149156.926) is True
        worker, posted = radar_worker(tmp_path, monkeypatch, ops)
        assert worker.measure(swing_capture()) == "shot"
    finally:
        ops.stop()

    assert posted == [{"ball_speed_mph": 120.4}]
    assert logged(tmp_path)["ops243"]["reason"].startswith("OPS243 off: no OPS243 on")
    out = capsys.readouterr().out
    assert out.count("WARNING: OPS243 club speed off") == 1
    assert not (tmp_path / "ops243.jsonl").exists()
