#!/usr/bin/env python3
"""
Check and time the link from this machine to a simulator, with the same
connector the server uses (OpenConnect V1: GSPro, Open-Birdie, OpenGolfSim,
PAR-TEE).

It reports how long the simulator takes to accept a connection, what it calls
itself, the club it says is selected, and how long it takes to answer a
heartbeat. With --shots it also sends test shots and times each
acknowledgement.

    --shots PLAYS REAL SHOTS IN THE SIMULATOR. Each one counts as a stroke in
    whatever round or practice session is open there. Leave it off to only
    look at the link.

Run it on the Pi against the simulator's computer to see what the network
between them costs; run it on the simulator's own computer (127.0.0.1) to see
what the simulator alone costs.

Usage:
    uv run python scripts/analysis/bench_sim_link.py --host 192.168.1.50
    uv run python scripts/analysis/bench_sim_link.py --host 192.168.1.50 --shots 5
    uv run python scripts/analysis/bench_sim_link.py --host sim.local --port 3111 --type opengolfsim
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# pylint: disable=wrong-import-position
from openflight.clubs import ClubType
from openflight.sim import build_connector
from openflight.sim.config import KNOWN_TYPES, ConnectorConfig
from openflight.sim.types import (
    ConnectionState,
    PlayerUpdate,
    ResolvedShot,
    ShotAck,
    SimError,
    initial_shot_counter,
)

# A mid-iron: obviously a test shot, and short enough to stay on most holes.
TEST_SHOT = {
    "ball_speed_mph": 110.0,
    "vla": 18.0,
    "hla": 0.0,
    "total_spin_rpm": 6500.0,
    "spin_axis_deg": 0.0,
    "carry_yards": 150.0,
    "club_path_deg": 0.0,
}


class LinkProbe:
    """Collects what the connector reports, with the time each thing happened."""

    def __init__(self) -> None:
        self.connected = threading.Event()
        self.reply = threading.Event()
        self.shot_ack = threading.Event()
        self.connected_at: float | None = None
        self.reply_at: float | None = None
        self.shot_ack_at: float | None = None
        self.peer = ""
        self.club: ClubType | None = None
        self.handed: str | None = None
        self.errors: list[str] = []
        self.rejected: list[str] = []

    def on_status(self, _target: str, event) -> None:
        if event.peer:
            self.peer = event.peer
        if event.state == ConnectionState.CONNECTED and not self.connected.is_set():
            self.connected_at = time.perf_counter()
            self.connected.set()

    def on_inbound(self, _target: str, event) -> None:
        now = time.perf_counter()
        if isinstance(event, PlayerUpdate):
            self.club = event.club if event.club is not None else self.club
            self.handed = event.handed if event.handed is not None else self.handed
        elif isinstance(event, SimError):
            self.errors.append(event.message)
        elif isinstance(event, ShotAck):
            if not event.ok:
                self.rejected.append(event.message)
            if "shot" in (event.message or "").lower():
                self.shot_ack_at = now
                self.shot_ack.set()
                return
        self.reply_at = now
        self.reply.set()


def _summary(values_ms: list[float]) -> dict:
    ordered = sorted(values_ms)
    return {
        "n": len(ordered),
        "median_ms": round(statistics.median(ordered), 2),
        "min_ms": round(ordered[0], 2),
        "max_ms": round(ordered[-1], 2),
    }


def run(args: argparse.Namespace) -> dict:
    """Probe the link and return the report."""
    probe = LinkProbe()
    cfg = ConnectorConfig(
        type=args.type,
        enabled=True,
        host=args.host,
        port=args.port,
        # The probe sends its own heartbeats, so the timings are not mixed
        # with the connector's background ones.
        heartbeat_interval_s=3600.0,
    )
    connector = build_connector(cfg, on_status=probe.on_status, on_inbound=probe.on_inbound)
    report: dict = {"host": args.host, "port": args.port, "type": args.type}

    started = time.perf_counter()
    connector.start()
    try:
        if not probe.connected.wait(timeout=args.timeout):
            report["error"] = f"no connection within {args.timeout:g} s"
            return report
        report["connect_ms"] = round((probe.connected_at - started) * 1000.0, 2)

        # A sim that greets (GSPro and Open-Birdie send the player on connect)
        # answers within a moment; one that doesn't is not an error.
        probe.reply.wait(timeout=1.0)
        report["greeting_ms"] = (
            round((probe.reply_at - probe.connected_at) * 1000.0, 2) if probe.reply_at else None
        )

        beat = connector.codec.heartbeat_bytes()
        round_trips: list[float] = []
        if beat is not None:
            for _ in range(args.heartbeats):
                probe.reply.clear()
                sent = time.perf_counter()
                connector._client.send_raw(beat)  # pylint: disable=protected-access
                if probe.reply.wait(timeout=2.0):
                    round_trips.append((probe.reply_at - sent) * 1000.0)
                time.sleep(0.05)
        report["heartbeat_round_trip"] = _summary(round_trips) if round_trips else None

        if args.shots > 0:
            acks: list[float] = []
            unacked = 0
            shot_number = initial_shot_counter()
            for index in range(args.shots):
                shot_number += 1
                resolved = ResolvedShot(
                    shot_number=shot_number,
                    back_spin_rpm=TEST_SHOT["total_spin_rpm"],
                    side_spin_rpm=0.0,
                    club=probe.club or ClubType.IRON_7,
                    club_speed_mph=None,
                    **{**TEST_SHOT, "hla": round(-1.0 + index * 0.5, 1)},
                )
                probe.shot_ack.clear()
                sent = time.perf_counter()
                if not connector.deliver_shot(resolved):
                    unacked += 1
                    continue
                if probe.shot_ack.wait(timeout=2.0):
                    acks.append((probe.shot_ack_at - sent) * 1000.0)
                else:
                    unacked += 1
                time.sleep(args.shot_gap)
            report["shot_ack"] = _summary(acks) if acks else None
            report["shots_sent"] = args.shots
            report["shots_without_ack"] = unacked

        report["simulator"] = probe.peer or connector.peer or None
        report["club"] = probe.club.value if probe.club else None
        report["handed"] = probe.handed
        report["errors"] = probe.errors
        report["rejected"] = probe.rejected
        return report
    finally:
        connector.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    parser.add_argument("--host", required=True, help="the simulator's address")
    parser.add_argument("--port", type=int, default=921, help="its OpenConnect port (default 921)")
    parser.add_argument("--type", choices=KNOWN_TYPES, default="gspro", help="connector type")
    parser.add_argument(
        "--heartbeats", type=int, default=20, help="heartbeats to time (default 20)"
    )
    parser.add_argument(
        "--shots",
        type=int,
        default=0,
        help="test shots to send; each one IS PLAYED in the simulator (default 0)",
    )
    parser.add_argument("--shot-gap", type=float, default=1.0, help="seconds between test shots")
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="seconds to wait for a connection"
    )
    parser.add_argument("--json", metavar="FILE", help="also write the report as JSON")
    args = parser.parse_args()

    report = run(args)
    if "error" in report:
        print(f"{args.host}:{args.port}: {report['error']}")
        return 1

    print(f"simulator at {args.host}:{args.port}: {report['simulator'] or args.type}")
    print(f"  connected in            {report['connect_ms']:.1f} ms")
    if report["greeting_ms"] is not None:
        print(
            f"  greeted after           {report['greeting_ms']:.1f} ms "
            f"(club {report['club'] or '-'}, {report['handed'] or '-'})"
        )
    else:
        print("  no greeting (this simulator does not send one)")
    beat = report["heartbeat_round_trip"]
    if beat:
        print(
            f"  heartbeat round trip    median {beat['median_ms']:.2f} ms, "
            f"{beat['min_ms']:.2f}-{beat['max_ms']:.2f} ms over {beat['n']}"
        )
    else:
        print("  heartbeats were not answered")
    if args.shots > 0:
        ack = report["shot_ack"]
        if ack:
            print(
                f"  shot acknowledged in    median {ack['median_ms']:.2f} ms, "
                f"{ack['min_ms']:.2f}-{ack['max_ms']:.2f} ms over {ack['n']}"
            )
        if report["shots_without_ack"]:
            print(f"  {report['shots_without_ack']} of {args.shots} shots were not acknowledged")
    for message in report["errors"]:
        print(f"  simulator error: {message}")
    for message in report["rejected"]:
        print(f"  simulator rejected: {message}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
