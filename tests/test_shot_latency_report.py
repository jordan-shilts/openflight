"""The shot latency report: session-log timestamps laid out from the trigger."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "analysis"))

import shot_latency_report as report  # noqa: E402

TRIGGER_EPOCH = 1_790_000_000.0
BAUD = 230_400
DUMP_BYTES = 40_556


def _approx(seconds: float):
    """Epochs near 1.8e9 hold about 0.2 us; entry timestamps hold 1 us."""
    return pytest.approx(seconds, abs=1e-4)


def _local_ts(epoch: float, zone_hours: float) -> str:
    """An entry timestamp as the logger writes it: local wall time, no zone."""
    local = datetime.fromtimestamp(epoch, tz=timezone.utc) + timedelta(hours=zone_hours)
    return local.replace(tzinfo=None).isoformat()


def _shot_entries(
    number: int,
    trigger: float,
    *,
    zone_hours: float = 0.0,
    radar_ready_s: float = 2.9,
    shot_s: float = 9.4,
    simulator_s: float | None = 9.41,
    pipeline_ms: dict | None = None,
    first_byte: bool = True,
) -> list[dict]:
    capture = {
        "ts": _local_ts(trigger + radar_ready_s, zone_hours),
        "type": "rolling_buffer_capture",
        "shot_number": number,
        "trigger_timestamp": trigger,
    }
    if first_byte:
        capture["first_byte_timestamp"] = trigger + 0.068
    entries = [
        {"type": "trigger_event", "accepted": True, "response_bytes": DUMP_BYTES},
        capture,
        {
            "ts": _local_ts(trigger + 9.0, zone_hours),
            "type": "iwr6843_capture",
            "shot_number": number,
            "dump_duration_s": 7.6,
        },
        {
            "ts": _local_ts(trigger + shot_s, zone_hours),
            "type": "shot_detected",
            "shot_number": number,
            "ball_speed_mph": 120.0,
            "pipeline_ms": pipeline_ms if pipeline_ms is not None else {"initial_ui": 2950.0},
        },
    ]
    if simulator_s is not None:
        entries.append(
            {
                "ts": _local_ts(trigger + simulator_s, zone_hours),
                "type": "sim_send",
                "target": "gspro",
                # the simulator's own counter, not the shot number
                "shot_number": 1_790_000_123,
            }
        )
    return entries


def _connection(baud: int = BAUD) -> dict:
    return {"type": "connection", "device": "ops243", "baud": baud}


def test_timeline_is_measured_from_the_trigger():
    shots = report.shot_timelines([_connection(), *_shot_entries(1, TRIGGER_EPOCH)])

    assert len(shots) == 1
    shot = shots[0]
    assert shot["first_byte_s"] == _approx(0.068)
    assert shot["radar_ready_s"] == _approx(2.9)
    assert shot["first_numbers_s"] == _approx(2.95)
    assert shot["shot_s"] == _approx(9.4)
    assert shot["simulator_s"] == _approx(9.41)
    assert shot["iwr_dump_s"] == _approx(7.6)


def test_time_after_the_dump_excludes_the_transfer():
    shot = report.shot_timelines([_connection(), *_shot_entries(1, TRIGGER_EPOCH)])[0]

    transfer = DUMP_BYTES * 10 / BAUD
    assert shot["dump_transfer_s"] == _approx(transfer)
    # radar ready at 2.9 s; the dump ran from 0.068 s for ~1.76 s
    assert shot["after_dump_s"] == _approx(2.9 - 0.068 - transfer)


@pytest.mark.parametrize("zone_hours", [-6.0, 0.0, 5.5, 13.0])
def test_the_logs_wall_clock_zone_does_not_shift_the_timeline(zone_hours):
    """Entries carry local time, epochs are UTC: the report runs on any machine."""
    entries = [_connection(), *_shot_entries(1, TRIGGER_EPOCH, zone_hours=zone_hours)]
    shot = report.shot_timelines(entries)[0]

    assert shot["radar_ready_s"] == _approx(2.9)
    assert shot["shot_s"] == _approx(9.4)
    assert shot["simulator_s"] == _approx(9.41)


def test_each_shot_keeps_its_own_simulator_send():
    entries = [
        _connection(),
        *_shot_entries(1, TRIGGER_EPOCH, shot_s=2.0, simulator_s=2.01),
        *_shot_entries(2, TRIGGER_EPOCH + 60.0, shot_s=3.0, simulator_s=None),
        *_shot_entries(3, TRIGGER_EPOCH + 120.0, shot_s=4.0, simulator_s=4.5),
    ]
    shots = report.shot_timelines(entries)

    assert [shot["simulator_s"] for shot in shots] == [
        _approx(2.01),
        None,
        _approx(4.5),
    ]


def test_only_the_first_simulator_send_of_a_shot_counts():
    entries = [_connection(), *_shot_entries(1, TRIGGER_EPOCH)]
    entries.append({"ts": _local_ts(TRIGGER_EPOCH + 30.0, 0.0), "type": "sim_send"})

    assert report.shot_timelines(entries)[0]["simulator_s"] == _approx(9.41)


def test_a_log_without_first_byte_timing_still_reports_what_it_can():
    entries = [_connection(), *_shot_entries(1, TRIGGER_EPOCH, first_byte=False)]
    shot = report.shot_timelines(entries)[0]

    assert shot["first_byte_s"] is None
    assert shot["after_dump_s"] is None
    assert shot["radar_ready_s"] == _approx(2.9)


def test_an_old_log_with_no_timestamps_yields_no_timing():
    """Logs from before first-byte timestamps: shots are counted, nothing is invented."""
    entries = [
        {"ts": "2026-05-06T15:25:05.878558", "type": "rolling_buffer_capture", "shot_number": 1},
        {
            "ts": "2026-05-06T15:25:06.145967",
            "type": "shot_detected",
            "shot_number": 1,
            "pipeline_ms": {"kld7": 265.0},
        },
    ]
    shots = report.shot_timelines(entries)

    assert len(shots) == 1
    assert report.summarize(shots) == {}


def test_a_capture_from_another_shot_is_not_used():
    """A rejected capture's leftovers must not date the next shot."""
    entries = [_connection(), *_shot_entries(1, TRIGGER_EPOCH)]
    entries[2]["shot_number"] = 7  # the capture entry
    shot = report.shot_timelines(entries)[0]

    assert shot["radar_ready_s"] is None
    assert shot["shot_s"] is None


def test_summary_takes_the_median_of_each_stage():
    entries = [_connection()]
    for index, ready in enumerate((2.8, 3.0, 3.6), start=1):
        entries += _shot_entries(index, TRIGGER_EPOCH + 60.0 * index, radar_ready_s=ready)
    summary = report.summarize(report.shot_timelines(entries))

    assert summary["radar_ready_s"]["median_s"] == _approx(3.0)
    assert summary["radar_ready_s"]["min_s"] == _approx(2.8)
    assert summary["radar_ready_s"]["max_s"] == _approx(3.6)
    assert summary["radar_ready_s"]["n"] == 3


def test_read_entries_skips_lines_that_are_not_json(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text('{"type": "session_start"}\nnot json\n[1, 2]\n{"type": "session_end"}\n')

    assert [entry["type"] for entry in report.read_entries(path)] == [
        "session_start",
        "session_end",
    ]


def test_command_line_prints_a_summary_and_writes_json(tmp_path, capsys, monkeypatch):
    path = tmp_path / "session.jsonl"
    entries = [_connection(), *_shot_entries(1, TRIGGER_EPOCH)]
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    out = tmp_path / "report.json"
    monkeypatch.setattr(
        sys, "argv", ["shot_latency_report.py", str(path), "--shots", "--json", str(out)]
    )

    assert report.main() == 0

    printed = capsys.readouterr().out
    assert "1 shot(s) from 1 session log(s)" in printed
    assert "after the dump: sync, re-arm, processing" in printed
    assert "#1:" in printed
    written = json.loads(out.read_text())
    assert written["summary"]["shot_s"]["median_s"] == _approx(9.4)
    assert len(written["shots"]) == 1
