"""The setup endpoints, and the radar path following the active setup."""

from types import SimpleNamespace

import numpy as np
import pytest

from openflight import server as server_module
from openflight.iwr6843 import Calibration
from openflight.rig_setup import COURSE, SIMULATOR, RigSetupStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    rig = RigSetupStore(tmp_path / "rig.json")
    monkeypatch.setattr(server_module, "rig_setup_store", rig)
    monkeypatch.setattr(server_module, "iwr6843_runtime", None)
    return rig


@pytest.fixture
def emitted(monkeypatch):
    events = []
    monkeypatch.setattr(
        server_module.socketio, "emit", lambda event, payload=None: events.append((event, payload))
    )
    return events


@pytest.fixture
def client():
    return server_module.app.test_client()


def observe(client, payload, remote_addr="127.0.0.1"):
    return client.post(
        "/api/rig-setup/observation",
        json=payload,
        environ_base={"REMOTE_ADDR": remote_addr},
    )


def radar_runtime():
    return SimpleNamespace(
        calibration=Calibration(
            elem_correction=np.ones(8, dtype=complex),
            tilt_rad=0.1,
            range_bias_m=0.0,
            tee_range_m=1.575,
            tee_ball_height_m=0.04,
        ),
        net_range_m=4.6,
    )


def test_reading_the_setups_returns_what_the_screen_shows(store, client):
    body = client.get("/api/rig-setup").get_json()

    assert body["active"] == SIMULATOR
    assert set(body["setups"]) == {SIMULATOR, COURSE}
    assert body["effective"]["distance_source"] == "none"


def test_a_change_is_applied_and_every_screen_hears_about_it(store, client, emitted):
    response = client.post("/api/rig-setup", json={"ball_distance_m": 1.4})

    assert response.status_code == 200
    assert response.get_json()["effective"]["ball_distance_m"] == pytest.approx(1.4)
    assert [event for event, _payload in emitted] == ["rig_setup"]
    assert emitted[0][1]["setups"][SIMULATOR]["ball_distance_m"] == pytest.approx(1.4)


def test_a_bad_change_is_refused_and_nobody_is_told(store, client, emitted):
    response = client.post("/api/rig-setup", json={"ball_distance_m": 99})

    assert response.status_code == 400
    assert "ball_distance_m must be between" in response.get_json()["error"]
    assert emitted == []


def test_a_body_that_is_not_json_is_refused(store, client, emitted):
    response = client.post("/api/rig-setup", data="nope", content_type="application/json")

    assert response.status_code == 400
    assert emitted == []


def test_an_observation_returns_this_swings_geometry(store, client, emitted):
    store.update({"ball_distance_m": 1.4})

    response = observe(client, {"diameter_px": 15.0, "pitch_deg": -3.0})

    assert response.status_code == 200
    body = response.get_json()
    assert body["ball_distance_m"] == pytest.approx(1.4)
    assert body["distance_source"] == "manual"
    assert body["tee_range_m"] > 1.4
    assert body["net_range_m"] == pytest.approx(4.6)
    assert body["tilt_offset_deg"] == 0.0
    assert [event for event, _payload in emitted] == ["rig_setup"]


def test_an_observation_with_no_readings_still_returns_geometry(store, client, emitted):
    response = observe(client, {})

    assert response.status_code == 200
    assert response.get_json()["tee_range_m"] is None


@pytest.mark.parametrize("remote", ["10.0.0.42", "192.168.1.20"])
def test_other_devices_cannot_teach_the_lens(store, client, emitted, remote):
    store.update({"ball_distance_m": 1.4})

    assert observe(client, {"diameter_px": 15.0}, remote_addr=remote).status_code == 403
    assert store.snapshot()["scale_samples_needed"] == 3
    assert emitted == []


def test_an_observation_that_is_not_an_object_is_refused(store, client):
    response = client.post(
        "/api/rig-setup/observation", data="[1]", content_type="application/json"
    )

    assert response.status_code == 400


def test_the_live_radar_path_follows_the_active_setup(store, client, emitted, monkeypatch):
    runtime = radar_runtime()
    monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)
    monkeypatch.setattr(server_module, "ball_speed_correction_distance_ft", 5.5)
    monkeypatch.setattr(server_module, "ball_speed_correction_ball_above_radar_ft", -0.3)

    client.post(
        "/api/rig-setup",
        json={"ball_distance_m": 2.0, "radar_height_m": 0.25, "net_distance_m": 3.5},
    )

    expected_tee = float(np.hypot(2.0, 0.25 - 0.04))
    assert runtime.calibration.tee_range_m == pytest.approx(expected_tee)
    assert runtime.calibration.radar_height_m == pytest.approx(0.25)
    assert runtime.calibration.tee_ball_height_m == pytest.approx(0.04)
    assert runtime.calibration.tilt_rad == pytest.approx(0.1)  # tilt stays with the sensor path
    assert runtime.net_range_m == pytest.approx(3.5)
    assert server_module.ball_speed_correction_distance_ft == pytest.approx(expected_tee * 3.28084)
    assert server_module.ball_speed_correction_ball_above_radar_ft == pytest.approx(
        (0.04 - 0.25) * 3.28084
    )


def test_no_net_reaches_the_live_radar_path(store, client, emitted, monkeypatch):
    runtime = radar_runtime()
    monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)

    client.post("/api/rig-setup", json={"ball_distance_m": 2.0, "net_enabled": False})

    assert runtime.net_range_m is None


def test_a_rig_never_set_up_keeps_its_command_line_geometry(store, client, emitted, monkeypatch):
    runtime = radar_runtime()
    monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)

    client.post("/api/rig-setup", json={"net_enabled": False})

    assert runtime.calibration.tee_range_m == pytest.approx(1.575)
    assert runtime.net_range_m == pytest.approx(4.6)


def saved_clip(path, diameter=15.0):
    yy, xx = np.mgrid[0:400, 0:640]
    ball = ((xx - 416) ** 2 + (yy - 242) ** 2) <= (diameter / 2.0) ** 2
    before = np.full((400, 640), 30, dtype=np.uint8)
    before[ball] = 170
    after = np.full((400, 640), 30, dtype=np.uint8)
    frames = np.stack([before if index <= 35 else after for index in range(60)])
    np.savez(path / "frames.npz", frames=frames, pre_trigger_count=36)


def test_a_shots_clip_teaches_the_lens_in_the_simulator(store, emitted, tmp_path):
    store.update({"ball_distance_m": 1.4})
    saved_clip(tmp_path)
    capture = SimpleNamespace(valid=True, path=str(tmp_path))

    for _ in range(3):
        server_module._observe_camera_ball(capture)

    assert store.snapshot()["camera_ready"] is True
    assert emitted[-1][0] == "rig_setup"


def test_a_shots_clip_sets_the_course_distance(store, emitted, tmp_path):
    store.update({"ball_distance_m": 1.4})
    saved_clip(tmp_path)
    capture = SimpleNamespace(valid=True, path=str(tmp_path))
    for _ in range(3):
        server_module._observe_camera_ball(capture)
    store.update({"active": COURSE})

    server_module._observe_camera_ball(capture)

    assert store.geometry().distance_source == "camera"
    assert store.geometry().ball_distance_m == pytest.approx(1.4, abs=0.03)


@pytest.mark.parametrize(
    "capture",
    [
        None,
        SimpleNamespace(valid=False, path="/nope"),
        SimpleNamespace(valid=True, path=None),
        SimpleNamespace(valid=True, path="/nope"),
    ],
)
def test_no_usable_clip_changes_nothing(store, emitted, capture):
    server_module._observe_camera_ball(capture)

    assert emitted == []
    assert store.geometry().distance_source == "none"
