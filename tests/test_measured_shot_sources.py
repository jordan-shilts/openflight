"""Camera launch, camera club delivery and OPS243 club speed on recorder-posted swings.

The swing recorder posts each swing to /api/shots/measured. Its camera's
ball-flight fit now measures the launch angles the radar could not, so each
axis names its sensor; it may also send the camera's club path and attack
angle, the OPS243's club speed, and both sensors' own readings for analysis.
"""

import json
import math
from datetime import datetime
from types import SimpleNamespace

import pytest

from openflight import server as server_module
from openflight.ble.protocol import build_shot_event
from openflight.clubs import ClubType
from openflight.d_plane import SPIN_AXIS_MIN_HORIZONTAL_CONFIDENCE
from openflight.launch_monitor import Shot
from openflight.measured_shot import MeasuredLaunch, MeasuredSwing, parse_measured_shot
from openflight.rig_setup import RigSetupStore
from openflight.server import (
    ANGLE_CONFIDENCE_CEILING,
    ANGLE_CONFIDENCE_FLOOR,
    MEASURED_SHOT_MODE,
    RECORDER_CAMERA_DELIVERY_STATUS,
    camera_fit_confidence,
    camera_horizontal_confidence,
    horizontal_confidence_from,
    shot_to_dict,
)
from openflight.session_logger import SessionLogger
from openflight.spin_estimate import club_anchored_spin_rpm


def camera_fit(**overrides):
    """The camera fit's diagnostics for one clean full swing."""
    fit = {
        "status": "ok",
        "accepted": True,
        "vla_deg": 16.0,
        "hla_deg": 1.0,
        "hla_axis_deg": 3.1,
        "aim_yaw_deg": 2.1,
        "bump_yaw_deg": 0.0,
        "aim_status": "set",
        "hits": 12,
        "frames": 12,
        "t0_ms": 1.25,
        "fit_score": 310.5,
    }
    fit.update(overrides)
    return fit


def radar_reading(**overrides):
    """The radar's own reading of the same swing: a spread-out single channel."""
    reading = {
        "vla_deg": 24.5,
        "hla_deg": -0.4,
        "single_channel": True,
        "component_std_deg": 6.0,
        "horizontal_coherence": 0.91,
    }
    reading.update(overrides)
    return reading


def camera_swing(**overrides):
    """A 7-iron the camera measured: 16 deg up, 1 deg right, radar kept aside."""
    payload = {
        "ball_speed_mph": 120.0,
        "launch_angle_vertical": 16.0,
        "launch_angle_vertical_source": "camera",
        "launch_angle_horizontal": 1.0,
        "launch_angle_horizontal_source": "camera",
        "camera_launch": camera_fit(),
        "radar_launch": radar_reading(),
    }
    payload.update(overrides)
    return payload


def club_delivery(**overrides):
    payload = {
        "club_path_deg": -3.0,
        "club_delivery_source": "camera",
        "club_delivery_confidence": "high",
    }
    payload.update(overrides)
    return payload


def post(payload, remote_addr="127.0.0.1"):
    return server_module.app.test_client().post(
        "/api/shots/measured", json=payload, environ_base={"REMOTE_ADDR": remote_addr}
    )


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A rig that has never been set up: no ball distance, no straight line."""
    store = RigSetupStore(tmp_path / "rig.json")
    monkeypatch.setattr(server_module, "rig_setup_store", store)
    return store


@pytest.fixture
def recorded(monkeypatch, rig):
    """A --mock kiosk that only records posted shots (validation and parsing)."""
    shots = []
    mock = server_module.MockLaunchMonitor()
    mock.set_club(ClubType.IRON_7)
    mock.start(shot_callback=shots.append)
    monkeypatch.setattr(server_module, "monitor", mock)
    monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)
    return shots


class _Kiosk:
    """What a --mock kiosk publishes for posted swings, through the real pipeline."""

    def __init__(self, session_log):
        self.session_log = session_log
        self.sim_shots = []
        self.phone_shots = []

    def logged_shots(self):
        lines = self.session_log.session_path.read_text().strip().split("\n")
        entries = [json.loads(line) for line in lines]
        return [entry for entry in entries if entry["type"] == "shot_detected"]

    def swing(self, payload, club=ClubType.IRON_7):
        server_module.monitor.set_club(club)
        response = post(payload)
        assert response.status_code == 201, response.get_json()
        _wait_for_finalization()
        return server_module.monitor.get_shots()[-1]


def _wait_for_finalization(timeout_s: float = 2.0) -> None:
    with server_module._shot_finalization_condition:
        idle = server_module._shot_finalization_condition.wait_for(
            lambda: (
                not server_module._shot_finalization_order
                and not server_module._shot_finalization_running
            ),
            timeout=timeout_s,
        )
    assert idle, "shot finalization did not become idle"


@pytest.fixture
def kiosk(monkeypatch, tmp_path, rig):
    """A --mock kiosk with no hardware, a session log, a simulator and phones."""
    server_module._reset_shot_sequence()
    mock = server_module.MockLaunchMonitor()
    mock.start(shot_callback=server_module.on_shot_detected)
    monkeypatch.setattr(server_module, "monitor", mock)
    for hardware in (
        "iwr6843_runtime",
        "camera_capture_runtime",
        "kld7_vertical",
        "kld7_horizontal",
        "inclinometer_service",
    ):
        monkeypatch.setattr(server_module, hardware, None)
    monkeypatch.setattr(server_module, "debug_mode", False)
    monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

    session_log = SessionLogger(log_dir=tmp_path / "sessions", enabled=True)
    session_log.start_session(mode="mock")
    monkeypatch.setattr(server_module, "get_session_logger", lambda: session_log)

    published = _Kiosk(session_log)

    def deliver_shot(resolved, on_delivered=None):  # pylint: disable=unused-argument
        published.sim_shots.append(resolved)
        return True

    monkeypatch.setattr(
        server_module, "sim_connectors", [SimpleNamespace(name="sim", deliver_shot=deliver_shot)]
    )
    monkeypatch.setattr(
        server_module,
        "_publish_phone_shot",
        lambda shot_data, **kwargs: published.phone_shots.append(shot_data),
    )
    yield published
    _wait_for_finalization()
    session_log.end_session()


# --- absent fields: today's behaviour exactly ---------------------------------


class TestWithoutTheNewFields:
    def test_a_radar_swing_is_recorded_as_before(self, recorded):
        response = post(
            {
                "ball_speed_mph": 118.0,
                "launch_angle_vertical": 16.4,
                "vertical_component_std_deg": 6.0,
                "launch_angle_horizontal": -2.3,
                "horizontal_coherence": 0.8,
            }
        )

        assert response.status_code == 201
        [shot] = recorded
        evidence = SimpleNamespace(single_channel=False, component_std_deg=6.0)
        assert shot.launch_angle_vertical == pytest.approx(16.4)
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.angle_source == "radar"
        assert shot.launch_angle_vertical_confidence == server_module.vertical_confidence(evidence)
        assert shot.launch_angle_horizontal_source == "radar"
        assert shot.iwr6843_horizontal_deg == pytest.approx(-2.3)
        assert shot.launch_angle_horizontal_confidence == horizontal_confidence_from(0.8)
        assert shot.club_speed_mph is None
        assert shot.club_speed_source is None
        assert shot.camera_launch is None
        assert shot.radar_launch is None
        assert shot.experimental_fused_status is None
        assert shot.experimental_fused_club_path_deg is None
        assert shot.experimental_fused_attack_angle_deg is None

    def test_naming_the_radar_changes_nothing(self, recorded):
        legacy = {
            "ball_speed_mph": 118.0,
            "launch_angle_vertical": 16.4,
            "vertical_single_channel": True,
            "vertical_component_std_deg": 1.5,
            "launch_angle_horizontal": -2.3,
            "horizontal_coherence": 0.8,
        }
        named = {
            **legacy,
            "launch_angle_vertical_source": "radar",
            "launch_angle_horizontal_source": "radar",
        }

        assert post(legacy).status_code == 201
        assert post(named).status_code == 201

        first, second = (shot.to_dict() for shot in recorded)
        for entry in (first, second):
            entry.pop("timestamp")
        assert first == second

    def test_a_ball_speed_alone_is_still_a_shot(self, recorded):
        assert post({"ball_speed_mph": 110}).status_code == 201

        [shot] = recorded
        assert shot.launch_angle_vertical is None
        assert shot.launch_angle_horizontal is None
        assert shot.angle_source is None
        assert shot.club_speed_mph is None

    def test_a_radar_only_swing_still_gets_the_five_tile_row(self, recorded):
        post({"ball_speed_mph": 110, "launch_angle_vertical": 15.0})

        [shot] = recorded
        assert shot_to_dict(shot)["mode"] == MEASURED_SHOT_MODE


class TestParseMeasuredShot:
    """The payload parser on its own, without Flask."""

    @pytest.mark.parametrize("payload", [None, [110], "118.2", 118.2])
    def test_anything_but_an_object_is_refused(self, payload):
        with pytest.raises(ValueError, match="Send a JSON object"):
            parse_measured_shot(payload)

    def test_a_ball_speed_alone_parses_to_nothing_else(self):
        assert parse_measured_shot({"ball_speed_mph": 110}) == MeasuredSwing(
            ball_speed_mph=110.0, launch=MeasuredLaunch()
        )

    def test_each_axis_goes_to_the_sensor_it_names(self):
        swing = parse_measured_shot(
            {
                "ball_speed_mph": 110,
                "launch_angle_vertical": 15.0,
                "launch_angle_vertical_source": "camera",
                "launch_angle_horizontal": -2.0,
                "horizontal_coherence": 0.9,
            }
        )

        assert swing.launch.camera_vertical_deg == 15.0
        assert swing.launch.camera_horizontal_deg is None
        assert swing.launch.radar.angle_deg is None
        assert swing.launch.radar.horizontal_deg == -2.0
        assert swing.launch.radar.horizontal_confidence == 0.9

    def test_camera_axes_leave_no_radar_evidence(self):
        swing = parse_measured_shot(camera_swing())

        assert swing.launch.radar is None
        assert swing.launch.camera_vertical_deg == 16.0
        assert swing.launch.camera_horizontal_deg == 1.0


# --- validation -----------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize(
        ("extra", "message"),
        [
            (
                {"launch_angle_vertical": 15, "launch_angle_vertical_source": "lidar"},
                "launch_angle_vertical_source must be one of: radar, camera",
            ),
            (
                {"launch_angle_vertical": 15, "launch_angle_vertical_source": "Camera"},
                "launch_angle_vertical_source must be one of",
            ),
            (
                {"launch_angle_horizontal": 1, "launch_angle_horizontal_source": 1},
                "launch_angle_horizontal_source must be one of: radar, camera",
            ),
            (
                {"launch_angle_horizontal_source": ["camera"]},
                "launch_angle_horizontal_source must be one of",
            ),
            ({"camera_launch": "fit"}, "camera_launch must be an object"),
            ({"camera_launch": [1, 2]}, "camera_launch must be an object"),
            ({"camera_launch": {"hits": 3.5}}, "camera_launch.hits must be a whole number"),
            ({"camera_launch": {"hits": True}}, "camera_launch.hits must be a whole number"),
            ({"camera_launch": {"hits": "12"}}, "camera_launch.hits must be a whole number"),
            ({"camera_launch": {"hits": -1}}, "camera_launch.hits must be between 0 and 10000"),
            ({"camera_launch": {"frames": 10001}}, "camera_launch.frames must be between"),
            ({"camera_launch": {"vla_deg": "high"}}, "camera_launch.vla_deg must be a number"),
            (
                {"camera_launch": {"hla_deg": float("nan")}},
                "camera_launch.hla_deg must be a number",
            ),
            (
                {"camera_launch": {"hla_axis_deg": float("inf")}},
                "camera_launch.hla_axis_deg must be a number",
            ),
            ({"camera_launch": {"aim_yaw_deg": 181}}, "camera_launch.aim_yaw_deg must be between"),
            (
                {"camera_launch": {"bump_yaw_deg": False}},
                "camera_launch.bump_yaw_deg must be a number",
            ),
            (
                {"camera_launch": {"aim_status": "aimed"}},
                "camera_launch.aim_status must be one of: set, unset, moved, scene_lost",
            ),
            ({"camera_launch": {"t0_ms": 1500}}, "camera_launch.t0_ms must be between"),
            ({"camera_launch": {"status": 3}}, "camera_launch.status must be a non-empty string"),
            ({"camera_launch": {"status": ""}}, "camera_launch.status must be a non-empty string"),
            (
                {"camera_launch": {"status": "x" * 41}},
                "camera_launch.status must be at most 40 characters",
            ),
            (
                {"camera_launch": {"accepted": "yes"}},
                "camera_launch.accepted must be true or false",
            ),
            (
                {"camera_launch": {"accepted": None}},
                "camera_launch.accepted must be true or false",
            ),
            (
                {
                    "launch_angle_vertical": 15,
                    "launch_angle_vertical_source": "camera",
                    "camera_launch": {"accepted": False},
                },
                "launch_angle_vertical_source is camera but camera_launch.accepted is false",
            ),
            (
                {
                    "launch_angle_horizontal": 1,
                    "launch_angle_horizontal_source": "camera",
                    "camera_launch": {"accepted": False},
                },
                "launch_angle_horizontal_source is camera but camera_launch.accepted is false",
            ),
            ({"camera_launch": {"fit_score": -1}}, "camera_launch.fit_score must be between"),
            ({"radar_launch": [24.5]}, "radar_launch must be an object"),
            ({"radar_launch": {"vla_deg": "x"}}, "radar_launch.vla_deg must be a number"),
            ({"radar_launch": {"hla_deg": -200}}, "radar_launch.hla_deg must be between"),
            (
                {"radar_launch": {"single_channel": "yes"}},
                "radar_launch.single_channel must be true or false",
            ),
            (
                {"radar_launch": {"single_channel": None}},
                "radar_launch.single_channel must be true or false",
            ),
            (
                {"radar_launch": {"component_std_deg": -1}},
                "radar_launch.component_std_deg must be between",
            ),
            (
                {"radar_launch": {"horizontal_coherence": 1.5}},
                "radar_launch.horizontal_coherence must be between",
            ),
            ({**club_delivery(), "club_path_deg": "left"}, "club_path_deg must be a number"),
            (
                {**club_delivery(), "club_path_deg": 30.5},
                "club_path_deg must be between -30 and 30",
            ),
            ({**club_delivery(), "attack_angle_deg": -31}, "attack_angle_deg must be between"),
            (
                {**club_delivery(), "attack_angle_deg": float("nan")},
                "attack_angle_deg must be a number",
            ),
            ({"club_path_deg": -3.0}, "club_delivery_source is required with club_path_deg"),
            (
                {"attack_angle_deg": -4.0, "club_delivery_confidence": "high"},
                "club_delivery_source is required",
            ),
            (
                {"club_path_deg": -3.0, "club_delivery_source": "camera"},
                "club_delivery_confidence is required with club_path_deg",
            ),
            (
                {**club_delivery(), "club_delivery_source": "radar"},
                "club_delivery_source must be one of: camera",
            ),
            (
                {**club_delivery(), "club_delivery_confidence": "withheld"},
                "club_delivery_confidence must be one of: high, medium, low",
            ),
            (
                {"club_delivery_confidence": "great"},
                "club_delivery_confidence must be one of",
            ),
            (
                {"club_speed_mph": "fast", "club_speed_source": "ops243"},
                "club_speed_mph must be a number",
            ),
            (
                {"club_speed_mph": 201, "club_speed_source": "ops243"},
                "club_speed_mph must be between 5 and 200",
            ),
            (
                {"club_speed_mph": 4.9, "club_speed_source": "ops243"},
                "club_speed_mph must be between",
            ),
            ({"club_speed_mph": 85.0}, "club_speed_source is required with club_speed_mph"),
            (
                {"club_speed_mph": 85.0, "club_speed_source": "radar"},
                "club_speed_source must be one of: ops243",
            ),
            (
                {"launch_angle_horizontal": 1, "vertical_single_channel": "yes"},
                "vertical_single_channel must be true or false",
            ),
        ],
    )
    def test_a_bad_field_refuses_the_whole_post_and_names_it(self, recorded, extra, message):
        response = post({"ball_speed_mph": 110, **extra})

        assert response.status_code == 400
        assert message in response.get_json()["error"]
        assert recorded == []

    def test_the_local_only_guard_still_holds(self, recorded):
        response = post(camera_swing(), remote_addr="192.168.1.20")

        assert response.status_code == 403
        assert recorded == []

    def test_the_mock_only_guard_still_holds(self, monkeypatch):
        monkeypatch.setattr(server_module, "monitor", SimpleNamespace(current_club=ClubType.IRON_7))

        assert post(camera_swing()).status_code == 409

    def test_the_boundaries_are_accepted(self, recorded):
        payload = camera_swing(
            camera_launch=camera_fit(
                hits=0, frames=10_000, t0_ms=-1000, fit_score=0, aim_yaw_deg=-180
            ),
            radar_launch=radar_reading(component_std_deg=0, horizontal_coherence=1),
            club_speed_mph=200,
            club_speed_source="ops243",
            **club_delivery(club_path_deg=30, attack_angle_deg=-30, club_delivery_confidence="low"),
        )

        assert post(payload).status_code == 201
        assert post({**payload, "club_speed_mph": 5, "club_path_deg": -30}).status_code == 201
        assert len(recorded) == 2

    def test_nulls_count_as_absent(self, recorded):
        payload = camera_swing(
            camera_launch=camera_fit(vla_deg=None, hla_deg=None, hits=None, aim_status=None),
            club_path_deg=None,
            attack_angle_deg=None,
            club_speed_mph=None,
            launch_angle_horizontal_source=None,
        )

        assert post(payload).status_code == 201
        [shot] = recorded
        assert shot.camera_launch["vla_deg"] is None
        assert shot.camera_launch["hits"] is None
        # A null source falls back to the radar, as when it is absent.
        assert shot.launch_angle_horizontal_source == "radar"
        assert shot.experimental_fused_status is None

    def test_a_delivery_tier_without_a_delivery_applies_nothing(self, recorded):
        response = post(
            {
                "ball_speed_mph": 110,
                "club_delivery_source": "camera",
                "club_delivery_confidence": "low",
            }
        )

        assert response.status_code == 201
        [shot] = recorded
        assert shot.experimental_fused_status is None
        assert shot.experimental_fused_club_path_confidence is None

    def test_a_club_speed_source_without_a_speed_applies_nothing(self, recorded):
        assert post({"ball_speed_mph": 110, "club_speed_source": "ops243"}).status_code == 201

        [shot] = recorded
        assert shot.club_speed_mph is None
        assert shot.club_speed_source is None

    def test_a_camera_source_without_an_angle_applies_nothing(self, recorded):
        response = post(
            {
                "ball_speed_mph": 110,
                "launch_angle_vertical_source": "camera",
                "launch_angle_horizontal_source": "camera",
            }
        )

        assert response.status_code == 201
        [shot] = recorded
        assert shot.launch_angle_vertical is None
        assert shot.launch_angle_horizontal is None
        assert shot.angle_source is None


# --- camera vertical ----------------------------------------------------------------


class TestCameraFitConfidence:
    def test_ten_hits_earn_the_ceiling(self):
        assert camera_fit_confidence({"hits": 10}) == ANGLE_CONFIDENCE_CEILING
        assert camera_fit_confidence({"hits": 40}) == ANGLE_CONFIDENCE_CEILING

    @pytest.mark.parametrize("fit", [None, {}, {"hits": None}, {"hits": 0}, {"hits": 3}])
    def test_little_or_no_evidence_gets_the_floor(self, fit):
        assert camera_fit_confidence(fit) == ANGLE_CONFIDENCE_FLOOR

    def test_fewer_hits_are_less_confident(self):
        confidences = [camera_fit_confidence({"hits": hits}) for hits in range(3, 11)]

        assert confidences == sorted(confidences)
        assert len(set(confidences)) == len(confidences)
        assert camera_fit_confidence({"hits": 7}) == pytest.approx(
            ANGLE_CONFIDENCE_FLOOR + (ANGLE_CONFIDENCE_CEILING - ANGLE_CONFIDENCE_FLOOR) * 4 / 7,
            abs=1e-3,
        )

    def test_any_measured_camera_angle_beats_the_table_estimate(self):
        estimate_confidence = server_module.estimate_launch_angle(
            ClubType.IRON_7, 120.0, club_speed_mph=85.0, spin_rpm=7000.0
        )[1]

        assert camera_fit_confidence(None) > estimate_confidence

    def test_only_an_aimed_camera_gives_a_confident_start_direction(self):
        assert camera_horizontal_confidence(camera_fit()) == ANGLE_CONFIDENCE_CEILING
        # Background hidden for this clip, last good correction used: still aimed.
        scene_lost = camera_fit(aim_status="scene_lost")
        assert camera_horizontal_confidence(scene_lost) == ANGLE_CONFIDENCE_CEILING
        for status in ("unset", "moved", None):
            fit = camera_fit(aim_status=status)
            assert camera_horizontal_confidence(fit) == ANGLE_CONFIDENCE_FLOOR
        assert camera_horizontal_confidence(None) == ANGLE_CONFIDENCE_FLOOR
        assert ANGLE_CONFIDENCE_FLOOR < SPIN_AXIS_MIN_HORIZONTAL_CONFIDENCE

    def test_aimed_but_thin_fits_are_derated_like_the_vertical(self):
        fit = camera_fit(hits=6)

        assert camera_horizontal_confidence(fit) == camera_fit_confidence(fit)
        assert ANGLE_CONFIDENCE_FLOOR < camera_fit_confidence(fit) < ANGLE_CONFIDENCE_CEILING


class TestCameraVertical:
    def test_a_camera_vertical_is_stored_as_a_camera_measurement(self, recorded):
        assert post(camera_swing(camera_launch=camera_fit(hits=7))).status_code == 201

        [shot] = recorded
        assert shot.launch_angle_vertical == pytest.approx(16.0)
        assert shot.launch_angle_vertical_source == "camera"
        assert shot.angle_source == "camera"
        assert shot.launch_angle_vertical_confidence == camera_fit_confidence({"hits": 7})
        assert shot.launch_angle_confidence == shot.launch_angle_vertical_confidence
        server_module._ensure_user_facing_launch_angles(shot)
        assert shot.launch_angle_vertical == pytest.approx(16.0)
        assert shot.launch_angle_vertical_source == "camera"

    def test_radar_evidence_does_not_derate_a_camera_vertical(self, recorded):
        post(
            camera_swing(
                vertical_single_channel=True,
                vertical_component_std_deg=9.0,
                horizontal_coherence=0.1,
            )
        )

        [shot] = recorded
        assert shot.launch_angle_vertical_confidence == ANGLE_CONFIDENCE_CEILING
        assert shot.launch_angle_horizontal_confidence == ANGLE_CONFIDENCE_CEILING

    def test_the_single_channel_gate_never_withholds_a_camera_vertical(self, recorded, monkeypatch):
        """--max-single-channel-spread 3 judges the radar's channels; the camera has none."""
        monkeypatch.setattr(server_module, "single_channel_max_spread_deg", 3.0)

        post(
            camera_swing(
                vertical_single_channel=True,
                vertical_component_std_deg=6.0,
                radar_launch=radar_reading(single_channel=True, component_std_deg=6.0),
            )
        )

        [shot] = recorded
        assert shot.launch_angle_vertical == pytest.approx(16.0)
        assert shot.launch_angle_vertical_source == "camera"

    @pytest.mark.parametrize("named", [True, False])
    def test_a_radar_vertical_is_still_gated(self, recorded, monkeypatch, named):
        monkeypatch.setattr(server_module, "single_channel_max_spread_deg", 3.0)
        payload = {
            "ball_speed_mph": 120.0,
            "launch_angle_vertical": 24.5,
            "vertical_single_channel": True,
            "vertical_component_std_deg": 6.0,
        }
        if named:
            payload["launch_angle_vertical_source"] = "radar"

        post(payload)

        [shot] = recorded
        assert shot.launch_angle_vertical is None
        server_module._ensure_user_facing_launch_angles(shot)
        assert shot.launch_angle_vertical_source == "estimated"

    def test_a_camera_vertical_with_a_radar_horizontal(self, recorded):
        post(
            camera_swing(
                launch_angle_horizontal=-2.3,
                launch_angle_horizontal_source="radar",
                horizontal_coherence=0.8,
            )
        )

        [shot] = recorded
        assert shot.launch_angle_vertical_source == "camera"
        assert shot.launch_angle_horizontal == pytest.approx(-2.3)
        assert shot.launch_angle_horizontal_source == "radar"
        assert shot.launch_angle_horizontal_confidence == horizontal_confidence_from(0.8)
        assert shot.iwr6843_horizontal_deg == pytest.approx(-2.3)


# --- camera horizontal --------------------------------------------------------------


class TestCameraHorizontal:
    def test_a_camera_horizontal_is_stored_as_a_camera_measurement(self, recorded):
        post(camera_swing(launch_angle_vertical=None))

        [shot] = recorded
        assert shot.launch_angle_horizontal == pytest.approx(1.0)
        assert shot.launch_angle_horizontal_source == "camera"
        assert shot.launch_angle_horizontal_confidence == ANGLE_CONFIDENCE_CEILING
        # Not the radar's: its own sideways reading stays in radar_launch.
        assert shot.iwr6843_horizontal_deg is None
        assert shot.radar_launch["hla_deg"] == pytest.approx(-0.4)

    @pytest.mark.parametrize("aim_status", ["unset", "moved"])
    def test_an_unaimed_camera_horizontal_is_shown_at_the_floor(self, recorded, aim_status):
        post(camera_swing(camera_launch=camera_fit(aim_status=aim_status)))

        [shot] = recorded
        assert shot.launch_angle_horizontal == pytest.approx(1.0)
        assert shot.launch_angle_horizontal_confidence == ANGLE_CONFIDENCE_FLOOR
        # The vertical does not depend on the aim.
        assert shot.launch_angle_vertical_confidence == ANGLE_CONFIDENCE_CEILING

    def test_a_camera_horizontal_needs_no_set_straight(self, recorded, rig):
        assert rig.snapshot()["straight_line"]["set"] is False

        post(camera_swing(horizontal_phase_rad=-0.8))

        [shot] = recorded
        assert shot.launch_angle_horizontal == pytest.approx(1.0)
        assert shot.launch_angle_horizontal_source == "camera"
        # The radar's start line is still collected for its own Set straight.
        assert rig.snapshot()["straight_line"]["shots"] == 1

    def test_a_radar_vertical_with_a_camera_horizontal(self, recorded):
        post(
            camera_swing(
                launch_angle_vertical=17.5,
                launch_angle_vertical_source="radar",
                vertical_component_std_deg=1.0,
            )
        )

        [shot] = recorded
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.angle_source == "radar"
        assert shot.launch_angle_horizontal_source == "camera"
        assert shot.iwr6843_horizontal_deg is None

    def test_a_camera_horizontal_with_a_withheld_radar_vertical(self, recorded, monkeypatch):
        monkeypatch.setattr(server_module, "single_channel_max_spread_deg", 3.0)

        post(
            camera_swing(
                launch_angle_vertical=24.5,
                launch_angle_vertical_source="radar",
                vertical_single_channel=True,
                vertical_component_std_deg=6.0,
            )
        )

        [shot] = recorded
        assert shot.launch_angle_vertical is None
        assert shot.launch_angle_horizontal_source == "camera"
        server_module._ensure_user_facing_launch_angles(shot)
        assert shot.launch_angle_vertical_source == "estimated"
        assert shot.launch_angle_horizontal_source == "camera"


# --- an unaccepted camera fit, and the camera's aim --------------------------------------


class TestCameraFitStatus:
    def test_an_unaccepted_fit_is_kept_as_diagnostics_only(self, recorded):
        rejected = camera_fit(status="ball_not_seen", accepted=False, hits=2, vla_deg=31.0)

        response = post(
            {
                "ball_speed_mph": 118.0,
                "launch_angle_vertical": 17.5,
                "vertical_component_std_deg": 1.0,
                "camera_launch": rejected,
            }
        )

        assert response.status_code == 201
        [shot] = recorded
        assert shot.launch_angle_vertical == pytest.approx(17.5)
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.camera_launch == rejected

    def test_an_unaccepted_fit_with_no_angles_at_all(self, recorded):
        assert (
            post(
                {
                    "ball_speed_mph": 118.0,
                    "camera_launch": camera_fit(status="ball_stayed", accepted=False),
                }
            ).status_code
            == 201
        )

        [shot] = recorded
        assert shot.launch_angle_vertical is None
        assert shot.camera_launch["status"] == "ball_stayed"

    def test_scene_lost_counts_as_aimed(self, recorded):
        post(camera_swing(camera_launch=camera_fit(aim_status="scene_lost")))

        [shot] = recorded
        assert shot.launch_angle_horizontal_confidence == ANGLE_CONFIDENCE_CEILING


class TestCameraAimOnSetup:
    """A monitor that moved since its camera was aimed shows on Setup's snapshot."""

    @pytest.fixture
    def emitted(self, monkeypatch, recorded):  # pylint: disable=unused-argument
        events = []
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload=None, **kwargs: events.append((event, payload)),
        )
        return events

    def test_a_new_rig_has_no_camera_aim(self, rig):
        assert rig.snapshot()["camera_aim"] is None

    def test_a_moved_monitor_reaches_setup(self, rig, emitted):
        # The recorder omits the sideways angle while the monitor is moved.
        payload = camera_swing(camera_launch=camera_fit(aim_status="moved"))
        del payload["launch_angle_horizontal"], payload["launch_angle_horizontal_source"]

        assert post(payload).status_code == 201

        assert rig.snapshot()["camera_aim"] == "moved"
        assert emitted[-1][0] == "rig_setup"
        assert emitted[-1][1]["camera_aim"] == "moved"

    def test_an_unchanged_aim_does_not_republish(self, rig, emitted):
        post(camera_swing())
        published = [event for event, _payload in emitted if event == "rig_setup"]
        post(camera_swing())

        assert rig.snapshot()["camera_aim"] == "set"
        assert [event for event, _payload in emitted if event == "rig_setup"] == published

    def test_setting_straight_again_clears_moved(self, rig, emitted):  # pylint: disable=unused-argument
        for phase in (-0.80, -0.78, -0.82):
            post(
                camera_swing(
                    camera_launch=camera_fit(aim_status="moved"), horizontal_phase_rad=phase
                )
            )

        response = server_module.app.test_client().post(
            "/api/rig-setup", json={"straight_line": "set"}
        )

        assert response.status_code == 200
        assert response.get_json()["camera_aim"] is None
        post(camera_swing())
        assert rig.snapshot()["camera_aim"] == "set"

    def test_no_aim_reported_leaves_setup_alone(self, rig, emitted):
        post(camera_swing(camera_launch=camera_fit(aim_status=None)))

        assert rig.snapshot()["camera_aim"] is None
        assert "rig_setup" not in [event for event, _payload in emitted]


# --- diagnostics ----------------------------------------------------------------------


class TestDiagnostics:
    def test_both_readings_are_kept_with_the_shot(self, recorded):
        post(camera_swing())

        [shot] = recorded
        assert shot.camera_launch == camera_fit()
        assert shot.radar_launch == radar_reading()
        assert shot.to_dict()["camera_launch"] == camera_fit()
        assert shot.to_dict()["radar_launch"] == radar_reading()

    def test_unknown_diagnostic_keys_are_dropped(self, recorded):
        post(camera_swing(camera_launch={**camera_fit(), "debug_blob": "x" * 100}))

        [shot] = recorded
        assert "debug_blob" not in shot.camera_launch
        assert set(shot.camera_launch) == set(camera_fit())

    def test_missing_diagnostic_keys_are_kept_as_null(self, recorded):
        post(camera_swing(camera_launch={"hits": 11}, radar_launch={"vla_deg": 22.0}))

        [shot] = recorded
        assert shot.camera_launch == {key: None for key in camera_fit()} | {"hits": 11}
        assert shot.radar_launch == {key: None for key in radar_reading()} | {"vla_deg": 22.0}

    def test_the_session_log_has_both_readings_side_by_side(self, kiosk):
        kiosk.swing(camera_swing())

        [entry] = kiosk.logged_shots()
        assert entry["launch_angle_vertical"] == pytest.approx(16.0)
        assert entry["launch_angle_vertical_source"] == "camera"
        assert entry["camera_launch"] == camera_fit()
        assert entry["radar_launch"] == radar_reading()
        assert entry["mode"] == MEASURED_SHOT_MODE


# --- club delivery from the camera ----------------------------------------------------


class TestCameraClubDelivery:
    """The camera's club path and attack angle go through the D-plane code that is
    validated against TrackMan (openflight.d_plane); the numbers below are the
    ones its server tests pin for the same 7-iron delivery."""

    def test_a_confident_camera_path_gives_face_angle_and_spin_axis(self, kiosk):
        shot = kiosk.swing(camera_swing(**club_delivery()))

        assert shot.experimental_fused_club_path_deg == pytest.approx(-3.0)
        assert shot.experimental_fused_club_path_confidence == "high"
        assert shot.experimental_fused_status == RECORDER_CAMERA_DELIVERY_STATUS
        assert shot.delivery_club_path_source == "camera"
        assert shot.delivery_attack_angle_source == "typical"
        assert shot.face_angle_deg == pytest.approx(2.2)
        assert shot.face_to_path_deg == pytest.approx(5.2)
        assert shot.spin_axis_deg == pytest.approx(10.8)
        # The canonical club fields stay a club radar's.
        assert shot.club_path_deg is None
        assert shot.club_angle_deg is None

    def test_a_camera_attack_angle_gives_dynamic_and_spin_loft(self, kiosk):
        shot = kiosk.swing(camera_swing(**club_delivery(attack_angle_deg=-5.0)))

        assert shot.delivery_attack_angle_source == "camera"
        assert shot.dynamic_loft_deg == pytest.approx(22.3)
        assert shot.spin_loft_deg == pytest.approx(27.3)
        assert shot.spin_axis_deg == pytest.approx(10.3)

    def test_a_medium_tier_is_trusted_too(self, kiosk):
        shot = kiosk.swing(camera_swing(**club_delivery(club_delivery_confidence="medium")))

        assert shot.spin_axis_deg == pytest.approx(10.8)

    def test_a_low_tier_is_shown_but_drives_nothing(self, kiosk):
        shot = kiosk.swing(camera_swing(**club_delivery(club_delivery_confidence="low")))

        assert shot.experimental_fused_club_path_deg == pytest.approx(-3.0)
        assert shot.experimental_fused_club_path_confidence == "low"
        assert shot.face_angle_deg is None
        assert shot.spin_axis_deg is None
        assert shot.delivery_club_path_source is None

    def test_no_path_means_no_spin_axis(self, kiosk):
        shot = kiosk.swing(
            camera_swing(
                attack_angle_deg=-5.0,
                club_delivery_source="camera",
                club_delivery_confidence="high",
            )
        )

        assert shot.experimental_fused_club_path_deg is None
        assert shot.dynamic_loft_deg == pytest.approx(22.3)
        assert shot.face_angle_deg is None
        assert shot.spin_axis_deg is None

    def test_no_delivery_at_all_invents_nothing(self, kiosk):
        shot = kiosk.swing(camera_swing())

        assert shot.experimental_fused_status is None
        assert shot.face_angle_deg is None
        assert shot.spin_axis_deg is None
        assert kiosk.sim_shots[-1].provenance["club_path"] == "estimated"
        assert kiosk.sim_shots[-1].provenance["spin_axis"] == "estimated"

    @pytest.mark.parametrize("club", [ClubType.DRIVER, ClubType.WOOD_3, ClubType.HYBRID_5])
    def test_woods_and_hybrids_withhold_the_spin_axis(self, kiosk, club):
        shot = kiosk.swing(camera_swing(launch_angle_vertical=12.0, **club_delivery()), club=club)

        assert shot.face_angle_deg is not None
        assert shot.spin_axis_deg is None

    def test_woods_get_a_spin_axis_when_opted_in(self, kiosk, monkeypatch):
        monkeypatch.setattr(server_module, "wood_spin_axis_enabled", True)

        shot = kiosk.swing(
            camera_swing(launch_angle_vertical=12.0, **club_delivery()), club=ClubType.DRIVER
        )

        assert shot.spin_axis_deg is not None and shot.spin_axis_deg > 0

    def test_an_unaimed_camera_horizontal_gives_no_spin_axis(self, kiosk):
        shot = kiosk.swing(
            camera_swing(camera_launch=camera_fit(aim_status="unset"), **club_delivery())
        )

        assert shot.face_angle_deg == pytest.approx(2.2)
        assert shot.spin_axis_deg is None

    def test_an_estimated_vertical_gives_no_spin_axis(self, kiosk):
        shot = kiosk.swing(camera_swing(launch_angle_vertical=None, **club_delivery()))

        assert shot.launch_angle_vertical_source == "estimated"
        assert shot.spin_axis_deg is None

    def test_the_spin_axis_curves_the_flight(self, kiosk):
        shot = kiosk.swing(camera_swing(**club_delivery()))

        lateral_yards = [point[2] for point in shot.trajectory["points"]]
        assert lateral_yards[-1] > lateral_yards[len(lateral_yards) // 2] > 0


# --- club speed -----------------------------------------------------------------------


class TestClubSpeed:
    def test_a_measured_club_speed_is_used_and_labelled(self, kiosk):
        shot = kiosk.swing(camera_swing(club_speed_mph=85.0, club_speed_source="ops243"))

        assert shot.club_speed_mph == pytest.approx(85.0)
        assert shot.club_speed_source == "ops243"
        assert shot.smash_factor == pytest.approx(120.0 / 85.0)
        [entry] = kiosk.logged_shots()
        assert entry["club_speed_mph"] == pytest.approx(85.0)
        assert entry["club_speed_source"] == "ops243"
        resolved = kiosk.sim_shots[-1]
        assert resolved.club_speed_mph == pytest.approx(85.0)
        assert resolved.provenance["club_speed"] == "measured"

    def test_no_club_speed_is_invented(self, kiosk):
        shot = kiosk.swing(camera_swing())

        assert shot.club_speed_mph is None
        assert shot.smash_factor is None
        resolved = kiosk.sim_shots[-1]
        assert resolved.club_speed_mph is None
        assert resolved.provenance["club_speed"] == "estimated"
        assert kiosk.phone_shots[-1]["club_speed_mph"] is None

    def test_an_impossible_smash_hides_smash_but_keeps_the_reading(self, recorded):
        post(camera_swing(club_speed_mph=60.0, club_speed_source="ops243"))

        [shot] = recorded
        assert shot.club_speed_mph == pytest.approx(60.0)
        assert shot.smash_factor is None


# --- spin -------------------------------------------------------------------------------


class TestClubAnchoredSpin:
    @pytest.fixture(autouse=True)
    def _club_anchored(self, monkeypatch):
        monkeypatch.setattr(server_module, "calculated_spin_enabled", True)
        monkeypatch.setattr(server_module, "spin_model", server_module.SPIN_MODEL_CLUB_ANCHORED)

    def test_a_camera_vertical_reaches_the_spin_model_as_measured(self, kiosk):
        shot = kiosk.swing(camera_swing())

        measured = club_anchored_spin_rpm(ClubType.IRON_7, 120.0, 16.0)
        unmeasured = club_anchored_spin_rpm(ClubType.IRON_7, 120.0, None)
        assert measured != pytest.approx(unmeasured)
        assert shot.spin_rpm == pytest.approx(measured)
        assert shot.spin_source == "calculated"

    def test_the_table_estimate_stands_in_only_without_any_vertical(self, kiosk):
        shot = kiosk.swing({"ball_speed_mph": 120.0})

        assert shot.launch_angle_vertical_source == "estimated"
        assert shot.spin_rpm == pytest.approx(club_anchored_spin_rpm(ClubType.IRON_7, 120.0, None))

    def test_a_radar_vertical_still_counts_as_measured(self, kiosk):
        shot = kiosk.swing(
            {
                "ball_speed_mph": 120.0,
                "launch_angle_vertical": 18.0,
                "vertical_component_std_deg": 1.0,
            }
        )

        assert shot.launch_angle_vertical_source == "radar"
        assert shot.spin_rpm == pytest.approx(club_anchored_spin_rpm(ClubType.IRON_7, 120.0, 18.0))

    def test_a_camera_vertical_is_never_replaced_by_the_estimate(self, kiosk, monkeypatch):
        monkeypatch.setattr(server_module, "single_channel_max_spread_deg", 3.0)

        shot = kiosk.swing(
            camera_swing(vertical_single_channel=True, vertical_component_std_deg=6.0)
        )

        assert shot.launch_angle_vertical == pytest.approx(16.0)
        assert shot.launch_angle_vertical_source == "camera"


# --- everything that leaves the kiosk ---------------------------------------------------


class TestLabelsEverywhere:
    """No "radar" label on a camera angle: shot dict, phone stream/BLE, simulators, log."""

    @staticmethod
    def _full_camera_swing():
        return camera_swing(
            club_speed_mph=85.0,
            club_speed_source="ops243",
            **club_delivery(attack_angle_deg=-5.0),
        )

    def test_the_shot_dict_carries_the_camera_sources(self, kiosk):
        kiosk.swing(self._full_camera_swing())

        shot_data = kiosk.phone_shots[-1]
        assert shot_data["launch_angle_vertical"] == pytest.approx(16.0)
        assert shot_data["launch_angle_horizontal"] == pytest.approx(1.0)
        assert shot_data["launch_angle_vertical_source"] == "camera"
        assert shot_data["launch_angle_horizontal_source"] == "camera"
        assert shot_data["angle_source"] == "camera"
        assert shot_data["delivery_club_path_source"] == "camera"
        assert shot_data["delivery_attack_angle_source"] == "camera"
        assert shot_data["club_speed_mph"] == pytest.approx(85.0)
        # Club data measured, so Live lays it out like any full shot.
        assert "mode" not in shot_data

    def test_phones_get_the_camera_values_and_no_radar_label(self, kiosk):
        kiosk.swing(self._full_camera_swing())

        event = build_shot_event(kiosk.phone_shots[-1], final=True)
        assert event["launch_angle_vertical"] == pytest.approx(16.0)
        assert event["launch_angle_horizontal"] == pytest.approx(1.0)
        assert event["club_speed_mph"] == pytest.approx(85.0)
        assert event["spin_axis_deg"] is not None
        assert "radar" not in json.dumps(event)

    def test_simulators_get_the_camera_values_as_measured(self, kiosk):
        shot = kiosk.swing(self._full_camera_swing())

        resolved = kiosk.sim_shots[-1]
        assert resolved.vla == pytest.approx(16.0)
        assert resolved.hla == pytest.approx(1.0)
        assert resolved.club_path_deg == pytest.approx(-3.0)
        assert resolved.attack_angle_deg == pytest.approx(-5.0)
        assert resolved.club_speed_mph == pytest.approx(85.0)
        assert resolved.face_angle_deg == pytest.approx(shot.face_angle_deg)
        assert resolved.spin_axis_deg == pytest.approx(shot.spin_axis_deg)
        for field in (
            "vla",
            "hla",
            "club_path",
            "attack_angle",
            "club_speed",
            "face_angle",
            "dynamic_loft",
            "spin_axis",
        ):
            assert resolved.provenance[field] == "measured", field

    def test_the_session_log_carries_the_camera_sources(self, kiosk):
        kiosk.swing(self._full_camera_swing())

        [entry] = kiosk.logged_shots()
        assert entry["launch_angle_vertical_source"] == "camera"
        assert entry["launch_angle_horizontal_source"] == "camera"
        assert entry["angle_source"] == "camera"
        assert entry["delivery_club_path_source"] == "camera"
        assert entry["experimental_fused_status"] == RECORDER_CAMERA_DELIVERY_STATUS
        assert entry["iwr6843_horizontal_deg"] is None
        assert entry["radar_launch"] == radar_reading()

    def test_a_measured_swing_without_club_data_keeps_the_five_tile_row(self, kiosk):
        kiosk.swing(camera_swing())

        assert kiosk.phone_shots[-1]["mode"] == MEASURED_SHOT_MODE

    def test_a_low_tier_delivery_is_still_club_data_on_screen(self, kiosk):
        kiosk.swing(camera_swing(**club_delivery(club_delivery_confidence="low")))

        assert "mode" not in kiosk.phone_shots[-1]


# --- the kiosk's own camera fusion ------------------------------------------------------


class TestKioskFusionLeavesRecorderResultsAlone:
    @pytest.fixture
    def clip_matched(self, monkeypatch):
        """A kiosk that (unusually) matched its own clip to a measured swing."""
        clip = SimpleNamespace(
            valid=True, path=None, trigger_timestamp=1.0, metadata={}, error=None, sequence=1
        )
        monkeypatch.setattr(
            server_module,
            "camera_capture_runtime",
            SimpleNamespace(capture_for_shot=lambda *args, **kwargs: clip),
        )
        for hardware in ("iwr6843_runtime", "kld7_vertical", "kld7_horizontal"):
            monkeypatch.setattr(server_module, hardware, None)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        fused = []
        monkeypatch.setattr(
            server_module,
            "_fuse_camera_measurements",
            lambda shot, capture: fused.append(shot) or 0.0,
        )
        return fused

    @staticmethod
    def _measured_shot(**fields):
        return Shot(
            ball_speed_mph=120.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_7,
            mode=MEASURED_SHOT_MODE,
            **fields,
        )

    @pytest.mark.parametrize(
        "fields",
        [
            {"launch_angle_vertical": 16.0, "launch_angle_vertical_source": "camera"},
            {"launch_angle_horizontal": 1.0, "launch_angle_horizontal_source": "camera"},
            {
                "experimental_fused_club_path_deg": -3.0,
                "experimental_fused_status": RECORDER_CAMERA_DELIVERY_STATUS,
            },
        ],
    )
    def test_recorder_camera_results_are_not_fused_again(self, clip_matched, fields):
        server_module._enrich_shot_from_optional_hardware(self._measured_shot(**fields))

        assert clip_matched == []

    def test_a_radar_measured_swing_with_a_clip_is_still_fused(self, clip_matched):
        shot = self._measured_shot(launch_angle_vertical=16.0, launch_angle_vertical_source="radar")

        server_module._enrich_shot_from_optional_hardware(shot)

        assert clip_matched == [shot]


def test_horizontal_floor_is_below_the_spin_axis_gate():
    """The floor is what keeps an unaimed camera sideways out of the spin axis."""
    assert math.isclose(ANGLE_CONFIDENCE_FLOOR, 0.55)
    assert ANGLE_CONFIDENCE_FLOOR < SPIN_AXIS_MIN_HORIZONTAL_CONFIDENCE
