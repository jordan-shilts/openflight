"""Tests for gspro.codec — OpenConnectV1 wire serialization + inbound parsing."""
import json

import pytest

from openflight.clubs import ClubType
from openflight.gspro.codec import CLUB_NAME_PEERS, GSProCodec
from openflight.gspro.state import gspro_code_to_club
from openflight.sim.types import PlayerUpdate, ResolvedShot, ShotAck, SimError


def _resolved(**kw) -> ResolvedShot:
    base = dict(
        shot_number=1, ball_speed_mph=140.0, vla=12.0, hla=1.5,
        total_spin_rpm=2500.0, spin_axis_deg=-3.0, back_spin_rpm=2496.6,
        side_spin_rpm=-130.8, carry_yards=255.0, club_path_deg=0.5,
        club=ClubType.DRIVER, club_speed_mph=110.0,
        provenance={},
    )
    base.update(kw)
    return ResolvedShot(**base)


def _build(codec, resolved, **kw) -> dict:
    return json.loads(codec.build_shot(resolved, **kw).decode("utf-8"))


def test_build_shot_full_payload():
    p = _build(GSProCodec(), _resolved())
    assert p["DeviceID"] == "OpenFlight"
    assert p["Units"] == "Yards"
    assert p["ShotNumber"] == 1
    assert p["APIversion"] == "1" and isinstance(p["APIversion"], str)
    assert p["BallData"]["Speed"] == 140.0
    assert p["BallData"]["VLA"] == 12.0
    assert p["BallData"]["HLA"] == 1.5
    assert p["BallData"]["TotalSpin"] == 2500.0
    assert p["BallData"]["SpinAxis"] == -3.0
    assert p["BallData"]["CarryDistance"] == 255.0
    assert p["ClubData"]["Speed"] == 110.0
    assert p["ClubData"]["Path"] == 0.5
    assert p["ShotDataOptions"]["ContainsClubData"] is True


def test_build_shot_no_club_speed_drops_club_flag():
    p = _build(GSProCodec(), _resolved(club_speed_mph=None))
    assert p["ClubData"]["Speed"] == 0.0
    assert p["ShotDataOptions"]["ContainsClubData"] is False


def test_build_shot_sends_attack_angle_when_resolved():
    p = _build(GSProCodec(), _resolved(attack_angle_deg=-2.94))
    assert p["ClubData"]["AngleOfAttack"] == -2.9
    assert p["ShotDataOptions"]["ContainsClubData"] is True
    # an attack angle alone (no club speed) still carries club data
    p = _build(GSProCodec(), _resolved(club_speed_mph=None, attack_angle_deg=1.2))
    assert p["ClubData"]["AngleOfAttack"] == 1.2
    assert p["ShotDataOptions"]["ContainsClubData"] is True


def test_build_shot_without_attack_angle_leaves_it_zero():
    p = _build(GSProCodec(), _resolved())
    assert p["ClubData"]["AngleOfAttack"] == 0.0


def test_build_shot_options_flags():
    opts = _build(GSProCodec(), _resolved())["ShotDataOptions"]
    assert opts["ContainsBallData"] is True
    assert opts["LaunchMonitorIsReady"] is True
    assert opts["LaunchMonitorBallDetected"] is True
    assert opts["IsHeartBeat"] is False


def test_device_id_and_units_configurable():
    p = _build(GSProCodec(device_id="Bay7", units="Meters"), _resolved())
    assert p["DeviceID"] == "Bay7"
    assert p["Units"] == "Meters"


def test_heartbeat_bytes_shape():
    obj = json.loads(GSProCodec().heartbeat_bytes().decode("utf-8"))
    assert obj["ShotDataOptions"]["IsHeartBeat"] is True
    assert obj["ShotDataOptions"]["LaunchMonitorIsReady"] is True
    assert obj["ShotDataOptions"]["LaunchMonitorBallDetected"] is False


def test_on_connect_bytes_none():
    assert GSProCodec().on_connect_bytes() is None


def test_parse_player_update_code_201():
    raw = json.dumps({"Code": 201, "Player": {"Handed": "LH", "Club": "I7"}}).encode()
    events = GSProCodec().parse_inbound(raw)
    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, PlayerUpdate)
    assert evt.handed == "LH"
    assert evt.club is ClubType.IRON_7


def test_parse_player_update_putter():
    raw = json.dumps({"Code": 201, "Player": {"Handed": "RH", "Club": "PT"}}).encode()
    (evt,) = GSProCodec().parse_inbound(raw)
    assert isinstance(evt, PlayerUpdate)
    assert evt.club is ClubType.PUTTER


def test_parse_shot_ack_code_200():
    events = GSProCodec().parse_inbound(json.dumps({"Code": 200, "Message": "OK"}).encode())
    assert isinstance(events[0], ShotAck)
    assert events[0].ok is True


def test_parse_error_code_5xx():
    events = GSProCodec().parse_inbound(json.dumps({"Code": 501, "Message": "bad"}).encode())
    assert isinstance(events[0], SimError)
    assert events[0].message == "bad"


def test_parse_unknown_code_yields_nothing():
    assert GSProCodec().parse_inbound(json.dumps({"Code": 0}).encode()) == []


def test_fields_for_target_lists_logical_fields():
    fields = GSProCodec().fields_for_target()
    assert "ball_speed" in fields and "carry" in fields and "club_path" in fields
    assert "attack_angle" in fields


def test_name_defaults_to_gspro_and_is_configurable():
    assert GSProCodec().name == "gspro"
    # OGS reaches this same OpenConnect codec under its own target name.
    assert GSProCodec(name="opengolfsim").name == "opengolfsim"


def test_parse_inbound_reports_a_named_peer_before_the_event():
    from openflight.sim.types import PeerInfo

    events = GSProCodec().parse_inbound(
        json.dumps({"Code": 200, "Message": "Shot received successfully", "Simulator": "Open-Birdie"}).encode()
    )
    assert isinstance(events[0], PeerInfo) and events[0].name == "Open-Birdie"
    assert isinstance(events[1], ShotAck)
    player = GSProCodec().parse_inbound(
        json.dumps({"Code": 201, "Message": "GSPro Player Information", "Player": {"Club": "DR"}, "Simulator": "Open-Birdie"}).encode()
    )
    assert [type(e).__name__ for e in player] == ["PeerInfo", "PlayerUpdate"]
    plain = GSProCodec().parse_inbound(json.dumps({"Code": 200, "Message": "ok"}).encode())
    assert not any(isinstance(e, PeerInfo) for e in plain), "real GSPro: no peer name"


# --- the club a shot was hit with (ClubName), for a simulator that reads it ---
#
# OpenConnect has no field for it: the simulator tells the launch monitor the
# club, never the other way. Open-Birdie reads ClubName as an extension, so a
# club changed on OpenFlight's own screen is the club its bag files the shot
# under. Nothing else is sent it.

SELECTABLE_CLUBS = [club for club in ClubType if club is not ClubType.UNKNOWN]


def test_open_birdie_is_the_only_simulator_sent_the_club_name():
    assert CLUB_NAME_PEERS == frozenset({"Open-Birdie"})


def test_a_shot_for_open_birdie_names_its_club_in_the_open_connect_code():
    p = _build(GSProCodec(), _resolved(club=ClubType.IRON_7), peer="Open-Birdie")
    assert p["ClubName"] == "I7"


@pytest.mark.parametrize("club", SELECTABLE_CLUBS, ids=lambda club: club.value)
def test_every_club_is_named_in_the_code_the_simulator_would_send_back(club):
    """One vocabulary both ways: the code we send reads back as the same club."""
    p = _build(GSProCodec(), _resolved(club=club), peer="Open-Birdie")
    assert gspro_code_to_club(p["ClubName"]) is club


def test_an_unknown_club_is_not_named_so_the_simulator_keeps_its_own_pick():
    p = _build(GSProCodec(), _resolved(club=ClubType.UNKNOWN), peer="Open-Birdie")
    assert "ClubName" not in p


@pytest.mark.parametrize("peer", ["", "GSPro", "OpenGolfSim", "PAR-TEE", "open-birdie", "Open-Birdie 2"])
def test_no_other_simulator_is_sent_a_club_name(peer):
    p = _build(GSProCodec(), _resolved(club=ClubType.IRON_7), peer=peer)
    assert "ClubName" not in p


def test_a_shot_for_any_other_simulator_is_the_same_bytes_as_before():
    """GSPro, OpenGolfSim and PAR-TEE must see no difference at all."""
    codec = GSProCodec()
    resolved = _resolved(club=ClubType.IRON_7)
    expected = (
        b'{"DeviceID":"OpenFlight","Units":"Yards","ShotNumber":1,"APIversion":"1",'
        b'"BallData":{"Speed":140.0,"SpinAxis":-3.0,"TotalSpin":2500.0,"BackSpin":2497.0,'
        b'"SideSpin":-131.0,"HLA":1.5,"VLA":12.0,"CarryDistance":255.0},'
        b'"ClubData":{"Speed":110.0,"AngleOfAttack":0.0,"FaceToTarget":0.0,"Lie":0.0,"Loft":0.0,'
        b'"Path":0.5,"SpeedAtImpact":0.0,"VerticalFaceImpact":0.0,"HorizontalFaceImpact":0.0,'
        b'"ClosureRate":0.0},'
        b'"ShotDataOptions":{"ContainsBallData":true,"ContainsClubData":true,'
        b'"LaunchMonitorIsReady":true,"LaunchMonitorBallDetected":true,"IsHeartBeat":false}}'
    )
    assert codec.build_shot(resolved) == expected
    assert codec.build_shot(resolved, peer="GSPro") == expected


def test_the_club_name_is_the_only_difference_for_open_birdie():
    codec = GSProCodec()
    resolved = _resolved(club=ClubType.IRON_7)
    plain = json.loads(codec.build_shot(resolved))
    named = json.loads(codec.build_shot(resolved, peer="Open-Birdie"))
    assert named.pop("ClubName") == "I7"
    assert named == plain


def test_a_heartbeat_never_carries_a_club_name():
    assert "ClubName" not in json.loads(GSProCodec().heartbeat_bytes())
