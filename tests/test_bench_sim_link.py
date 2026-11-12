"""The simulator link check, run against an Open Connect sim on a real socket."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "analysis"))

import bench_sim_link as link  # noqa: E402

from tests.test_sim_delivery import ScriptedSim, _free_port  # noqa: E402


def _args(port: int, **overrides) -> argparse.Namespace:
    values = dict(
        host="127.0.0.1",
        port=port,
        type="gspro",
        heartbeats=3,
        shots=0,
        shot_gap=0.0,
        timeout=3.0,
        json=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_reports_the_simulator_and_times_its_heartbeats():
    sim = ScriptedSim(name="Open-Birdie")
    try:
        report = link.run(_args(sim.port))
    finally:
        sim.stop()

    assert report["simulator"] == "Open-Birdie"
    assert report["handed"] == "RH"
    assert report["connect_ms"] >= 0
    assert report["greeting_ms"] is not None
    assert report["heartbeat_round_trip"]["n"] == 3
    assert report["errors"] == [] and report["rejected"] == []
    # looking at the link plays nothing
    assert sim.shots == []


def test_no_shot_is_sent_unless_asked_for():
    sim = ScriptedSim()
    try:
        link.run(_args(sim.port, heartbeats=1))
    finally:
        sim.stop()

    assert sim.shots == []


def test_test_shots_are_sent_once_each_and_their_acks_timed():
    sim = ScriptedSim()
    try:
        report = link.run(_args(sim.port, heartbeats=1, shots=3))
    finally:
        sim.stop()

    assert report["shots_sent"] == 3
    assert report["shots_without_ack"] == 0
    assert report["shot_ack"]["n"] == 3
    numbers = [number for _connection, number in sim.shots]
    assert len(numbers) == 3
    assert numbers == sorted(set(numbers)), "every test shot has its own, rising number"


def test_a_sim_that_never_acks_shots_is_reported_not_retried_forever():
    sim = ScriptedSim(name="", ack_on=lambda _index: False)
    try:
        report = link.run(_args(sim.port, heartbeats=1, shots=1))
    finally:
        sim.stop()

    assert report["shot_ack"] is None
    assert report["shots_without_ack"] == 1
    assert report["heartbeat_round_trip"] is None
    assert report["simulator"] is None


def test_nothing_listening_is_an_error_not_a_hang():
    report = link.run(_args(_free_port(), timeout=0.6))

    assert "no connection within" in report["error"]


def test_command_line_exits_nonzero_when_the_simulator_is_unreachable(monkeypatch, capsys):
    port = _free_port()
    monkeypatch.setattr(
        sys,
        "argv",
        ["bench_sim_link.py", "--host", "127.0.0.1", "--port", str(port), "--timeout", "0.6"],
    )

    assert link.main() == 1
    assert "no connection within" in capsys.readouterr().out
