"""Tests for the openflight-cloud CLI argument wiring."""

import json
import sys

import pytest
from supabase_fakes import FakeSupabase, SessionFile, make_config

from openflight.cloud import cli, trigger
from openflight.cloud.config import CloudConfig, save_config
from openflight.cloud.supabase.client import SupabaseClient
from openflight.cloud.supabase.config import save_supabase_config
from openflight.cloud.supabase.push import PushSummary


class TestArgParsing:
    def test_requires_subcommand(self, capsys):
        rc = cli.main([])
        assert rc != 0

    def test_unknown_subcommand_errors(self):
        with pytest.raises(SystemExit):
            cli.main(["frobnicate"])


class TestDispatch:
    def test_status_dispatches(self, monkeypatch, tmp_path):
        called = {}

        def fake_status(config, log_dir, client=None, out=print):
            called["log_dir"] = log_dir
            out("status-ran")
            return {}

        monkeypatch.setattr(cli.commands, "cmd_status", fake_status)
        rc = cli.main(["status", "--log-dir", str(tmp_path), "--config", str(tmp_path / "c.json")])
        assert rc == 0
        assert called["log_dir"] == tmp_path

    def test_push_passes_dry_run_flag(self, monkeypatch, tmp_path):
        captured = {}

        def fake_push(config, log_dir, client, dry_run=False, retry=False, session=None, out=print):
            captured["dry_run"] = dry_run
            return {"needs_relink": False}

        monkeypatch.setattr(cli.commands, "cmd_push", fake_push)
        cli.main(
            ["push", "--dry-run", "--log-dir", str(tmp_path), "--config", str(tmp_path / "c.json")]
        )
        assert captured["dry_run"] is True

    def test_push_retry_all(self, monkeypatch, tmp_path):
        captured = {}

        def fake_push(config, log_dir, client, dry_run=False, retry=False, session=None, out=print):
            captured.update(retry=retry, session=session)
            return {"needs_relink": False}

        monkeypatch.setattr(cli.commands, "cmd_push", fake_push)
        cli.main(
            ["push", "--retry", "--log-dir", str(tmp_path), "--config", str(tmp_path / "c.json")]
        )
        assert captured == {"retry": True, "session": None}

    def test_push_retry_named_session(self, monkeypatch, tmp_path):
        captured = {}

        def fake_push(config, log_dir, client, dry_run=False, retry=False, session=None, out=print):
            captured.update(retry=retry, session=session)
            return {"needs_relink": False}

        monkeypatch.setattr(cli.commands, "cmd_push", fake_push)
        cli.main(
            [
                "push",
                "--retry",
                "session_20260527",
                "--log-dir",
                str(tmp_path),
                "--config",
                str(tmp_path / "c.json"),
            ]
        )
        assert captured == {"retry": True, "session": "session_20260527"}

    def test_push_no_retry_by_default(self, monkeypatch, tmp_path):
        captured = {}

        def fake_push(config, log_dir, client, dry_run=False, retry=False, session=None, out=print):
            captured.update(retry=retry, session=session)
            return {"needs_relink": False}

        monkeypatch.setattr(cli.commands, "cmd_push", fake_push)
        cli.main(["push", "--log-dir", str(tmp_path), "--config", str(tmp_path / "c.json")])
        assert captured == {"retry": False, "session": None}

    def test_push_returns_nonzero_when_relink_needed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli.commands, "cmd_push", lambda *a, **k: {"needs_relink": True})
        rc = cli.main(["push", "--log-dir", str(tmp_path), "--config", str(tmp_path / "c.json")])
        assert rc != 0

    def test_link_dispatches(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli.commands, "cmd_link", lambda *a, **k: True)
        rc = cli.main(["link", "--config", str(tmp_path / "c.json")])
        assert rc == 0

    def test_link_returns_nonzero_on_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli.commands, "cmd_link", lambda *a, **k: False)
        rc = cli.main(["link", "--config", str(tmp_path / "c.json")])
        assert rc != 0


class TestSharedOptions:
    """--config/--log-dir must work before or after the subcommand.

    fire_push_async (cloud/trigger.py) runs ``--config X --log-dir Y push``.
    """

    def _run_push(self, monkeypatch, argv):
        captured = {}

        def fake_load_config(path):
            captured["config"] = path

        def fake_push(config, log_dir, client, dry_run=False, retry=False, session=None, out=print):
            captured["log_dir"] = log_dir
            return {"needs_relink": False}

        monkeypatch.setattr(cli, "load_config", fake_load_config)
        monkeypatch.setattr(cli.commands, "cmd_push", fake_push)
        assert cli.main(argv) == 0
        return captured

    @pytest.mark.parametrize("before_subcommand", [True, False])
    def test_options_reach_push(self, monkeypatch, tmp_path, before_subcommand):
        opts = ["--config", str(tmp_path / "c.json"), "--log-dir", str(tmp_path)]
        argv = opts + ["push"] if before_subcommand else ["push"] + opts
        captured = self._run_push(monkeypatch, argv)
        assert captured == {"config": tmp_path / "c.json", "log_dir": tmp_path}

    def test_session_end_push_command(self, monkeypatch, tmp_path):
        calls = []
        trigger.fire_push_async(
            CloudConfig(device_token="t", device_id="i", enabled=True),
            log_dir=tmp_path,
            config_path=tmp_path / "c.json",
            popen_fn=lambda cmd, **k: calls.append(cmd),
        )
        assert calls[0][:3] == [sys.executable, "-m", "openflight.cloud.cli"]
        argv = calls[0][3:]
        captured = self._run_push(monkeypatch, argv)
        assert captured == {"config": tmp_path / "c.json", "log_dir": tmp_path}

    def test_defaults_without_options(self, monkeypatch):
        captured = self._run_push(monkeypatch, ["push"])
        assert captured == {"config": cli.CONFIG_PATH, "log_dir": cli.DEFAULT_LOG_DIR}


def _save_supabase(tmp_path, **overrides):
    config = make_config(**overrides)
    save_supabase_config(config, tmp_path / "supabase.json")
    return config


class TestPushRunsSupabase:
    """``push`` (the timer and session-end command) covers Supabase when it is set up."""

    def _patch(self, monkeypatch, fake=None, supabase_result=None):
        calls = {"flightweb": 0, "supabase": []}

        def fake_cmd_push(
            config, log_dir, client, dry_run=False, retry=False, session=None, out=print
        ):
            calls["flightweb"] += 1
            return {"needs_relink": False}

        monkeypatch.setattr(cli.commands, "cmd_push", fake_cmd_push)
        if supabase_result is not None:

            def fake_supabase_push(config, log_dir, client, **kwargs):
                calls["supabase"].append(kwargs)
                return supabase_result

            monkeypatch.setattr(cli.supabase_push, "push", fake_supabase_push)
        if fake is not None:
            monkeypatch.setattr(
                cli.supabase_commands,
                "make_client",
                lambda config: SupabaseClient(
                    config.supabase_url,
                    config.supabase_key,
                    config.supabase_device_token,
                    request_fn=fake,
                ),
            )
        return calls

    def _argv(self, tmp_path, *extra):
        return [
            "push",
            "--config",
            str(tmp_path / "cloud.json"),
            "--log-dir",
            str(tmp_path),
            *extra,
        ]

    def test_supabase_alone_skips_flightweb(self, monkeypatch, tmp_path):
        _save_supabase(tmp_path)
        calls = self._patch(monkeypatch, supabase_result=PushSummary())
        assert cli.main(self._argv(tmp_path)) == 0
        assert calls["flightweb"] == 0
        assert calls["supabase"] == [{"dry_run": False, "respect_pause": True}]

    def test_both_run_when_both_are_set_up(self, monkeypatch, tmp_path):
        save_config(CloudConfig(device_token="t", device_id="i"), tmp_path / "cloud.json")
        _save_supabase(tmp_path)
        calls = self._patch(monkeypatch, supabase_result=PushSummary())
        cli.main(self._argv(tmp_path))
        assert calls["flightweb"] == 1
        assert len(calls["supabase"]) == 1

    def test_neither_set_up_runs_flightweb_for_its_hint(self, monkeypatch, tmp_path):
        calls = self._patch(monkeypatch, supabase_result=PushSummary())
        cli.main(self._argv(tmp_path))
        assert calls["flightweb"] == 1
        assert calls["supabase"] == []

    def test_disabled_supabase_is_skipped(self, monkeypatch, tmp_path):
        _save_supabase(tmp_path, enabled=False)
        calls = self._patch(monkeypatch, supabase_result=PushSummary())
        cli.main(self._argv(tmp_path))
        assert calls["supabase"] == []

    def test_supabase_needing_the_owner_fails_the_run(self, monkeypatch, tmp_path):
        _save_supabase(tmp_path)
        self._patch(monkeypatch, supabase_result=PushSummary(stopped="unauthorized"))
        assert cli.main(self._argv(tmp_path)) == 1

    def test_dry_run_reaches_supabase(self, monkeypatch, tmp_path):
        _save_supabase(tmp_path)
        calls = self._patch(monkeypatch, supabase_result=PushSummary())
        cli.main(self._argv(tmp_path, "--dry-run"))
        assert calls["supabase"][0]["dry_run"] is True

    def test_unreadable_supabase_config_does_not_stop_flightweb(
        self, monkeypatch, tmp_path, capsys
    ):
        save_config(CloudConfig(device_token="t", device_id="i"), tmp_path / "cloud.json")
        (tmp_path / "supabase.json").write_text("{broken")
        calls = self._patch(monkeypatch, supabase_result=PushSummary())
        assert cli.main(self._argv(tmp_path)) == 0
        assert calls["flightweb"] == 1
        assert "could not read" in capsys.readouterr().out

    def test_flightweb_raw_mode_never_reaches_supabase(self, monkeypatch, tmp_path):
        save_config(
            CloudConfig(device_token="t", device_id="i", upload_raw=True), tmp_path / "cloud.json"
        )
        _save_supabase(tmp_path)
        session = SessionFile(tmp_path / "session_20261114_090000_range.jsonl")
        session.start()
        session.raw_capture()
        session.shot(1)
        fake = FakeSupabase()
        self._patch(monkeypatch, fake=fake)

        cli.main(self._argv(tmp_path))

        sent = json.dumps([r.body for r in fake.requests])
        assert "rolling_buffer_capture" not in sent
        assert "i_samples" not in sent
        assert len(fake.shots) == 1


class TestSupabaseSubcommands:
    def _argv(self, tmp_path, *command):
        return [
            "supabase",
            *command,
            "--config",
            str(tmp_path / "cloud.json"),
            "--log-dir",
            str(tmp_path),
        ]

    @pytest.mark.parametrize(
        "extra, expected",
        [
            ((), {"dry_run": False, "resend": False, "respect_pause": False}),
            (("--background",), {"dry_run": False, "resend": False, "respect_pause": True}),
            (("--resend",), {"dry_run": False, "resend": True, "respect_pause": False}),
            (("--dry-run",), {"dry_run": True, "resend": False, "respect_pause": False}),
        ],
    )
    def test_push_flags(self, monkeypatch, tmp_path, extra, expected):
        _save_supabase(tmp_path)
        seen = []
        monkeypatch.setattr(
            cli.supabase_push,
            "push",
            lambda config, log_dir, client, **kwargs: seen.append(kwargs) or PushSummary(),
        )
        assert cli.main(self._argv(tmp_path, "push", *extra)) == 0
        assert seen == [expected]

    def test_push_is_the_command_the_server_spawns(self, monkeypatch, tmp_path):
        calls = []
        trigger.start_supabase_push(
            make_config(),
            log_dir=tmp_path,
            config_path=tmp_path / "cloud.json",
            popen_fn=lambda cmd, **k: calls.append(cmd),
        )
        _save_supabase(tmp_path)
        seen = []
        monkeypatch.setattr(
            cli.supabase_push,
            "push",
            lambda config, log_dir, client, **kwargs: (
                seen.append((log_dir, kwargs)) or PushSummary()
            ),
        )
        assert cli.main(calls[0][3:]) == 0
        assert seen == [(tmp_path, {"dry_run": False, "resend": False, "respect_pause": True})]

    def test_push_without_setup_says_so(self, tmp_path, capsys):
        assert cli.main(self._argv(tmp_path, "push")) == 0
        assert "not set up" in capsys.readouterr().out

    def test_status_without_setup(self, tmp_path, capsys):
        assert cli.main(self._argv(tmp_path, "status")) == 0
        assert "not set up" in capsys.readouterr().out

    def test_test_dispatches(self, monkeypatch, tmp_path):
        _save_supabase(tmp_path)
        monkeypatch.setattr(cli.supabase_commands, "cmd_test", lambda *a, **k: True)
        assert cli.main(self._argv(tmp_path, "test")) == 0
        monkeypatch.setattr(cli.supabase_commands, "cmd_test", lambda *a, **k: False)
        assert cli.main(self._argv(tmp_path, "test")) == 1

    def test_bare_supabase_prints_usage(self, tmp_path, capsys):
        assert cli.main(["supabase", "--config", str(tmp_path / "cloud.json")]) == 1
        assert "usage" in capsys.readouterr().out
