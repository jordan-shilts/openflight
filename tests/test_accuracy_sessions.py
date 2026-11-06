"""Tests for reading session logs in the accuracy report."""

import json

import pytest
from accuracy_logs import lcmf, make_shot, write_session

from openflight.accuracy.sessions import find_session_files, load_session, load_sessions


def test_load_session_keeps_the_entry_types_the_report_reads(tmp_path):
    path = write_session(
        tmp_path,
        "range",
        triggers=[(True, ""), (True, ""), (False, "no_ball")],
        shots=[make_shot(), make_shot()],
        captures=[(100.0, lcmf(), None)],
        error_components=["iwr6843"],
        sim_sends=[("gspro", 7)],
    )

    session = load_session(path)

    assert session.session_id == session.start["session_id"]
    assert session.mode == "rolling-buffer"
    assert not session.is_mock
    assert len(session.shots) == 2
    assert len(session.triggers) == 3
    assert len(session.iwr6843_captures) == 1
    assert len(session.errors) == 1
    assert len(session.sim_sends) == 1
    assert session.malformed_lines == 0


def test_load_session_counts_and_skips_unreadable_lines(tmp_path):
    path = write_session(tmp_path, "range", shots=[make_shot()])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write("not json\n")
        handle.write("[1, 2]\n")
        handle.write('{"ts": "2026-11-06T10:00:00", "type": "shot_detected", "ball_sp')

    session = load_session(path)

    assert session.malformed_lines == 3
    assert len(session.shots) == 1


def test_session_without_a_start_entry_falls_back_to_the_file_name(tmp_path):
    path = tmp_path / "session_20261106_100000_range.jsonl"
    path.write_text(json.dumps({"type": "shot_detected", "ball_speed_mph": 90.0}) + "\n")

    session = load_session(path)

    assert session.session_id == "session_20261106_100000_range"
    assert session.start_time == ""
    assert session.mode == "unknown"
    assert not session.is_mock
    assert len(session.shots) == 1


def test_mock_sessions_are_flagged(tmp_path):
    assert load_session(write_session(tmp_path, "mock", mode="mock")).is_mock


def test_find_session_files_reads_directories_once_and_ignores_other_files(tmp_path):
    first = write_session(tmp_path, "a")
    second = write_session(tmp_path, "b")
    (tmp_path / "notes.txt").write_text("not a session")
    (tmp_path / "calibration").mkdir()
    (tmp_path / "calibration" / "session_nested.jsonl").write_text("{}\n")

    files = find_session_files([tmp_path, first])

    assert sorted(files) == sorted([first.resolve(), second.resolve()])
    assert not any(path.name.startswith("radar_raw_") for path in files)


def test_find_session_files_rejects_a_missing_path(tmp_path):
    with pytest.raises(FileNotFoundError, match="missing"):
        find_session_files([tmp_path / "missing"])


def test_load_sessions_orders_by_start_time_not_file_name(tmp_path):
    for name, session_id, start_time in (
        ("session_a.jsonl", "late", "2026-11-06T11:00:00"),
        ("session_b.jsonl", "early", "2026-11-06T09:00:00"),
    ):
        entry = {"type": "session_start", "session_id": session_id, "start_time": start_time}
        (tmp_path / name).write_text(json.dumps(entry) + "\n")

    sessions = load_sessions([tmp_path])

    assert [session.session_id for session in sessions] == ["early", "late"]
