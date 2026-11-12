#!/usr/bin/env python3
"""
Where the time goes between impact and the shot appearing, from session logs.

For every shot in the given session JSONL files it reads the timestamps the
server already records and lays them out as a timeline from the trigger:

    first byte     the OPS243 starts sending its buffer
    radar ready    the capture is parsed, clock-synced, re-armed and processed
                   (the moment the raw capture is logged)
    first numbers  the OPS numbers reach the UI (only logged when a slower
                   sensor is still working: pipeline_ms.initial_ui)
    shot           the finished shot is logged and published
    simulator      the shot is sent to each simulator

"radar ready" minus the dump's own transfer time (bytes over the link's baud
rate) is what the Pi adds after the last byte arrives: the clock sync, the
radar re-arm and the signal processing.

Usage:
    uv run python scripts/analysis/shot_latency_report.py ~/openflight_sessions/session_*.jsonl
    uv run python scripts/analysis/shot_latency_report.py session.jsonl --shots --json report.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

# Bits on the wire per byte (8N1): the dump's transfer time is bytes * 10 / baud.
BITS_PER_BYTE = 10
# Entry timestamps are local wall time without a zone; epochs are UTC. The zone
# is recovered from the gap between the two, which is a whole number of these.
ZONE_STEP_S = 900.0


def _entry_epoch_utc(entry: dict) -> Optional[float]:
    """An entry's ``ts`` read as if it were UTC (the zone is corrected later)."""
    try:
        naive = datetime.fromisoformat(entry["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    return naive.replace(tzinfo=timezone.utc).timestamp()


def read_entries(path: Path) -> list[dict]:
    """Every JSON object in a session log, skipping lines that are not JSON."""
    entries = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def shot_timelines(entries: Iterable[dict]) -> list[dict]:
    """One timeline per shot, every time in seconds after the trigger.

    A stage the log has no timestamp for is None, so logs from older builds
    (no first-byte timestamp, no simulator) still produce what they can.
    """
    baud = None
    response_bytes = None
    capture = None
    iwr_dump_s: dict = {}  # shot number -> dump seconds; logged before its shot
    shots: list[dict] = []
    current = None

    for entry in entries:
        kind = entry.get("type")
        if kind == "connection" and entry.get("device") in (None, "ops243"):
            baud = entry.get("baud") or baud
        elif kind in ("trigger_event", "trigger_diagnostic") and entry.get("accepted"):
            response_bytes = entry.get("response_bytes") or response_bytes
        elif kind == "rolling_buffer_capture":
            capture = entry
        elif kind == "iwr6843_capture":
            iwr_dump_s[entry.get("shot_number")] = entry.get("dump_duration_s")
        elif kind == "sim_send" and current is not None and current["trigger_epoch"]:
            sent = _entry_epoch_utc(entry)
            if sent is not None and current["simulator_s"] is None:
                current["simulator_s"] = sent - current["zone_s"] - current["trigger_epoch"]
        elif kind == "shot_detected":
            current = _timeline(entry, capture, baud, response_bytes)
            current["iwr_dump_s"] = iwr_dump_s.pop(entry.get("shot_number"), None)
            shots.append(current)
            capture = None
            response_bytes = None
            iwr_dump_s.clear()
    return shots


def _timeline(
    shot: dict,
    capture: Optional[dict],
    baud: Optional[int],
    response_bytes: Optional[int],
) -> dict:
    pipeline = shot.get("pipeline_ms") or {}
    timeline = {
        "shot_number": shot.get("shot_number"),
        "ball_speed_mph": shot.get("ball_speed_mph"),
        "trigger_epoch": None,
        "zone_s": 0.0,
        "first_byte_s": None,
        "dump_transfer_s": None,
        "radar_ready_s": None,
        "after_dump_s": None,
        "first_numbers_s": (
            pipeline["initial_ui"] / 1000.0 if pipeline.get("initial_ui") is not None else None
        ),
        "shot_s": None,
        "simulator_s": None,
        "iwr_ms": pipeline.get("iwr6843"),
        "iwr_dump_s": None,
    }
    if capture is None or capture.get("shot_number") != shot.get("shot_number"):
        return timeline

    first_byte = capture.get("first_byte_timestamp")
    trigger = capture.get("trigger_timestamp") or first_byte
    logged = _entry_epoch_utc(capture)
    if trigger is None or logged is None:
        return timeline

    # The log's wall-clock zone: the capture is logged seconds after the
    # trigger, so the gap between the two clocks is the zone offset.
    zone_s = round((logged - trigger) / ZONE_STEP_S) * ZONE_STEP_S
    timeline["trigger_epoch"] = trigger
    timeline["zone_s"] = zone_s
    timeline["radar_ready_s"] = logged - zone_s - trigger
    if first_byte is not None:
        timeline["first_byte_s"] = first_byte - trigger
    if baud and response_bytes:
        timeline["dump_transfer_s"] = response_bytes * BITS_PER_BYTE / baud
        if first_byte is not None:
            timeline["after_dump_s"] = (
                timeline["radar_ready_s"] - timeline["first_byte_s"] - timeline["dump_transfer_s"]
            )
    shot_logged = _entry_epoch_utc(shot)
    if shot_logged is not None:
        timeline["shot_s"] = shot_logged - zone_s - trigger
    return timeline


STAGES = (
    ("first_byte_s", "trigger -> first byte"),
    ("dump_transfer_s", "dump transfer (bytes / baud)"),
    ("after_dump_s", "after the dump: sync, re-arm, processing"),
    ("radar_ready_s", "trigger -> radar ready"),
    ("first_numbers_s", "trigger -> first numbers on the UI"),
    ("shot_s", "trigger -> finished shot"),
    ("simulator_s", "trigger -> sent to the simulator"),
    ("iwr_dump_s", "IWR6843 dump"),
)


def summarize(shots: list[dict]) -> dict:
    """Median, fastest and slowest of every stage that has data."""
    summary = {}
    for key, label in STAGES:
        values = sorted(shot[key] for shot in shots if shot.get(key) is not None)
        if values:
            summary[key] = {
                "label": label,
                "n": len(values),
                "median_s": round(statistics.median(values), 3),
                "min_s": round(values[0], 3),
                "max_s": round(values[-1], 3),
            }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    parser.add_argument("logs", nargs="+", help="session JSONL files")
    parser.add_argument("--shots", action="store_true", help="also print every shot's timeline")
    parser.add_argument("--json", metavar="FILE", help="also write the report as JSON")
    args = parser.parse_args()

    shots: list[dict] = []
    for path in args.logs:
        shots.extend(shot_timelines(read_entries(Path(path))))
    summary = summarize(shots)

    print(f"{len(shots)} shot(s) from {len(args.logs)} session log(s)\n")
    if not summary:
        print("No timing data: these logs carry no first-byte or trigger timestamps.")
    else:
        print(f"{'stage':<44}{'median':>9}{'fastest':>9}{'slowest':>9}{'shots':>7}")
        for stage in summary.values():
            print(
                f"{stage['label']:<44}{stage['median_s']:>7.2f} s{stage['min_s']:>7.2f} s"
                f"{stage['max_s']:>7.2f} s{stage['n']:>7}"
            )
    if args.shots:
        print()
        for shot in shots:
            cells = [
                f"{label.split(' -> ')[-1]} {shot[key]:.2f}s"
                for key, label in STAGES
                if shot.get(key) is not None
            ]
            print(f"#{shot['shot_number']}: " + (", ".join(cells) or "no timing data"))
    if args.json:
        Path(args.json).write_text(
            json.dumps({"summary": summary, "shots": shots}, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
