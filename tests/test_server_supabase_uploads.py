"""The server's Supabase upload triggers: after each logged shot, and at session end."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from supabase_fakes import make_config

from openflight import server as server_module
from openflight.cloud import config as cloud_config_module, trigger
from openflight.cloud.supabase import config as supabase_config_module
from openflight.cloud.supabase.config import save_supabase_config
from openflight.cloud.supabase.link import LinkStatus, SupabaseLink
from openflight.clubs import ClubType
from openflight.launch_monitor import Shot
from openflight.session_logger import SessionLogger


def _wait_for_shot_finalization_idle(timeout_s: float = 2.0) -> None:
    with server_module._shot_finalization_condition:
        idle = server_module._shot_finalization_condition.wait_for(
            lambda: (
                not server_module._shot_finalization_order
                and not server_module._shot_finalization_running
            ),
            timeout=timeout_s,
        )
    assert idle


def _save_supabase(**overrides):
    save_supabase_config(make_config(**overrides), supabase_config_module.SUPABASE_CONFIG_PATH)


class TestShotNotifiesSupabase:
    @pytest.fixture(autouse=True)
    def _isolate_finalization(self, monkeypatch):
        server_module._reset_shot_sequence()
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(server_module, "camera_capture_runtime", None)
        monkeypatch.setattr(server_module, "ball_speed_correction_enabled", False)
        monkeypatch.setattr(server_module, "calculated_spin_enabled", False)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "sim_connectors", [])
        monkeypatch.setattr(server_module.socketio, "emit", lambda *_args, **_kwargs: None)
        yield
        _wait_for_shot_finalization_idle()

    @staticmethod
    def _shot() -> Shot:
        return Shot(
            ball_speed_mph=104.2,
            club_speed_mph=83.7,
            timestamp=datetime(2026, 11, 14, 12, 0, 0),
            club=ClubType.IRON_7,
            launch_angle_vertical=19.1,
            mode="rolling-buffer",
        )

    def test_notifies_after_the_shot_is_on_disk(self, monkeypatch, tmp_path):
        session_log = SessionLogger(log_dir=tmp_path)
        session_log.start_session(mode="mock")
        monkeypatch.setattr(server_module, "get_session_logger", lambda: session_log)
        logged_when_notified = []

        def notify_shot():
            text = session_log.session_path.read_text()
            logged_when_notified.append(text.count('"type": "shot_detected"'))

        monkeypatch.setattr(
            server_module, "_supabase_link", SimpleNamespace(notify_shot=notify_shot)
        )
        try:
            server_module._finalize_shot_detected(self._shot(), emit_event="shot")
        finally:
            session_log.end_session()

        assert logged_when_notified == [1]

    def test_no_session_log_no_notify(self, monkeypatch):
        notified = []
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(
            server_module,
            "_supabase_link",
            SimpleNamespace(notify_shot=lambda: notified.append(1)),
        )
        server_module._finalize_shot_detected(self._shot(), emit_event="shot")
        assert notified == []

    def test_the_real_link_does_no_work_on_the_shot_thread(self, monkeypatch, tmp_path):
        session_log = SessionLogger(log_dir=tmp_path)
        session_log.start_session(mode="mock")
        monkeypatch.setattr(server_module, "get_session_logger", lambda: session_log)

        def fail(*_args):
            pytest.fail("the shot thread resolved, probed or pushed")

        link = SupabaseLink(fail, run_push=fail, client_factory=fail)
        monkeypatch.setattr(server_module, "_supabase_link", link)
        try:
            server_module._finalize_shot_detected(self._shot(), emit_event="shot")
        finally:
            session_log.end_session()


class TestResolveSupabaseLink:
    def _logger(self, monkeypatch, tmp_path, enabled=True):
        monkeypatch.setattr(
            server_module,
            "get_session_logger",
            lambda: SimpleNamespace(log_dir=tmp_path, enabled=enabled),
        )

    def test_active_config_and_the_session_log_dir(self, monkeypatch, tmp_path):
        _save_supabase()
        self._logger(monkeypatch, tmp_path)
        config, log_dir, why_not = server_module._resolve_supabase_link()
        assert config.supabase_url == make_config().supabase_url
        assert log_dir == tmp_path
        assert why_not == ""

    @pytest.mark.parametrize(
        "setup, logging_on, expected",
        [
            (None, True, "Supabase is not set up"),
            ("[]", True, "Supabase is not set up"),
            ({"enabled": False}, True, "Supabase uploads are off"),
            ({}, False, "session logging is off"),
        ],
    )
    def test_reasons_it_is_not_set_up(self, monkeypatch, tmp_path, setup, logging_on, expected):
        if isinstance(setup, dict):
            _save_supabase(**setup)
        elif setup is not None:
            supabase_config_module.SUPABASE_CONFIG_PATH.write_text(setup)
        self._logger(monkeypatch, tmp_path, enabled=logging_on)
        assert server_module._resolve_supabase_link() == (None, None, expected)


class TestCloudStatusApi:
    def _link(self, monkeypatch, status):
        monkeypatch.setattr(server_module, "_supabase_link", SimpleNamespace(status=lambda: status))

    def test_rest_endpoint(self, monkeypatch):
        self._link(monkeypatch, LinkStatus("offline", 12, "network error: no route"))
        response = server_module.app.test_client().get("/api/cloud/status")
        assert response.status_code == 200
        assert response.get_json() == {
            "state": "offline",
            "pending": 12,
            "message": "network error: no route",
            "reason": "",
        }

    def test_socket_request_and_change_events(self, monkeypatch):
        emitted = []
        monkeypatch.setattr(
            server_module.socketio, "emit", lambda event, data: emitted.append((event, data))
        )
        self._link(monkeypatch, LinkStatus("online", 0))

        server_module.handle_get_cloud_status()
        server_module._emit_cloud_status(LinkStatus("needs_attention", 3, "401", "unauthorized"))

        assert emitted == [
            ("cloud_status", {"state": "online", "pending": 0, "message": "", "reason": ""}),
            (
                "cloud_status",
                {
                    "state": "needs_attention",
                    "pending": 3,
                    "message": "401",
                    "reason": "unauthorized",
                },
            ),
        ]

    def test_shutdown_stops_the_link(self, monkeypatch):
        stopped = []
        monkeypatch.setattr(
            server_module, "_supabase_link", SimpleNamespace(stop=lambda: stopped.append(1))
        )
        monkeypatch.setattr(server_module, "shutdown_cleanup_started", False)
        for name in (
            "status_light",
            "kld7_vertical",
            "kld7_horizontal",
            "inclinometer_service",
            "iwr6843_runtime",
            "power_monitor",
            "camera_capture_runtime",
            "ble_publisher",
        ):
            monkeypatch.setattr(server_module, name, None)
        monkeypatch.setattr(server_module, "sim_connectors", [])
        monkeypatch.setattr(server_module, "stop_monitor", lambda: None)

        assert server_module._cleanup_hardware_for_shutdown() is True
        assert stopped == [1]


class TestSessionEndPush:
    @pytest.fixture(autouse=True)
    def _no_flightweb(self, monkeypatch):
        monkeypatch.setattr(cloud_config_module, "load_config", lambda *args, **kwargs: None)

    @pytest.fixture
    def spawned(self, monkeypatch):
        calls = []

        def fake_spawn(cli_args, config_path, log_dir, popen_fn):
            calls.append((cli_args, log_dir))
            return True

        monkeypatch.setattr(trigger, "_spawn_cli", fake_spawn)
        return calls

    def test_supabase_alone_spawns_a_push(self, tmp_path, spawned):
        _save_supabase()
        server_module._fire_cloud_push(SimpleNamespace(log_dir=tmp_path))
        assert spawned == [(["push"], tmp_path)]

    def test_nothing_set_up_spawns_nothing(self, tmp_path, spawned):
        server_module._fire_cloud_push(SimpleNamespace(log_dir=tmp_path))
        assert spawned == []

    def test_disabled_supabase_spawns_nothing(self, tmp_path, spawned):
        _save_supabase(enabled=False)
        server_module._fire_cloud_push(SimpleNamespace(log_dir=tmp_path))
        assert spawned == []

    def test_never_raises(self, monkeypatch, tmp_path):
        def boom(*args, **kwargs):
            raise RuntimeError("broken")

        monkeypatch.setattr(trigger, "fire_push_async", boom)
        server_module._fire_cloud_push(SimpleNamespace(log_dir=tmp_path))
