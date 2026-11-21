"""OPS243 club speed on the camera trigger (openflight.ops243_club_speed).

The worker owns the OPS243 serial port in rolling-buffer mode, sends ``S!`` on
each camera trigger, processes the dump with the production processor and
keeps the result by trigger time. These tests drive it three ways: the gates
on their own, the worker against a fake radar object (fast), and the real
OPS243 driver against a fake J3 UART that answers like the board, replaying a
committed TrackMan-paired rolling-buffer capture.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
import serial

import openflight.ops243  # noqa: F401  (before replay_club_speed, which stubs it when absent)
from openflight.clubs import ClubType
from openflight.measured_shot import CLUB_SPEED_SOURCES, parse_measured_shot
from openflight.ops243 import OPS243Radar
from openflight.ops243_club_speed import (
    CLUB_SPEED_SOURCE,
    ClubSpeedVerdict,
    OPS243ClubSpeed,
    OPSCapture,
    club_type_of,
    judge_club_speed,
    split_ms,
)
from openflight.rolling_buffer.processor import RollingBufferProcessor
from openflight.rolling_buffer.types import IQCapture

REPO = Path(__file__).resolve().parents[1]
SESSION = REPO / "session_logs" / "session_20260506_152406_range.jsonl"
TRACKMAN = REPO / "session_logs" / "OpenFlight-Test.Normalized.csv"

WAVELENGTH_M = 0.01243
MPS_TO_MPH = 2.23694


# -- dumps -------------------------------------------------------------------


def dump_text(i_samples, q_samples, *, sample_time=10.0, trigger_time=10.068) -> str:
    """A rolling-buffer dump as the board sends it: four JSON lines."""
    lines = [
        json.dumps({"sample_time": f"{sample_time:.3f}"}),
        json.dumps({"trigger_time": f"{trigger_time:.3f}"}),
        json.dumps({"I": [int(v) for v in i_samples]}, separators=(",", ":")),
        json.dumps({"Q": [int(v) for v in q_samples]}, separators=(",", ":")),
    ]
    return "\r\n".join(lines) + "\r\n"


def synthetic_dump(club_mph: float, ball_mph: float, *, impact_ms: float = 60.0) -> str:
    """Club approach for 35 ms, then a ball echo that fades as it flies off."""
    rng = np.random.default_rng(0)
    t_ms = np.arange(4096) / 30.0

    def tone(mph):
        return np.exp(2j * np.pi * (2 * mph / MPS_TO_MPH / WAVELENGTH_M) * t_ms / 1000.0)

    club = (t_ms >= impact_ms - 35.0) & (t_ms < impact_ms)
    ball = (t_ms >= impact_ms) & (t_ms < impact_ms + 60.0)
    signal = 400 * tone(club_mph) * club
    signal = signal + 600 * np.exp(-(t_ms - impact_ms) / 20.0) * tone(ball_mph) * ball
    i_samples = np.round(2048 + signal.real + rng.normal(0, 2, t_ms.size))
    q_samples = np.round(2048 + signal.imag + rng.normal(0, 2, t_ms.size))
    return dump_text(i_samples, q_samples)


def recorded_capture(shot_number: int) -> dict:
    """One rolling_buffer_capture from the committed TrackMan session."""
    if not SESSION.exists():
        pytest.skip(f"reference session missing: {SESSION}")
    with SESSION.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line.strip().strip("\x00"))
            except json.JSONDecodeError:
                continue
            if (
                entry.get("type") == "rolling_buffer_capture"
                and entry.get("shot_number") == shot_number
            ):
                return entry
    pytest.fail(f"shot {shot_number} not in {SESSION.name}")
    return {}


def recorded_dump(shot_number: int) -> str:
    entry = recorded_capture(shot_number)
    return dump_text(
        entry["i_samples"],
        entry["q_samples"],
        sample_time=entry["sample_time"],
        trigger_time=entry["trigger_time"],
    )


def wait_until(condition, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


# -- where the swing lands in the buffer --------------------------------------


def test_s16_keeps_half_the_136ms_buffer_on_each_side_of_the_trigger():
    pre, post = split_ms(16)

    assert pre == pytest.approx(68.27, abs=0.01)
    assert post == pytest.approx(68.27, abs=0.01)


def test_each_segment_is_128_samples_at_30_ksps():
    pre, post = split_ms(8)  # AN-027's default split: a quarter before

    assert pre == pytest.approx(34.13, abs=0.01)
    assert pre + post == pytest.approx(4096 / 30.0, abs=0.01)


def test_a_split_without_pre_trigger_history_is_refused():
    with pytest.raises(ValueError):
        OPS243ClubSpeed(pre_trigger_segments=0)


# -- the gates ------------------------------------------------------------------


def ops_capture(ball=118.0, club=85.04, status="ok", **kwargs) -> OPSCapture:
    return OPSCapture(
        100.0, status, ball_speed_mph=ball, club_speed_mph=club, trigger_offset_ms=68.0, **kwargs
    )


def test_an_agreeing_ball_and_a_physical_smash_give_the_club_speed():
    verdict = judge_club_speed(ops_capture(), 120.0)

    assert verdict.club_speed_mph == 85.0
    assert verdict.reason == "ok"
    assert verdict.smash == pytest.approx(120.0 / 85.04)
    assert verdict.capture["ball_speed_mph"] == 118.0


@pytest.mark.parametrize(
    ("ti", "ops", "agrees"),
    [
        (60.0, 63.9, True),  # 4 mph floor for slow balls
        (60.0, 64.1, False),
        (150.0, 157.4, True),  # 5% above 80 mph
        (150.0, 157.6, False),
        (150.0, 142.6, True),
        (150.0, 142.4, False),
    ],
)
def test_the_ops_ball_must_be_within_4_mph_or_5_percent_of_the_ti_ball(ti, ops, agrees):
    verdict = judge_club_speed(ops_capture(ball=ops, club=ti / 1.3), ti)

    assert (verdict.club_speed_mph is not None) is agrees
    if not agrees:
        assert "ball speeds disagree" in verdict.reason


def test_an_ops_that_only_saw_the_club_gives_no_club_speed():
    """2026-11-19: on 2 of 5 swings the OPS reported only the club (75-81 mph)."""
    verdict = judge_club_speed(ops_capture(ball=80.9, club=62.0), 115.0)

    assert verdict.club_speed_mph is None
    assert "OPS 80.9 vs TI 115.0" in verdict.reason


@pytest.mark.parametrize(
    ("club", "shown"),
    [(74.0, False), (75.5, True), (119.0, True), (121.0, False)],
)
def test_the_smash_factor_must_be_between_1_0_and_1_6(club, shown):
    verdict = judge_club_speed(ops_capture(ball=120.0, club=club), 120.0)

    assert (verdict.club_speed_mph is not None) is shown
    if not shown:
        assert verdict.reason.startswith("smash ")
        assert verdict.smash == pytest.approx(120.0 / club)


@pytest.mark.parametrize(
    ("capture", "ti", "reason"),
    [
        (None, 120.0, "no OPS capture for this swing"),
        (ops_capture(status="no_ball", ball=None, club=None), 120.0, "OPS capture no_ball"),
        (
            ops_capture(status="refused", detail="the last dump is still in flight"),
            120.0,
            "OPS capture refused: the last dump is still in flight",
        ),
        (ops_capture(), None, "no TI ball speed to check the OPS against"),
        (ops_capture(ball=None), 120.0, "OPS saw no ball"),
        (ops_capture(club=None), 120.0, "OPS found no club"),
    ],
)
def test_without_everything_the_gates_need_there_is_no_club_speed(capture, ti, reason):
    verdict = judge_club_speed(capture, ti)

    assert verdict.club_speed_mph is None
    assert verdict.reason == reason


def test_a_club_speed_the_kiosk_would_refuse_is_not_sent():
    verdict = judge_club_speed(ops_capture(ball=6.0, club=4.5), 6.0)

    assert verdict.club_speed_mph is None
    assert "outside what the kiosk takes" in verdict.reason


def test_the_payload_gets_both_club_fields_and_the_kiosk_takes_them():
    payload = judge_club_speed(ops_capture(), 120.0).add_to({"ball_speed_mph": 120.0})

    assert payload == {
        "ball_speed_mph": 120.0,
        "club_speed_mph": 85.0,
        "club_speed_source": "ops243",
    }
    swing = parse_measured_shot(payload)
    assert swing.club_speed_mph == 85.0
    assert swing.club_speed_source == "ops243"


def test_a_refused_club_speed_leaves_the_payload_alone():
    payload = {"ball_speed_mph": 120.0}

    assert judge_club_speed(ops_capture(ball=90.0), 120.0).add_to(payload) is payload


def test_the_source_label_is_one_the_kiosk_takes():
    assert CLUB_SPEED_SOURCE in CLUB_SPEED_SOURCES


def test_the_verdict_record_and_line_say_what_happened():
    verdict = judge_club_speed(ops_capture(), 120.0)

    record = verdict.to_record()
    assert record["club_speed_mph"] == 85.0
    assert record["smash"] == pytest.approx(1.411, abs=0.001)
    json.dumps(record)  # radar.jsonl-ready
    assert verdict.line() == "club 85.0 mph (OPS ball 118.0 vs TI 120.0 mph, smash 1.41)"
    assert ClubSpeedVerdict(None, "OPS saw no ball").line() == "no club speed (OPS saw no ball)"


def test_the_capture_summary_keeps_the_trigger_time_exact():
    summary = OPSCapture(1795149156.926083, "late", send_delay_ms=80.123456).to_dict()

    assert summary["trigger_timestamp"] == 1795149156.926083
    assert summary["send_delay_ms"] == 80.12
    assert "processed" not in summary


def test_club_names_map_to_club_types():
    assert club_type_of("7-iron") is ClubType.IRON_7
    assert club_type_of(" SW ") is ClubType.SW
    assert club_type_of("7i") is ClubType.UNKNOWN
    assert club_type_of(None) is ClubType.UNKNOWN


def test_a_wedge_repicks_its_club_from_the_stored_timeline():
    processor = RollingBufferProcessor()
    entry = recorded_capture(18)  # PW
    processed = processor.process_capture(
        IQCapture(
            sample_time=entry["sample_time"],
            trigger_time=entry["trigger_time"],
            i_samples=entry["i_samples"],
            q_samples=entry["q_samples"],
        )
    )
    capture = OPSCapture(
        1.0,
        "ok",
        ball_speed_mph=processed.ball_speed_mph,
        club_speed_mph=processed.club_speed_mph,
        processed=processed,
    )
    seen = []
    real = processor.find_club_speed

    def spy(*args, **kwargs):
        seen.append(kwargs["club_type"])
        return real(*args, **kwargs)

    processor.find_club_speed = spy

    assert capture.club_speed_for(None, processor) == processed.club_speed_mph
    capture.club_speed_for("sw", processor)
    assert seen == [ClubType.SW]


def _median_club_picker():
    """replay_club_speed.median_club_speed, loaded without its sys.modules stubs
    leaking into the rest of the test run."""
    keys = (
        "openflight",
        "openflight.rolling_buffer",
        "openflight.ops243",
        "openflight.rolling_buffer.processor",
    )
    saved = {key: sys.modules.get(key) for key in keys}
    spec = importlib.util.spec_from_file_location(
        "replay_club_speed_under_test", REPO / "scripts" / "analysis" / "replay_club_speed.py"
    )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
    return module.median_club_speed


def test_the_production_club_pick_beats_the_median_pick_behind_the_ball():
    """Why the club speed is the processor's own pick, not replay_club_speed's
    median: on the committed TrackMan session (OPS behind the ball) the median
    only beats the old magnitude pick it was written against."""
    analysis = str(REPO / "scripts" / "analysis")
    if analysis not in sys.path:
        sys.path.insert(0, analysis)
    if not TRACKMAN.exists():
        pytest.skip("TrackMan export missing")
    from compare_trackman import (  # pylint: disable=import-outside-toplevel
        load_openflight,
        load_trackman,
        pair_shots,
    )

    median_club_speed = _median_club_picker()
    processor = RollingBufferProcessor()
    shots = load_openflight(SESSION)
    medians = {}
    for shot in shots:
        entry = recorded_capture(shot.shot_number)
        processed = processor.process_capture(
            IQCapture(
                sample_time=entry["sample_time"],
                trigger_time=entry["trigger_time"],
                i_samples=entry["i_samples"],
                q_samples=entry["q_samples"],
            ),
            club_type=ClubType(shot.club),
        )
        shot.ball_speed_mph = processed.ball_speed_mph
        shot.club_speed_mph = processed.club_speed_mph
        medians[shot.shot_number], _ = median_club_speed(
            processed.timeline, processed.ball_speed_mph, processed.ball_timestamp_ms
        )
    pairs = [
        pair
        for pair in pair_shots(shots, load_trackman(TRACKMAN))
        if pair.match_quality == "good" and pair.of.ball_speed_mph is not None
    ]

    def rmse(picks):
        errors = [pick - pair.tm.club_speed_mph for pair, pick in zip(pairs, picks)]
        return float(np.sqrt(np.mean(np.square(errors))))

    production = rmse([pair.of.club_speed_mph for pair in pairs])
    median = rmse([medians[pair.of.shot_number] for pair in pairs])
    assert len(pairs) == 22
    assert production == pytest.approx(5.47, abs=0.05)
    assert median == pytest.approx(5.81, abs=0.05)
    assert production < median


# -- the worker, against a fake radar ------------------------------------------


class FakeRadar:
    """What the worker uses of OPS243Radar, without a serial port."""

    DUMP_BYTES = 45000

    def __init__(self, dumps=(), *, baud=230400, connect_error=None):
        self.baud = baud
        self.bytes_per_second = baud / 10.0
        self.dumps = list(dumps)
        self.connect_error = connect_error
        self.serial = SimpleNamespace(in_waiting=0)
        self.calls = []
        self.rearm_results = []
        self.hold = None  # an Event to hold S! replies on
        self.last_software_trigger_timestamp = None
        self.last_software_trigger_first_byte_timestamp = None

    def connect(self, idle_first=False):
        self.calls.append(("connect", idle_first))
        if self.connect_error is not None:
            raise self.connect_error

    def configure_for_rolling_buffer(self, **kwargs):
        self.calls.append(("configure", kwargs))

    def trigger_capture(self, timeout=None):
        self.calls.append("S!")
        self.last_software_trigger_timestamp = time.time()
        if self.hold is not None:
            self.hold.wait(5.0)
        response = self.dumps.pop(0) if self.dumps else ""
        if isinstance(response, Exception):
            raise response
        self.last_software_trigger_first_byte_timestamp = time.time()
        return response

    def rearm_rolling_buffer(self, pre_trigger_segments=16):
        self.calls.append(("rearm", pre_trigger_segments))
        self.serial.in_waiting = 0
        return self.rearm_results.pop(0) if self.rearm_results else True

    def disconnect(self):
        self.calls.append("disconnect")


def started(radar, **kwargs):
    logs = []
    worker = OPS243ClubSpeed(
        "/dev/fake-ops", radar_factory=lambda port: radar, log=logs.append, **kwargs
    )
    worker.start()
    assert wait_until(lambda: worker.armed or not worker.enabled)
    return worker, logs


@pytest.fixture
def stop_workers():
    workers = []
    yield workers.append
    for worker in workers:
        worker.stop(timeout_s=2.0)


def test_a_camera_trigger_dumps_the_ops_and_yields_its_club_speed(stop_workers, tmp_path):
    radar = FakeRadar([synthetic_dump(85.0, 120.0)])
    worker, logs = started(radar, save_path=tmp_path / "ops243.jsonl")
    stop_workers(worker)
    swing = time.time()

    assert worker.trigger(swing) is True
    verdict = worker.verdict_for(swing, 121.0, club="7-iron")

    assert verdict.club_speed_mph == pytest.approx(85.0, abs=0.2)
    assert verdict.ops_ball_mph == pytest.approx(120.0, abs=0.3)
    capture = verdict.capture
    assert capture["status"] == "ok"
    assert capture["trigger_offset_ms"] == pytest.approx(68.0)
    assert capture["impact_before_trigger_ms"] == pytest.approx(8.3, abs=1.0)
    assert 0.0 <= capture["send_delay_ms"] < 100.0
    assert radar.calls[:3] == [
        ("connect", True),
        ("configure", {"pre_trigger_segments": 16, "sample_rate_ksps": 30, "transmit_power": 3}),
        "S!",
    ]
    assert wait_until(lambda: ("rearm", 16) in radar.calls)
    assert "keeps 68 ms before each camera trigger and 68 ms after" in logs[0]
    saved = json.loads((tmp_path / "ops243.jsonl").read_text())
    assert saved["type"] == "rolling_buffer_capture"
    assert len(saved["i_samples"]) == 4096
    assert saved["club_speed_mph"] == pytest.approx(85.0, abs=0.2)


def test_a_ball_speed_mismatch_gives_no_club_speed(stop_workers):
    worker, _logs = started(FakeRadar([synthetic_dump(85.0, 120.0)]))
    stop_workers(worker)
    swing = time.time()
    worker.trigger(swing)

    verdict = worker.verdict_for(swing, 95.0)

    assert verdict.club_speed_mph is None
    assert "ball speeds disagree" in verdict.reason


def test_a_club_pick_with_an_unphysical_smash_gives_no_club_speed(stop_workers):
    """A slow branch before a fast ball (70 against 120 mph, smash 1.7)."""
    worker, _logs = started(FakeRadar([synthetic_dump(70.0, 120.0)]))
    stop_workers(worker)
    swing = time.time()
    worker.trigger(swing)

    verdict = worker.verdict_for(swing, 120.0)

    assert verdict.club_speed_mph is None
    assert verdict.reason.startswith("smash 1.7")
    assert "outside 1.00-1.60 (club 70.0 mph)" in verdict.reason


def test_the_split_and_power_options_reach_the_radar(stop_workers):
    radar = FakeRadar()
    worker, logs = started(radar, pre_trigger_segments=20, transmit_power=1)
    stop_workers(worker)

    assert radar.calls[1] == (
        "configure",
        {"pre_trigger_segments": 20, "sample_rate_ksps": 30, "transmit_power": 1},
    )
    assert "keeps 85 ms before each camera trigger and 51 ms after" in logs[0]


def test_a_trigger_while_a_dump_is_in_flight_is_refused_without_waiting(stop_workers):
    radar = FakeRadar([synthetic_dump(85.0, 120.0)])
    radar.hold = threading.Event()
    worker, _logs = started(radar)
    stop_workers(worker)
    first = time.time()
    assert worker.trigger(first) is True
    assert wait_until(lambda: "S!" in radar.calls)

    began = time.monotonic()
    assert worker.trigger(first + 1.0) is False
    assert time.monotonic() - began < 0.05

    radar.hold.set()
    assert worker.verdict_for(first, 120.0).club_speed_mph is not None
    refused = worker.verdict_for(first + 1.0, 120.0)
    assert refused.reason == "OPS capture refused: the last dump is still in flight"
    assert worker.refused == 1


def test_a_trigger_before_the_ops_is_up_is_refused_and_said_so():
    worker = OPS243ClubSpeed("/dev/fake-ops", radar_factory=lambda port: FakeRadar())

    assert worker.trigger(50.0) is False
    assert worker.verdict_for(50.0, 120.0).reason == (
        "OPS capture refused: the OPS243 is still starting"
    )


def test_a_trigger_that_arrives_after_the_impact_left_the_buffer_sends_no_s(stop_workers):
    radar = FakeRadar([synthetic_dump(85.0, 120.0)])
    worker, _logs = started(radar)
    stop_workers(worker)
    stale = time.time() - 0.2  # the buffer holds 68 ms before S!

    assert worker.trigger(stale) is True
    verdict = worker.verdict_for(stale, 120.0)

    assert verdict.reason.startswith("OPS capture late: S! would go")
    assert "S!" not in radar.calls
    assert worker.armed


def test_three_empty_dumps_in_a_row_turn_club_speed_off_with_one_warning(stop_workers):
    worker, logs = started(FakeRadar(["", "", "", ""]))
    stop_workers(worker)
    for n in range(3):
        swing = time.time()
        assert wait_until(lambda: worker.armed or not worker.enabled)
        assert worker.trigger(swing) is True
        assert worker.verdict_for(swing, 120.0).reason.startswith("OPS capture no_dump")

    assert wait_until(lambda: not worker.is_alive())
    assert not worker.enabled
    assert worker.trigger(time.time()) is False
    assert [line for line in logs if line.startswith("WARNING")] == [
        "WARNING: OPS243 club speed off: 3 triggers in a row got no readable dump "
        "(0 bytes back from S!). Swings go on without club speed."
    ]
    later = time.time() + 10.0  # a later swing; the failed ones keep their own reasons
    assert worker.verdict_for(later, 120.0).reason.startswith("OPS243 off: 3 triggers")


def test_one_good_dump_resets_the_failure_count(stop_workers):
    good = synthetic_dump(85.0, 120.0)
    worker, _logs = started(FakeRadar(["", "", good, "", ""]))
    stop_workers(worker)
    for _ in range(5):
        assert wait_until(lambda: worker.armed)
        swing = time.time()
        worker.trigger(swing)
        worker.verdict_for(swing, 120.0)

    assert wait_until(lambda: worker.armed)
    assert worker.enabled


def test_a_serial_error_mid_session_turns_club_speed_off_once(stop_workers):
    radar = FakeRadar([serial.SerialException("device reports readiness but returned no data")])
    worker, logs = started(radar)
    stop_workers(worker)
    radar.rearm_rolling_buffer = lambda **_kw: (_ for _ in ()).throw(serial.SerialException("gone"))
    swing = time.time()
    worker.trigger(swing)

    verdict = worker.verdict_for(swing, 120.0)

    assert verdict.reason.startswith("OPS capture no_dump: SerialException")
    assert wait_until(lambda: not worker.enabled)
    assert sum(line.startswith("WARNING") for line in logs) == 1


def test_a_dump_nobody_asked_for_is_cleared_and_the_ops_rearmed(stop_workers):
    radar = FakeRadar()
    worker, logs = started(radar)
    stop_workers(worker)

    radar.serial.in_waiting = 512  # HOST_INT fired on its own

    assert wait_until(lambda: ("rearm", 16) in radar.calls)
    assert worker.unsolicited == 1
    assert worker.armed
    assert any("dump nobody asked for" in line for line in logs)


def test_a_failed_rearm_is_retried_until_the_ops_takes_it(stop_workers):
    radar = FakeRadar([synthetic_dump(85.0, 120.0)])
    radar.rearm_results = [False, False, True]
    worker, _logs = started(radar)
    stop_workers(worker)
    swing = time.time()
    worker.trigger(swing)
    worker.verdict_for(swing, 120.0)

    assert wait_until(lambda: worker.armed)
    assert radar.calls.count(("rearm", 16)) == 3
    assert worker.enabled


def test_stopping_closes_the_port(stop_workers):
    radar = FakeRadar()
    worker, _logs = started(radar)

    worker.stop(timeout_s=2.0)

    assert not worker.is_alive()
    assert radar.calls[-1] == "disconnect"


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (ConnectionError("No reply from OPS243 on /dev/ttyAMA0"), "No reply from OPS243"),
        (ConnectionError("Failed to connect to /dev/ttyAMA0"), "Failed to connect"),
    ],
)
def test_a_missing_ops_prints_one_warning_and_turns_the_feature_off(error, said):
    logs = []
    radar = FakeRadar(connect_error=error)
    worker = OPS243ClubSpeed("/dev/ttyAMA0", radar_factory=lambda port: radar, log=logs.append)

    worker.run()  # what the thread does; returns once it gives up

    assert not worker.enabled
    assert len(logs) == 1
    assert logs[0].startswith("WARNING: OPS243 club speed off: no OPS243 on /dev/ttyAMA0")
    assert said in logs[0]
    assert radar.calls[-1] == "disconnect"
    assert worker.trigger(time.time()) is False
    assert worker.verdict_for(time.time(), 120.0).reason.startswith("OPS243 off: no OPS243")


def test_an_ops_stuck_at_a_slow_baud_is_turned_off():
    logs = []
    radar = FakeRadar(baud=19200)
    worker = OPS243ClubSpeed("/dev/ttyAMA0", radar_factory=lambda port: radar, log=logs.append)

    worker.run()

    assert not worker.enabled
    assert "only reached 19200 baud, 23 s per dump" in logs[0]


# -- results by trigger time ------------------------------------------------------


def test_results_are_matched_to_the_swing_by_trigger_time(stop_workers):
    now = [100.0]
    radar = FakeRadar([synthetic_dump(85.0, 120.0), synthetic_dump(90.0, 125.0)])
    logs = []
    worker = OPS243ClubSpeed(
        "/dev/fake-ops", radar_factory=lambda port: radar, log=logs.append, clock=lambda: now[0]
    )
    worker.start()
    stop_workers(worker)
    for swing in (100.0, 112.0):
        now[0] = swing + 0.004
        assert wait_until(lambda: worker.armed)
        assert worker.trigger(swing)
        assert worker.wait_near(swing) is not None

    assert worker.near(100.2).trigger_timestamp == 100.0
    assert worker.near(111.7).trigger_timestamp == 112.0
    assert worker.near(106.0) is None
    assert worker.near(100.6) is None  # 0.5 s tolerance
    assert worker.near(100.6, tolerance_s=1.0).trigger_timestamp == 100.0
    assert worker.near(112.0).ball_speed_mph == pytest.approx(125.0, abs=0.3)


def test_a_swing_waits_while_its_own_dump_is_in_flight(stop_workers):
    radar = FakeRadar([synthetic_dump(85.0, 120.0)])
    radar.hold = threading.Event()
    worker, _logs = started(radar)
    stop_workers(worker)
    swing = time.time()
    worker.trigger(swing)
    threading.Timer(0.15, radar.hold.set).start()

    began = time.monotonic()
    found = worker.wait_near(swing, timeout_s=2.0)

    assert found is not None and found.status == "ok"
    assert 0.1 < time.monotonic() - began < 1.5


def test_a_swing_the_ops_never_took_does_not_wait(stop_workers):
    radar = FakeRadar([synthetic_dump(85.0, 120.0)])
    radar.hold = threading.Event()
    worker, _logs = started(radar)
    stop_workers(worker)
    swing = time.time()
    worker.trigger(swing)

    began = time.monotonic()
    assert worker.wait_near(swing + 30.0, timeout_s=2.0) is None
    assert time.monotonic() - began < 0.05
    radar.hold.set()


def test_a_dump_slower_than_the_wait_costs_only_the_club_speed(stop_workers):
    radar = FakeRadar([synthetic_dump(85.0, 120.0)])
    radar.hold = threading.Event()
    worker, _logs = started(radar)
    stop_workers(worker)
    swing = time.time()
    worker.trigger(swing)

    began = time.monotonic()
    verdict = worker.verdict_for(swing, 120.0, wait_s=0.1)

    assert verdict.club_speed_mph is None
    assert verdict.reason == "OPS dump still in flight (waited too long)"
    assert time.monotonic() - began < 0.5
    radar.hold.set()


# -- the real driver over a fake J3 UART ---------------------------------------------


class FakeOPSPort:
    """The OPS243-A on the Pi's J3 UART, as far as the club-speed worker uses it.

    Answers ?V only at 230,400 baud, takes mode commands silently, answers S!
    with a dump, and (``streaming``) behaves like a factory-CW board that sends
    a speed line on any motion until it is told PI.
    """

    def __init__(self, baudrate, *, dump="", answers=True, streaming=False):
        self.baudrate = baudrate
        self.dump = dump.encode()
        self.answers = answers
        self.streaming = streaming
        self.is_open = True
        self.timeout = 1.0
        self.writes = []
        self._pending = b""

    @property
    def in_waiting(self):
        return len(self._pending) + (6 if self.streaming else 0)

    def read(self, n=1):
        if not self._pending and self.streaming:
            self._pending = b"0.61\r\n"
        chunk, self._pending = self._pending[:n], self._pending[n:]
        return chunk

    def reset_input_buffer(self):
        self._pending = b""

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        if data == b"?V" and self.answers and self.baudrate == 230400:
            self._pending += b'{"Version":"1.3.3"}\r\n'
        elif data == b"PI":
            self.streaming = False
        elif data.startswith(b"S!"):
            self._pending += self.dump
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False


def fake_uart(**kwargs):
    opened = []

    def factory(*_args, **kw):
        port = FakeOPSPort(kw["baudrate"], **kwargs)
        opened.append(port)
        return port

    return opened, factory


def test_the_driver_over_the_uart_turns_a_recorded_swing_into_its_club_speed(tmp_path):
    """Driver shot 10 of the committed session: TrackMan 151.8 mph ball, 104.5
    club; the production pick reads 102.1 from this dump."""
    opened, factory = fake_uart(dump=recorded_dump(10))
    logs = []
    worker = OPS243ClubSpeed("/dev/ttyAMA0", log=logs.append, save_path=tmp_path / "ops243.jsonl")
    with patch("openflight.ops243.serial.Serial", side_effect=factory):
        worker.start()
        try:
            assert wait_until(lambda: worker.armed, timeout_s=5.0), logs
            swing = time.time()
            assert worker.trigger(swing)
            verdict = worker.verdict_for(swing, 151.8, club="driver", wait_s=3.0)
            assert wait_until(lambda: worker.armed, timeout_s=3.0)
        finally:
            worker.stop()

    assert verdict.club_speed_mph == pytest.approx(102.1, abs=0.1)
    assert verdict.smash == pytest.approx(151.8 / 102.1, abs=0.01)
    assert verdict.capture["trigger_offset_ms"] == pytest.approx(68.0)
    assert verdict.capture["ops_trigger_lag_ms"] is not None
    port = opened[-1]
    written = b"".join(port.writes)
    assert written.index(b"PI") < written.index(b"GC") < written.index(b"S#16\r")
    assert written.index(b"S#16\r") < written.index(b"S!\r")
    assert written.endswith(b"PA")  # re-armed after the dump
    analysis = str(REPO / "scripts" / "analysis")
    if analysis not in sys.path:
        sys.path.insert(0, analysis)
    from replay_captures import load_captures  # pylint: disable=import-outside-toplevel

    replayable = load_captures(str(tmp_path / "ops243.jsonl"))
    assert len(replayable) == 1
    assert replayable[0]["i_samples"] == recorded_capture(10)["i_samples"]


def test_a_streaming_factory_board_is_idled_before_the_connect_drain():
    """2026-11-19: factory CW mode streamed speed lines and connect() never quiesced."""
    opened, factory = fake_uart(streaming=True)
    radar = OPS243Radar(port="/dev/ttyAMA0")
    with patch("openflight.ops243.serial.Serial", side_effect=factory):
        began = time.monotonic()
        assert radar.connect(idle_first=True)

    assert time.monotonic() - began < 3.0
    assert b"PI" in opened[-1].writes


def test_a_port_that_cannot_open_turns_club_speed_off():
    logs = []
    worker = OPS243ClubSpeed("/dev/ttyAMA0", log=logs.append)
    with patch(
        "openflight.ops243.serial.Serial",
        side_effect=serial.SerialException("could not open port /dev/ttyAMA0"),
    ):
        worker.run()

    assert not worker.enabled
    assert len(logs) == 1 and "could not open port" in logs[0]


def test_a_radar_that_never_answers_turns_club_speed_off():
    logs = []
    _opened, factory = fake_uart(answers=False)
    worker = OPS243ClubSpeed("/dev/ttyAMA0", log=logs.append)
    with patch("openflight.ops243.serial.Serial", side_effect=factory):
        worker.run()

    assert not worker.enabled
    assert len(logs) == 1 and "No reply from OPS243" in logs[0]


def test_trigger_capture_records_when_s_went_out_and_the_dump_began():
    _opened, factory = fake_uart(dump=synthetic_dump(85.0, 120.0))
    radar = OPS243Radar(port="/dev/ttyAMA0", negotiate_baud=False)
    with patch("openflight.ops243.serial.Serial", side_effect=factory):
        radar._open_serial(230400)  # pylint: disable=protected-access
        before = time.time()
        response = radar.trigger_capture(timeout=2.0)

    assert '"Q"' in response
    assert before <= radar.last_software_trigger_timestamp
    assert radar.last_software_trigger_timestamp <= radar.last_software_trigger_first_byte_timestamp


def test_the_rolling_buffer_setup_takes_a_transmit_power():
    opened, factory = fake_uart()
    radar = OPS243Radar(port="/dev/ttyAMA0", negotiate_baud=False)
    with patch("openflight.ops243.serial.Serial", side_effect=factory):
        radar._open_serial(230400)  # pylint: disable=protected-access
        radar.configure_for_rolling_buffer(pre_trigger_segments=16, transmit_power=5)

    assert b"P5" in opened[-1].writes
    assert radar.rolling_buffer_armed
