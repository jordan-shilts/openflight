"""Tests for the detached push triggers: session end, and Supabase pushes the server starts."""

from openflight.cloud import trigger
from openflight.cloud.config import CloudConfig
from openflight.cloud.supabase.config import SupabaseConfig


class TestFirePushAsync:
    def test_fires_when_active(self, tmp_path):
        calls = []
        config = CloudConfig(device_token="t", device_id="i", enabled=True)
        ok = trigger.fire_push_async(
            config, log_dir=tmp_path, popen_fn=lambda cmd, **k: calls.append(cmd)
        )
        assert ok is True
        assert len(calls) == 1
        assert "push" in calls[0]

    def test_skips_when_inactive(self, tmp_path):
        calls = []
        config = CloudConfig(enabled=False)
        ok = trigger.fire_push_async(
            config, log_dir=tmp_path, popen_fn=lambda cmd, **k: calls.append(cmd)
        )
        assert ok is False
        assert calls == []

    def test_swallows_spawn_errors(self, tmp_path):
        def boom(cmd, **k):
            raise OSError("no exec")

        config = CloudConfig(device_token="t", device_id="i", enabled=True)
        # Must never raise into the caller (server shot path).
        assert trigger.fire_push_async(config, log_dir=tmp_path, popen_fn=boom) is False

    def test_passes_config_and_log_dir(self, tmp_path):
        calls = []
        config = CloudConfig(device_token="t", device_id="i", enabled=True)
        cfg_path = tmp_path / "cloud.json"
        trigger.fire_push_async(
            config,
            log_dir=tmp_path,
            config_path=cfg_path,
            popen_fn=lambda cmd, **k: calls.append(cmd),
        )
        cmd = calls[0]
        assert str(cfg_path) in cmd
        assert str(tmp_path) in cmd


def _supabase_config(**overrides):
    values = dict(
        supabase_url="https://abcdefghijklmnopqrst.supabase.co",
        supabase_key="sb_publishable_x",
        supabase_device_token="t" * 64,
    )
    values.update(overrides)
    return SupabaseConfig(**values)


class TestFirePushAsyncWithSupabase:
    def test_fires_for_supabase_alone(self, tmp_path):
        calls = []
        ok = trigger.fire_push_async(
            CloudConfig(),
            log_dir=tmp_path,
            popen_fn=lambda cmd, **k: calls.append(cmd),
            supabase_config=_supabase_config(),
        )
        assert ok is True
        assert calls[0][-1] == "push"

    def test_disabled_supabase_does_not_count(self, tmp_path):
        calls = []
        ok = trigger.fire_push_async(
            CloudConfig(),
            log_dir=tmp_path,
            popen_fn=lambda cmd, **k: calls.append(cmd),
            supabase_config=_supabase_config(enabled=False),
        )
        assert ok is False
        assert calls == []


class TestStartSupabasePush:
    def test_starts_a_detached_background_supabase_push(self, tmp_path):
        calls = []
        cfg_path = tmp_path / "cloud.json"
        proc = trigger.start_supabase_push(
            _supabase_config(),
            log_dir=tmp_path / "sessions",
            config_path=cfg_path,
            popen_fn=lambda cmd, **k: calls.append((cmd, k)) or "process",
        )
        cmd, kwargs = calls[0]
        assert proc == "process"
        assert cmd[-3:] == ["supabase", "push", "--background"]
        assert cmd[cmd.index("--config") + 1] == str(cfg_path)
        assert cmd[cmd.index("--log-dir") + 1] == str(tmp_path / "sessions")
        assert kwargs["start_new_session"] is True

    def test_nothing_when_not_set_up(self, tmp_path):
        calls = []
        proc = trigger.start_supabase_push(
            _supabase_config(supabase_device_token=""),
            log_dir=tmp_path,
            popen_fn=lambda cmd, **k: calls.append(cmd),
        )
        assert proc is None
        assert calls == []

    def test_swallows_spawn_errors(self, tmp_path):
        def boom(cmd, **k):
            raise OSError("no exec")

        assert trigger.start_supabase_push(_supabase_config(), tmp_path, popen_fn=boom) is None
