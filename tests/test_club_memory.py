"""The kiosk starts on the club picked last time, not on Driver."""

import json

import pytest

from openflight import server as server_module
from openflight.club_memory import CLUB_PATH_ENV, load_saved_club, resolve_club_path, save_club
from openflight.clubs import ClubType
from openflight.sim import PlayerState as SimPlayerState, PlayerUpdate


def test_a_saved_club_comes_back(tmp_path):
    path = tmp_path / "nested" / "club.json"

    assert save_club(ClubType.IRON_7, path) is True

    assert load_saved_club(path) is ClubType.IRON_7
    assert json.loads(path.read_text()) == {"club": "7-iron"}
    # The temp file was renamed into place, not left behind.
    assert [item.name for item in path.parent.iterdir()] == ["club.json"]


def test_no_file_means_no_saved_club(tmp_path):
    assert load_saved_club(tmp_path / "missing.json") is None


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", '"7-iron"', "{}", '{"club": "banana"}', '{"club": 7}', '{"club": null}'],
)
def test_an_unusable_file_is_ignored(tmp_path, content):
    path = tmp_path / "club.json"
    path.write_text(content)

    assert load_saved_club(path) is None


def test_unknown_is_never_restored(tmp_path):
    path = tmp_path / "club.json"
    path.write_text(json.dumps({"club": ClubType.UNKNOWN.value}))

    assert load_saved_club(path) is None


def test_a_failed_save_reports_false_and_leaves_no_temp_file(tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")

    assert save_club(ClubType.PW, blocker / "club.json") is False
    assert [item.name for item in tmp_path.iterdir()] == ["not-a-folder"]


def test_the_path_is_the_argument_then_the_env_then_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv(CLUB_PATH_ENV, str(tmp_path / "env.json"))

    assert resolve_club_path(tmp_path / "arg.json") == tmp_path / "arg.json"
    assert resolve_club_path() == tmp_path / "env.json"
    monkeypatch.delenv(CLUB_PATH_ENV)
    assert resolve_club_path().name == "club.json"


# -- the server -----------------------------------------------------------


class Monitor:
    def __init__(self, fail=False):
        self.clubs = []
        self.fail = fail

    def set_club(self, club):
        if self.fail:
            raise RuntimeError("radar busy")
        self.clubs.append(club)


@pytest.fixture
def quiet_server(monkeypatch):
    monkeypatch.setattr(server_module, "active_club", ClubType.DRIVER)
    monkeypatch.setattr(server_module, "ble_publisher", None)
    monkeypatch.setattr(server_module.socketio, "emit", lambda *_args, **_kwargs: None)
    return server_module


def test_picking_a_club_saves_it(quiet_server, monkeypatch):
    monkeypatch.setattr(quiet_server, "monitor", Monitor())

    _response, status = quiet_server.apply_club_selection({"club": "7-iron"})

    assert status == 200
    assert load_saved_club() is ClubType.IRON_7


def test_a_refused_pick_saves_nothing(quiet_server, monkeypatch):
    monkeypatch.setattr(quiet_server, "monitor", Monitor(fail=True))

    _response, status = quiet_server.apply_club_selection({"club": "7-iron"})

    assert status == 500
    assert load_saved_club() is None


def test_the_saved_club_is_applied_at_start(quiet_server, monkeypatch):
    monitor = Monitor()
    monkeypatch.setattr(quiet_server, "monitor", monitor)
    save_club(ClubType.PW)

    quiet_server._restore_saved_club()

    assert monitor.clubs == [ClubType.PW]
    assert quiet_server.active_club is ClubType.PW


def test_with_nothing_saved_the_start_stays_on_driver(quiet_server, monkeypatch):
    monitor = Monitor()
    monkeypatch.setattr(quiet_server, "monitor", monitor)

    quiet_server._restore_saved_club()

    assert monitor.clubs == []
    assert quiet_server.active_club is ClubType.DRIVER


def test_a_monitor_that_refuses_the_saved_club_keeps_driver(quiet_server, monkeypatch):
    monkeypatch.setattr(quiet_server, "monitor", Monitor(fail=True))
    save_club(ClubType.PW)

    quiet_server._restore_saved_club()

    assert quiet_server.active_club is ClubType.DRIVER


def test_a_mock_kiosk_starts_on_the_saved_club(quiet_server, monkeypatch):
    monkeypatch.setattr(quiet_server, "monitor", None)
    monkeypatch.setattr(quiet_server, "get_session_logger", lambda: None)
    save_club(ClubType.IRON_7)

    quiet_server.start_monitor(mock=True)
    try:
        assert quiet_server.monitor.current_club is ClubType.IRON_7
        assert quiet_server.active_club is ClubType.IRON_7
    finally:
        quiet_server.stop_monitor()


def test_a_simulator_moving_to_the_putter_is_not_remembered(quiet_server, monkeypatch):
    monkeypatch.setattr(quiet_server, "monitor", Monitor())
    monkeypatch.setattr(quiet_server, "sim_player_state", SimPlayerState())
    monkeypatch.setattr(quiet_server, "get_session_logger", lambda: None)
    save_club(ClubType.IRON_7)

    quiet_server._sim_on_inbound("gspro", PlayerUpdate(handed="RH", club=ClubType.PUTTER))

    assert quiet_server.active_club is ClubType.PUTTER
    assert load_saved_club() is ClubType.IRON_7
