"""Tests for assembling and rendering the accuracy and reliability report."""

import json
from pathlib import Path

import pytest
from accuracy_logs import inclinometer_reading, lcmf, make_shot, write_session

from openflight.accuracy.report import build_report, render_markdown, summarize_session
from openflight.accuracy.sessions import load_session, load_sessions

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_SESSION = REPO_ROOT / "session_logs" / "session_20260506_152406_range.jsonl"


def _mixed_session(log_dir: Path) -> Path:
    corrected = {"raw_ball_speed": 98.0, "spin_source": "calculated"}
    return write_session(
        log_dir,
        "range",
        triggers=[(True, "")] * 5 + [(False, "no_ball")],
        shots=[
            make_shot(inclinometer=inclinometer_reading(0.40), **corrected),
            make_shot(inclinometer=inclinometer_reading(0.45), **corrected),
            make_shot(
                horizontal_source="estimated",
                inclinometer=inclinometer_reading(0.55, applied=False),
                **corrected,
            ),
            make_shot(
                vertical_source="estimated",
                horizontal_source="estimated",
                inclinometer={"applied": False, "status": "stale", "age_s": None},
                **corrected,
            ),
            make_shot(vertical_source="camera", horizontal_source="estimated", club_speed=60.0),
        ],
        captures=[
            (100.0, lcmf(track_speed=100.0, agreement_deg=0.5), None),
            (100.0, lcmf(track_speed=102.0, single_channel=True), None),
            (100.0, lcmf("accepted_ops_guided", track_speed=120.0, agreement_deg=1.5), None),
            (100.0, None, "no capture matched the OPS impact timestamp"),
        ],
        error_components=["iwr6843", "iwr6843", None],
        sim_sends=[(target, number) for number in (1, 2, 3) for target in ("gspro", "ogs")],
    )


def _speed_shift_sessions(log_dir: Path) -> tuple[Path, Path]:
    def captures(low: float, high: float, count: int):
        return [
            (100.0, lcmf(track_speed=low if index % 2 == 0 else high), None)
            for index in range(count)
        ]

    before = write_session(log_dir, "before", captures=captures(99.0, 101.0, 40))
    after = write_session(log_dir, "after", captures=captures(89.0, 91.0, 20))
    return before, after


def _counts(rate: dict) -> tuple[int, int]:
    return rate["count"], rate["total"]


def test_session_summary_counts_reliability(tmp_path):
    summary = summarize_session(load_session(_mixed_session(tmp_path)))

    assert summary["shots"] == 5
    assert _counts(summary["triggers"]["accepted"]) == (5, 6)
    assert summary["triggers"]["rejected_reasons"] == {"no_ball": 1}
    vertical = summary["launch_angle"]["vertical"]
    assert _counts(vertical["measured"]) == (4, 5)
    assert vertical["sources"] == {"radar": 3, "estimated": 1, "camera": 1}
    assert _counts(summary["launch_angle"]["horizontal"]["measured"]) == (2, 5)
    assert summary["errors"] == {"iwr6843": 2, "unspecified": 1}
    assert _counts(summary["sim_forwarded"]) == (3, 5)
    assert summary["pipeline_ms"]["iwr6843"]["median"] == 900.0
    assert "kld7" not in summary["pipeline_ms"]


def test_session_summary_checks_sensor_consistency(tmp_path):
    summary = summarize_session(load_session(_mixed_session(tmp_path)))

    iwr = summary["iwr6843"]
    assert iwr["statuses"] == {"accepted": 2, "accepted_ops_guided": 1, "capture_error": 1}
    assert _counts(iwr["accepted"]) == (3, 4)
    assert _counts(iwr["single_channel"]) == (1, 3)
    assert iwr["ops_track_speed_ratio"]["n"] == 3
    assert iwr["ops_track_speed_ratio"]["median"] == pytest.approx(1.02)
    assert _counts(iwr["speed_mismatch"]) == (1, 3)
    assert iwr["agreement_deg"]["median"] == pytest.approx(1.0)
    assert _counts(summary["club_speed"]["measured"]) == (5, 5)
    assert _counts(summary["club_speed"]["over_smash_limit"]) == (1, 5)


def test_session_summary_reports_compensation_in_effect(tmp_path):
    summary = summarize_session(load_session(_mixed_session(tmp_path)))

    assert _counts(summary["compensation"]["ball_speed_corrected"]) == (4, 5)
    assert summary["compensation"]["spin_sources"] == {"calculated": 4, "none": 1}
    inclinometer = summary["inclinometer"]
    assert _counts(inclinometer["applied"]) == (2, 4)
    assert inclinometer["not_applied"] == {"ok": 1, "stale": 1}
    assert inclinometer["pitch_deg"] == {"n": 3, "min": 0.40, "max": 0.55}


def test_rates_carry_a_wilson_interval_and_empty_rates_have_none(tmp_path):
    summary = summarize_session(load_session(write_session(tmp_path, "empty")))

    assert summary["triggers"]["accepted"] == {"count": 0, "total": 0, "rate": None, "ci95": None}
    assert summary["sim_forwarded"] is None
    accepted = summarize_session(load_session(_mixed_session(tmp_path)))["iwr6843"]["accepted"]
    low, high = accepted["ci95"]
    assert 0.0 < low < 0.75 < high <= 1.0


def test_mock_sessions_are_skipped_unless_asked_for(tmp_path):
    write_session(tmp_path, "mock", mode="mock", shots=[make_shot()])
    write_session(tmp_path, "range", shots=[make_shot()])
    sessions = load_sessions([tmp_path])

    default = build_report(sessions)
    with_mock = build_report(sessions, include_mock=True)

    assert default["sessions_found"] == 2
    assert default["mock_sessions_skipped"] == 1
    assert [s["mode"] for s in default["sessions"]] == ["rolling-buffer"]
    assert with_mock["mock_sessions_skipped"] == 0
    assert len(with_mock["sessions"]) == 2


def test_drift_names_the_session_that_shifted(tmp_path):
    _before, after = _speed_shift_sessions(tmp_path)

    report = build_report(load_sessions([tmp_path]))

    (shift,) = report["drift"]
    assert shift["series"] == "ops_track_speed_ratio"
    assert shift["path"] == str(after.resolve())
    assert shift["direction"] == "down"
    assert shift["reference"] == pytest.approx(1.0)
    assert shift["level"] == pytest.approx(0.90)
    assert (shift["samples"], shift["reference_samples"]) == (20, 40)
    assert shift["p_value"] < 0.001


def test_drift_reports_why_a_series_was_not_compared(tmp_path):
    _speed_shift_sessions(tmp_path)

    report = build_report(load_sessions([tmp_path]))

    reasons = {entry["series"]: entry["reason"] for entry in report["drift_skipped"]}
    assert reasons["mount_pitch_deg"] == "needs two sessions with at least 10 samples"
    assert reasons["trigger_accepted"] == "needs two sessions with at least 10 samples"
    assert "ops_track_speed_ratio" not in reasons
    assert "lcmf_agreement_deg" not in reasons


def test_report_is_strict_json(tmp_path):
    _mixed_session(tmp_path)

    report = build_report(load_sessions([tmp_path]))

    assert json.loads(json.dumps(report, allow_nan=False))["sessions"][0]["shots"] == 5


def test_markdown_shows_every_section_and_the_drift(tmp_path):
    _speed_shift_sessions(tmp_path)
    write_session(tmp_path, "mock", mode="mock")
    sessions = load_sessions([tmp_path])

    markdown = render_markdown(build_report(sessions))

    for heading in (
        "# OpenFlight accuracy and reliability report",
        "## Reliability",
        "## Consistency",
        "## Compensation in effect",
        "## Drift across sessions",
        "## Details",
    ):
        assert heading in markdown
    assert "Skipped 1 mock session;" in markdown
    after = next(session for session in sessions if session.path.stem.endswith("_after"))
    drift_line = (
        "- **IWR6843 track speed / OPS243 ball speed** dropped from 1.000 to 0.900 "
        f"in {after.session_id} (20 samples against 40, p < 0.0001)."
    )
    skipped_line = (
        "- Not checked: Enclosure pitch (deg). Needs two sessions with at least 10 samples."
    )
    assert drift_line in markdown
    assert skipped_line in markdown


def test_markdown_says_when_there_is_nothing_to_report(tmp_path):
    write_session(tmp_path, "mock", mode="mock", shots=[make_shot()])

    markdown = render_markdown(build_report(load_sessions([tmp_path])))

    assert "No sessions to report on." in markdown
    assert "## Reliability" not in markdown


def test_real_kld7_session_from_the_repo():
    summary = summarize_session(load_session(REAL_SESSION))

    assert summary["shots"] == 24
    assert _counts(summary["triggers"]["accepted"]) == (24, 24)
    assert _counts(summary["launch_angle"]["vertical"]["measured"]) == (23, 24)
    assert _counts(summary["launch_angle"]["horizontal"]["measured"]) == (13, 24)
    assert _counts(summary["club_speed"]["over_smash_limit"]) == (0, 24)
    assert summary["iwr6843"]["captures"] == 0
    assert _counts(summary["compensation"]["ball_speed_corrected"]) == (0, 24)
    assert summary["pipeline_ms"]["kld7"]["median"] == pytest.approx(142.25)
