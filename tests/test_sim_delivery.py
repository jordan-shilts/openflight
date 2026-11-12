"""Shot delivery across dropped links and network switches.

When the Pi moves between home Wi-Fi and its own hotspot, or the sim's machine
does, the TCP link to the sim dies, sometimes without a FIN. These tests run a
small Open Connect sim on a real socket and check that a shot taken in that
window still arrives once, and that a sim which never acks is left alone.
"""

import json
import socket
import threading
import time

import pytest

from openflight.clubs import ClubType
from openflight.gspro.codec import GSProCodec
from openflight.sim import transport as transport_mod
from openflight.sim.transport import TcpSimClient
from openflight.sim.types import ConnectionState, ResolvedShot


def _resolved(n=7) -> ResolvedShot:
    return ResolvedShot(
        shot_number=n,
        ball_speed_mph=140.0,
        vla=12.0,
        hla=0.0,
        total_spin_rpm=2500.0,
        spin_axis_deg=0.0,
        back_spin_rpm=2500.0,
        side_spin_rpm=0.0,
        carry_yards=255.0,
        club_path_deg=0.0,
        club=ClubType.DRIVER,
        club_speed_mph=None,
        provenance={},
    )


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ScriptedSim:
    """An Open Connect sim on a real socket.

    ``name``: sent in every reply's Simulator field (Open-Birdie does this).
    ``ack_on``: which connections (0-based) ack shots; a connection that
    doesn't ack stays silent but open, like a peer that left the network.
    """

    def __init__(
        self,
        port=0,
        name="Open-Birdie",
        ack_on=lambda conn_index: True,
        greet=True,
        ack_heartbeats=True,
    ):
        self.name = name
        self.ack_on = ack_on
        self.greet = greet
        self.ack_heartbeats = ack_heartbeats
        self.shots = []  # (connection index, ShotNumber)
        self.shot_messages = []  # every shot as it arrived, whole
        self.connections = 0
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", port))
        self._listener.listen(4)
        self._listener.settimeout(0.1)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._socks = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _reply(self, sock, obj):
        if self.name:
            obj = {**obj, "Simulator": self.name}
        try:
            sock.sendall(json.dumps(obj).encode())
        except OSError:
            pass

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                sock, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            index = self.connections
            self.connections += 1
            self._socks.append(sock)
            threading.Thread(target=self._serve, args=(sock, index), daemon=True).start()

    def _serve(self, sock, index):
        sock.settimeout(0.1)
        if self.greet:
            self._reply(
                sock,
                {"Code": 201, "Message": "GSPro Player Information", "Player": {"Handed": "RH"}},
            )
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while True:
                end = transport_mod.find_json_end(buf)
                if end is None:
                    break
                msg = json.loads(buf[:end])
                buf = buf[end:]
                opts = msg.get("ShotDataOptions", {})
                if opts.get("IsHeartBeat"):
                    if self.ack_heartbeats and self.ack_on(index):
                        self._reply(sock, {"Code": 200, "Message": "Heartbeat OK"})
                    continue
                self.shots.append((index, msg["ShotNumber"]))
                self.shot_messages.append(msg)
                if self.ack_on(index):
                    self._reply(sock, {"Code": 200, "Message": "Shot received successfully"})

    def stop(self):
        self._stop.set()
        for s in self._socks:
            try:
                s.close()
            except OSError:
                pass
        self._listener.close()
        self._thread.join(timeout=2)


@pytest.fixture
def fast_timeouts(monkeypatch):
    monkeypatch.setattr(transport_mod, "SHOT_REPLY_TIMEOUT_S", 0.4)
    monkeypatch.setattr(transport_mod, "PENDING_SHOT_TTL_S", 5.0)


def _client(port, **kw):
    kw.setdefault("backoff_seconds", (0.05, 0.1, 0.2))
    return TcpSimClient("127.0.0.1", port, GSProCodec(), heartbeat_interval_s=60, **kw)


def _until(predicate, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _shot_bytes(client, n=7):
    return client._codec.build_shot(_resolved(n))  # pylint: disable=protected-access


def test_connected_shot_goes_out_now_and_reports_once(fast_timeouts):
    sim = ScriptedSim()
    client = _client(sim.port)
    delivered = []
    client.start()
    try:
        assert _until(lambda: client.peer == "Open-Birdie")
        assert client.deliver(_shot_bytes(client), on_delivered=lambda: delivered.append(1))
        assert _until(lambda: sim.shots == [(0, 7)])
        assert _until(lambda: not client.has_pending_shot)
        time.sleep(0.6)  # past the ack timeout: nothing more happens
        assert sim.shots == [(0, 7)] and sim.connections == 1 and delivered == [1]
    finally:
        client.stop()
        sim.stop()


def test_shot_taken_while_sim_unreachable_arrives_when_it_comes_up(fast_timeouts):
    port = _free_port()
    client = _client(port, backoff_seconds=(0.05, 1.0, 3.0))
    delivered = []
    client.start()
    sim = None
    try:
        time.sleep(0.3)  # a few failed attempts; backoff now long
        assert client.state != ConnectionState.CONNECTED
        assert not client.deliver(_shot_bytes(client), on_delivered=lambda: delivered.append(1))
        assert delivered == []
        sim = ScriptedSim(port=port)  # the network came back
        # A waiting shot keeps retries at the shortest interval, not 3 s.
        assert _until(lambda: sim.shots == [(0, 7)], timeout=1.5)
        assert delivered == [1]
        assert _until(lambda: not client.has_pending_shot)
    finally:
        client.stop()
        if sim:
            sim.stop()


def test_silent_dead_link_is_dropped_and_the_shot_resent(fast_timeouts):
    # Connection 0 goes silent (peer left the network without closing);
    # connection 1 is the sim seen again after the switch.
    sim = ScriptedSim(ack_on=lambda i: i >= 1)
    client = _client(sim.port)
    delivered = []
    statuses = []
    client.on_status = statuses.append
    client.start()
    try:
        assert _until(lambda: client.peer == "Open-Birdie")
        assert client.deliver(_shot_bytes(client), on_delivered=lambda: delivered.append(1))
        assert _until(lambda: (1, 7) in sim.shots)
        assert sim.shots == [(0, 7), (1, 7)]
        assert delivered == [1]  # the UI hears about it once
        assert any("no ack" in (s.message or "") for s in statuses)
        assert _until(lambda: not client.has_pending_shot)
    finally:
        client.stop()
        sim.stop()


def test_sim_that_never_acks_is_not_resent_or_dropped(fast_timeouts):
    sim = ScriptedSim(name="", greet=False, ack_on=lambda i: False)
    client = _client(sim.port)
    client.start()
    try:
        assert _until(lambda: client.is_connected())
        time.sleep(0.2)
        assert client.deliver(_shot_bytes(client))
        time.sleep(1.0)
        assert sim.shots == [(0, 7)]
        assert sim.connections == 1
        assert client.is_connected()
    finally:
        client.stop()
        sim.stop()


def test_ack_learned_from_an_unnamed_sim_protects_later_shots(fast_timeouts):
    # A GSPro-style sim (no Simulator field) that acks its first shot, then goes silent.
    sim = ScriptedSim(name="", greet=False, ack_on=lambda i: i != 0)
    state = {"silent": False}
    sim.ack_on = lambda i: not (i == 0 and state["silent"])
    client = _client(sim.port)
    client.start()
    try:
        assert _until(lambda: client.is_connected())
        time.sleep(0.2)
        assert client.deliver(_shot_bytes(client, 1))
        assert _until(lambda: not client.has_pending_shot)
        state["silent"] = True
        assert client.deliver(_shot_bytes(client, 2))
        assert _until(lambda: (1, 2) in sim.shots)
        assert sim.shots == [(0, 1), (0, 2), (1, 2)]
    finally:
        client.stop()
        sim.stop()


def test_heartbeat_reply_is_not_mistaken_for_a_shot_ack(fast_timeouts):
    client = _client(1)
    with client._shot_lock:  # pylint: disable=protected-access
        client._awaiting = transport_mod._Outgoing(b"{}", deadline=time.monotonic() + 5)  # pylint: disable=protected-access
    from openflight.sim.types import ShotAck

    client._note_shot_ack(ShotAck(ok=True, message="Heartbeat OK"))  # pylint: disable=protected-access
    assert client.has_pending_shot
    client._note_shot_ack(ShotAck(ok=True, message="Shot received successfully"))  # pylint: disable=protected-access
    assert not client.has_pending_shot


def test_held_shot_that_waits_too_long_is_dropped(monkeypatch):
    monkeypatch.setattr(transport_mod, "PENDING_SHOT_TTL_S", 0.2)
    port = _free_port()
    client = _client(port)
    delivered = []
    client.start()
    sim = None
    try:
        assert not client.deliver(_shot_bytes(client), on_delivered=lambda: delivered.append(1))
        time.sleep(0.4)
        sim = ScriptedSim(port=port)
        assert _until(lambda: client.is_connected())
        time.sleep(0.3)
        assert sim.shots == [] and delivered == []
        assert not client.has_pending_shot
    finally:
        client.stop()
        if sim:
            sim.stop()


def test_newer_held_shot_replaces_an_older_one(fast_timeouts):
    port = _free_port()
    client = _client(port, backoff_seconds=(0.05,))
    client.start()
    sim = None
    try:
        time.sleep(0.1)
        assert not client.deliver(_shot_bytes(client, 1))
        assert not client.deliver(_shot_bytes(client, 2))
        sim = ScriptedSim(port=port)
        assert _until(lambda: sim.shots == [(0, 2)], timeout=2.0)
        time.sleep(0.3)
        assert sim.shots == [(0, 2)]
    finally:
        client.stop()
        if sim:
            sim.stop()


def test_stop_wakes_a_long_backoff_promptly():
    client = _client(_free_port(), backoff_seconds=(30.0,))
    client.start()
    time.sleep(0.2)
    started = time.time()
    client.stop()
    assert time.time() - started < 1.5


# --- the club a shot was hit with, for a simulator that reads it -----------


def _connector(port, **kw):
    from openflight.sim import build_connector
    from openflight.sim.config import ConnectorConfig

    cfg = ConnectorConfig(
        type="gspro", enabled=True, host="127.0.0.1", port=port, heartbeat_interval_s=60
    )
    return build_connector(cfg, backoff_seconds=(0.05, 0.1, 0.2), **kw)


def _iron_shot(n=7) -> ResolvedShot:
    resolved = _resolved(n)
    resolved.club = ClubType.IRON_7
    return resolved


def test_open_birdie_is_told_the_club_the_shot_was_hit_with(fast_timeouts):
    """A club changed on OpenFlight's screen is the club Open-Birdie files the shot under."""
    sim = ScriptedSim(name="Open-Birdie")
    connector = _connector(sim.port)
    connector.start()
    try:
        assert _until(lambda: connector.peer == "Open-Birdie")
        assert connector.deliver_shot(_iron_shot())
        assert _until(lambda: len(sim.shot_messages) == 1)
        assert sim.shot_messages[0]["ClubName"] == "I7"
    finally:
        connector.stop()
        sim.stop()


@pytest.mark.parametrize("name", ["", "GSPro", "OpenGolfSim"])
def test_a_simulator_that_is_not_open_birdie_is_sent_no_club_name(fast_timeouts, name):
    sim = ScriptedSim(name=name)
    connector = _connector(sim.port)
    connector.start()
    try:
        assert _until(connector.is_connected)
        assert _until(lambda: connector.peer == name)
        time.sleep(0.1)  # the greeting, if any, has been read
        assert connector.deliver_shot(_iron_shot())
        assert _until(lambda: len(sim.shot_messages) == 1)
        assert "ClubName" not in sim.shot_messages[0]
    finally:
        connector.stop()
        sim.stop()


def test_a_shot_held_through_a_dropped_link_still_names_its_club(fast_timeouts):
    """The shot is built when it is hit, for the simulator last seen on this link."""
    sim = ScriptedSim(name="Open-Birdie")
    port = sim.port
    connector = _connector(port)
    connector.start()
    replacement = None
    try:
        assert _until(lambda: connector.peer == "Open-Birdie")
        sim.stop()  # the simulator's machine left the network
        assert _until(lambda: not connector.is_connected())
        assert not connector.deliver_shot(_iron_shot())
        replacement = ScriptedSim(port=port, name="Open-Birdie")
        assert _until(lambda: len(replacement.shot_messages) == 1)
        assert replacement.shot_messages[0]["ClubName"] == "I7"
    finally:
        connector.stop()
        if replacement:
            replacement.stop()


def test_a_shot_hit_before_any_simulator_was_seen_carries_no_club_name(fast_timeouts):
    """Nobody has named themselves yet: send what every simulator understands."""
    port = _free_port()
    connector = _connector(port)
    connector.start()
    sim = None
    try:
        time.sleep(0.2)
        assert not connector.deliver_shot(_iron_shot())
        sim = ScriptedSim(port=port, name="Open-Birdie")
        assert _until(lambda: len(sim.shot_messages) == 1)
        assert "ClubName" not in sim.shot_messages[0]
    finally:
        connector.stop()
        if sim:
            sim.stop()
