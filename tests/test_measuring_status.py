"""The kiosk's ready/measuring status for the camera-triggered radar recorder."""

import pytest

from openflight import server as server_module
from openflight.measuring_status import MeasuringStatus, parse_report


class FakeTimer:
    """Stands in for threading.Timer: fires only when a test says so."""

    created = []

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.cancelled = False
        self.started = False
        FakeTimer.created.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def status():
    FakeTimer.created = []
    changes = []
    now = [100.0]
    tracker = MeasuringStatus(changes.append, clock=lambda: now[0], timer_factory=FakeTimer)
    return tracker, changes, now


def test_nothing_reported_means_offline(status):
    tracker, changes, _now = status

    assert tracker.snapshot() == {"state": "offline"}
    assert changes == []


def test_ready_then_measuring_are_each_announced_once(status):
    tracker, changes, _now = status

    tracker.report("ready")
    tracker.report("measuring", expected_s=7.3)

    assert changes == [
        {"state": "ready"},
        {"state": "measuring", "expected_s": 7.3, "elapsed_s": 0.0},
    ]


def test_a_repeated_state_is_a_heartbeat_not_news(status):
    tracker, changes, now = status
    tracker.report("measuring", expected_s=7.3)

    now[0] += 3.0
    reply = tracker.report("measuring", expected_s=9.9)

    # The countdown on screen keeps its start and its length.
    assert len(changes) == 1
    assert reply == {"state": "measuring", "expected_s": 7.3, "elapsed_s": 3.0}


def test_a_late_client_learns_how_far_into_the_wait_it_is(status):
    tracker, _changes, now = status
    tracker.report("measuring", expected_s=7.3)

    now[0] += 2.5

    assert tracker.snapshot() == {"state": "measuring", "expected_s": 7.3, "elapsed_s": 2.5}


def test_the_result_rides_with_ready_once(status):
    tracker, changes, _now = status
    tracker.report("measuring", expected_s=7.3)

    tracker.report("ready", result="no_ball")
    tracker.report("ready")

    assert changes[1:] == [{"state": "ready", "result": "no_ball"}]
    assert tracker.snapshot() == {"state": "ready"}


def test_a_result_is_news_even_when_already_ready(status):
    tracker, changes, _now = status
    tracker.report("ready")

    tracker.report("ready", result="shot")

    assert changes == [{"state": "ready"}, {"state": "ready", "result": "shot"}]


def test_a_recorder_that_goes_quiet_is_shown_offline(status):
    tracker, changes, _now = status
    tracker.report("ready")
    [timer] = FakeTimer.created
    assert timer.started and timer.interval == pytest.approx(30.0)

    timer.function()

    assert changes == [{"state": "ready"}, {"state": "offline"}]
    assert tracker.snapshot() == {"state": "offline"}


def test_each_heartbeat_pushes_the_deadline_back(status):
    tracker, _changes, _now = status
    tracker.report("ready")
    tracker.report("ready")

    first, second = FakeTimer.created
    assert first.cancelled
    assert second.started and not second.cancelled


def test_a_goodbye_needs_no_deadline(status):
    tracker, changes, _now = status
    tracker.report("ready")

    tracker.report("offline")

    assert changes[-1] == {"state": "offline"}
    assert all(timer.cancelled for timer in FakeTimer.created)
    assert len(FakeTimer.created) == 1


def test_expiry_after_a_goodbye_says_nothing_twice(status):
    tracker, changes, _now = status
    tracker.report("ready")
    [timer] = FakeTimer.created
    tracker.report("offline")

    timer.function()

    assert changes == [{"state": "ready"}, {"state": "offline"}]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "JSON object"),
        ([], "JSON object"),
        ({}, "state must be"),
        ({"state": "busy"}, "state must be"),
        ({"state": "ready", "result": "great"}, "result must be"),
        ({"state": "measuring", "result": "shot"}, "result only comes with state ready"),
        ({"state": "measuring", "expected_s": "7"}, "expected_s"),
        ({"state": "measuring", "expected_s": True}, "expected_s"),
        ({"state": "measuring", "expected_s": 0}, "expected_s"),
        ({"state": "measuring", "expected_s": 600}, "expected_s"),
        ({"state": "measuring", "expected_s": float("nan")}, "expected_s"),
    ],
)
def test_bad_reports_are_refused_by_field(payload, message):
    with pytest.raises(ValueError, match=message):
        parse_report(payload)


def test_a_good_report_is_normalised():
    assert parse_report({"state": "measuring", "expected_s": 7}) == {
        "state": "measuring",
        "result": None,
        "expected_s": 7.0,
    }


# -- the endpoint ---------------------------------------------------------


@pytest.fixture
def kiosk(monkeypatch):
    """A --mock kiosk with its own status tracker and a record of socket events."""
    FakeTimer.created = []
    events = []
    monkeypatch.setattr(
        server_module.socketio, "emit", lambda event, payload=None: events.append((event, payload))
    )
    tracker = MeasuringStatus(server_module._emit_measuring_status, timer_factory=FakeTimer)
    monkeypatch.setattr(server_module, "measuring_status", tracker)
    monkeypatch.setattr(server_module, "monitor", server_module.MockLaunchMonitor())
    return events


def post(payload, remote_addr="127.0.0.1"):
    return server_module.app.test_client().post(
        "/api/shots/measuring", json=payload, environ_base={"REMOTE_ADDR": remote_addr}
    )


def test_the_recorder_starts_the_wait_on_screen(kiosk):
    response = post({"state": "measuring", "expected_s": 7.3})

    assert response.status_code == 200
    assert response.get_json()["state"] == "measuring"
    [(event, payload)] = kiosk
    assert event == "measuring_status"
    assert payload["state"] == "measuring"
    assert payload["expected_s"] == pytest.approx(7.3)


def test_heartbeats_do_not_reach_the_screen(kiosk):
    post({"state": "ready"})
    post({"state": "ready"})

    assert kiosk == [("measuring_status", {"state": "ready"})]


def test_only_the_pi_itself_may_report(kiosk):
    response = post({"state": "ready"}, remote_addr="192.168.1.50")

    assert response.status_code == 403
    assert kiosk == []


def test_a_real_radar_session_takes_no_recorder_status(kiosk, monkeypatch):
    monkeypatch.setattr(server_module, "monitor", object())

    assert post({"state": "ready"}).status_code == 409
    assert kiosk == []


def test_a_bad_report_is_a_400_that_names_the_field(kiosk):
    response = post({"state": "measuring", "expected_s": -1})

    assert response.status_code == 400
    assert "expected_s" in response.get_json()["error"]
    assert kiosk == []


def test_a_new_screen_is_told_the_current_state(kiosk):
    post({"state": "ready"})
    kiosk.clear()

    server_module.handle_connect()

    assert ("measuring_status", {"state": "ready"}) in kiosk


# -- radar-only swings and the camera -------------------------------------


def enriched(mode, monkeypatch):
    """One shot through the optional-hardware stage with no camera in the app."""
    from datetime import datetime

    from openflight.clubs import ClubType
    from openflight.launch_monitor import Shot

    monkeypatch.setattr(server_module, "camera_capture_runtime", None)
    monkeypatch.setattr(server_module, "iwr6843_runtime", None)
    monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
    shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club=ClubType.IRON_7, mode=mode)
    server_module._enrich_shot_from_optional_hardware(shot)
    return shot


def test_a_radar_only_swing_carries_no_camera_rejection(monkeypatch):
    shot = enriched(server_module.MEASURED_SHOT_MODE, monkeypatch)

    # The recorder owns the camera, so there was never a clip to fuse.
    assert shot.experimental_fused_status is None
    assert shot.experimental_camera_horizontal_status is None


def test_an_ops_shot_still_reports_its_missing_clip(monkeypatch):
    shot = enriched("rolling-buffer", monkeypatch)

    assert shot.experimental_fused_status == "rejected_no_camera_capture"


def test_the_screen_is_told_a_swing_is_radar_only(monkeypatch):
    from datetime import datetime

    from openflight.clubs import ClubType
    from openflight.launch_monitor import Shot

    def as_sent(mode):
        shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club=ClubType.IRON_7, mode=mode)
        return server_module.shot_to_dict(shot)

    # Live lays a radar-only swing out as five tiles, so it has to know.
    assert as_sent(server_module.MEASURED_SHOT_MODE)["mode"] == "radar-only"
    # For every other shot the mode stays a log-only detail.
    assert "mode" not in as_sent("rolling-buffer")
    assert "mode" not in as_sent("mock")
