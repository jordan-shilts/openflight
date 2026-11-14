"""Tests for ``openflight-cloud supabase`` setup, test, status and on/off."""

import hashlib
import re
import stat

import pytest
from supabase_fakes import (
    DEVICE_TOKEN,
    PROJECT_URL,
    PUBLISHABLE_KEY,
    FakeSupabase,
    SessionFile,
    error_response,
    make_config,
)

from openflight.cloud import cli
from openflight.cloud.client import CloudNetworkError
from openflight.cloud.supabase import commands, push as push_module
from openflight.cloud.supabase.client import SupabaseClient
from openflight.cloud.supabase.commands import Console, SetupOptions
from openflight.cloud.supabase.config import load_supabase_config, save_supabase_config

SQL_LINE = re.compile(
    r"^insert into public\.golf_devices \(name, token_sha256\) values "
    r"\('(?P<name>(?:[^']|'')*)', '(?P<hash>[0-9a-f]{64})'\) on conflict \(token_sha256\) do nothing;$"
)


def _sql_lines(text):
    return [line for line in text.splitlines() if line.startswith("insert into")]


def _fake_client(fake):
    def factory(config):
        return SupabaseClient(
            config.supabase_url, config.supabase_key, config.supabase_device_token, request_fn=fake
        )

    return factory


def _setup(tmp_path, options, answers=(), interactive=False, fake=None, existing=None):
    out = []
    replies = list(answers)

    def ask(prompt):
        out.append(prompt)
        if not replies:
            raise EOFError
        return replies.pop(0)

    ok = commands.cmd_setup(
        existing,
        tmp_path / "supabase.json",
        tmp_path / "sessions",
        options,
        Console(out=out.append, input_fn=ask, interactive=interactive),
        client_factory=_fake_client(fake or FakeSupabase()),
    )
    return ok, "\n".join(out)


class TestSetupCli:
    def test_prints_one_sql_line_with_the_hash_of_the_saved_token(self, tmp_path, capsys):
        rc = cli.main(
            [
                "supabase",
                "setup",
                "--config",
                str(tmp_path / "cloud.json"),
                "--log-dir",
                str(tmp_path / "sessions"),
                "--url",
                PROJECT_URL + "/",
                "--key",
                PUBLISHABLE_KEY,
                "--name",
                "Garage Pi",
                "--no-test",
            ]
        )
        output = capsys.readouterr().out
        saved = load_supabase_config(tmp_path / "supabase.json")

        assert rc == 0
        sql = _sql_lines(output)
        assert len(sql) == 1
        match = SQL_LINE.match(sql[0])
        assert match
        assert match["name"] == "Garage Pi"
        assert match["hash"] == hashlib.sha256(saved.supabase_device_token.encode()).hexdigest()
        assert saved.supabase_device_token not in output
        assert saved.supabase_url == PROJECT_URL
        assert saved.supabase_key == PUBLISHABLE_KEY
        assert re.fullmatch(r"[0-9a-f]{64}", saved.supabase_device_token)
        assert saved.enabled is True
        assert stat.S_IMODE((tmp_path / "supabase.json").stat().st_mode) == 0o600
        assert "openflight-cloud supabase test" in output

    def test_does_not_touch_the_flightweb_config(self, tmp_path):
        cli.main(
            [
                "supabase",
                "setup",
                "--config",
                str(tmp_path / "cloud.json"),
                "--url",
                PROJECT_URL,
                "--key",
                PUBLISHABLE_KEY,
                "--no-test",
            ]
        )
        assert not (tmp_path / "cloud.json").exists()

    def test_bad_flag_value_saves_nothing_and_fails(self, tmp_path, capsys):
        rc = cli.main(
            [
                "supabase",
                "setup",
                "--config",
                str(tmp_path / "cloud.json"),
                "--url",
                PROJECT_URL,
                "--key",
                "sb_secret_abc",
                "--no-test",
            ]
        )
        assert rc == 1
        assert "secret key" in capsys.readouterr().out
        assert not (tmp_path / "supabase.json").exists()


class TestSetup:
    def test_prompts_until_answers_are_valid(self, tmp_path):
        answers = ["http://nope", PROJECT_URL, "sb_secret_x", PUBLISHABLE_KEY]
        ok, output = _setup(tmp_path, SetupOptions(test=False), answers)
        assert ok
        assert "Expected the project URL" in output
        assert "secret key" in output
        assert load_supabase_config(tmp_path / "supabase.json").supabase_key == PUBLISHABLE_KEY

    def test_end_of_input_saves_nothing(self, tmp_path):
        ok, output = _setup(tmp_path, SetupOptions(test=False), answers=[PROJECT_URL])
        assert not ok
        assert "nothing saved" in output
        assert not (tmp_path / "supabase.json").exists()

    def test_default_name_is_the_hostname(self, tmp_path, monkeypatch):
        monkeypatch.setattr(commands.socket, "gethostname", lambda: "garage-pi")
        _, output = _setup(tmp_path, SetupOptions(PROJECT_URL, PUBLISHABLE_KEY, test=False))
        assert SQL_LINE.match(_sql_lines(output)[0])["name"] == "garage-pi"

    def test_name_is_quoted_and_kept_on_one_line(self, tmp_path):
        options = SetupOptions(PROJECT_URL, PUBLISHABLE_KEY, name="Jo's\nPi\t2", test=False)
        _, output = _setup(tmp_path, options)
        sql = _sql_lines(output)
        assert len(sql) == 1
        assert "values ('Jo''s Pi 2'," in sql[0]

    def test_rerun_keeps_the_token_so_the_pi_stays_registered(self, tmp_path):
        existing = make_config()
        options = SetupOptions(PROJECT_URL, "sb_publishable_NewKey", test=False)
        _, output = _setup(tmp_path, options, existing=existing)
        saved = load_supabase_config(tmp_path / "supabase.json")
        assert saved.supabase_device_token == DEVICE_TOKEN
        assert saved.supabase_key == "sb_publishable_NewKey"
        assert saved.supabase_device_name == "garage pi"
        assert "Kept this Pi's device token" in output
        assert hashlib.sha256(DEVICE_TOKEN.encode()).hexdigest() in output

    def test_new_token_replaces_it(self, tmp_path):
        options = SetupOptions(PROJECT_URL, PUBLISHABLE_KEY, new_token=True, test=False)
        _setup(tmp_path, options, existing=make_config())
        assert (
            load_supabase_config(tmp_path / "supabase.json").supabase_device_token != DEVICE_TOKEN
        )

    def test_interactive_setup_offers_the_test(self, tmp_path):
        fake = FakeSupabase()
        ok, output = _setup(
            tmp_path,
            SetupOptions(PROJECT_URL, PUBLISHABLE_KEY),
            answers=[""],
            interactive=True,
            fake=fake,
        )
        assert ok
        assert "press Enter to test" in output
        assert "registered as device" in output
        assert [r.path for r in fake.requests] == ["/rest/v1/rpc/golf_request_device"]

    def test_skipping_the_offered_test_explains_how_to_run_it_later(self, tmp_path):
        fake = FakeSupabase()
        ok, output = _setup(
            tmp_path,
            SetupOptions(PROJECT_URL, PUBLISHABLE_KEY),
            ["skip"],
            interactive=True,
            fake=fake,
        )
        assert ok
        assert fake.requests == []
        assert "openflight-cloud supabase test" in output

    def test_no_terminal_means_no_test(self, tmp_path):
        fake = FakeSupabase()
        _setup(tmp_path, SetupOptions(PROJECT_URL, PUBLISHABLE_KEY), interactive=False, fake=fake)
        assert fake.requests == []

    def test_requested_test_reports_an_unregistered_pi(self, tmp_path):
        ok, output = _setup(
            tmp_path,
            SetupOptions(PROJECT_URL, PUBLISHABLE_KEY, test=True),
            fake=FakeSupabase(device_id=None),
        )
        assert not ok
        assert "not registered yet. Run the line above" in output
        assert len(_sql_lines(output)) == 1
        assert (tmp_path / "supabase.json").exists()


class TestTest:
    def _run(self, tmp_path, fake, config=None):
        out = []
        config = config or make_config()
        ok = commands.cmd_test(config, tmp_path, _fake_client(fake)(config), out=out.append)
        return ok, "\n".join(out)

    def test_registered(self, tmp_path):
        ok, output = self._run(tmp_path, FakeSupabase(device_id="dev-1"))
        assert ok
        assert "registered as device dev-1; uploads are on" in output

    def test_registered_but_off(self, tmp_path):
        ok, output = self._run(tmp_path, FakeSupabase(), config=make_config(enabled=False))
        assert ok
        assert "uploads are off" in output

    def test_unregistered_prints_the_line_again(self, tmp_path):
        ok, output = self._run(tmp_path, FakeSupabase(device_id=None))
        assert not ok
        sql = _sql_lines(output)
        assert len(sql) == 1
        assert SQL_LINE.match(sql[0])["hash"] == hashlib.sha256(DEVICE_TOKEN.encode()).hexdigest()

    def test_bad_key(self, tmp_path):
        fake = FakeSupabase(script=[error_response(401, "", "Invalid API key")])
        ok, output = self._run(tmp_path, fake)
        assert not ok
        assert "refused the key (401: Invalid API key)" in output

    def test_missing_schema(self, tmp_path):
        fake = FakeSupabase(script=[error_response(404, "PGRST202", "Could not find the function")])
        ok, output = self._run(tmp_path, fake)
        assert not ok
        assert "scripts/supabase/schema.sql" in output

    def test_offline(self, tmp_path):
        ok, output = self._run(tmp_path, FakeSupabase(script=[CloudNetworkError("no route")]))
        assert not ok
        assert f"Could not reach {PROJECT_URL}" in output

    def test_registered_lifts_a_pause(self, tmp_path):
        SessionFile(tmp_path / "session_a.jsonl").start()
        out = []
        client = _fake_client(FakeSupabase(script=[error_response(401)]))(make_config())
        push_module.push(make_config(), tmp_path, client, out=out.append)
        assert "paused" in push_module.load_state(tmp_path)

        self._run(tmp_path, FakeSupabase())

        assert "paused" not in push_module.load_state(tmp_path)

    def test_not_set_up(self, tmp_path):
        out = []
        assert not commands.cmd_test(make_config(supabase_key=""), tmp_path, None, out=out.append)
        assert "not set up" in out[0]


class TestStatus:
    def test_not_set_up(self, tmp_path):
        out = []
        assert commands.cmd_status(None, tmp_path, out=out.append) == {"configured": False}
        assert "supabase setup" in out[0]

    def test_reports_progress_without_the_token(self, tmp_path):
        session = SessionFile(tmp_path / "session_20261114_090000_range.jsonl")
        session.start()
        session.shot(1)
        waiting = SessionFile(tmp_path / "session_20261114_100000_range.jsonl")
        waiting.start(session_uuid="11111111-2222-4333-8444-555555555555")
        config = make_config()
        client = _fake_client(FakeSupabase())(config)
        push_module.push(config, tmp_path, client, out=lambda _line: None)
        waiting.shot(1)
        out = []

        result = commands.cmd_status(config, tmp_path, out=out.append)

        text = "\n".join(out)
        assert DEVICE_TOKEN not in text
        assert "token sha256 " + hashlib.sha256(DEVICE_TOKEN.encode()).hexdigest()[:12] in text
        assert "2 total | 1 up to date | 1 with new data | 0 refused" in text
        assert "Shots sent: 1" in text
        assert "Waiting:    1 shot(s) not uploaded yet" in text
        assert result["pending"] == 1
        assert "Last check: online at " in text
        assert result["counts"]["waiting"] == 1
        assert "Last sent:" in text

    def test_reports_a_pause(self, tmp_path):
        SessionFile(tmp_path / "session_a.jsonl").start()
        config = make_config()
        client = _fake_client(FakeSupabase(script=[error_response(401, "", "Invalid API key")]))(
            config
        )
        push_module.push(config, tmp_path, client, out=lambda _line: None)
        out = []

        result = commands.cmd_status(config, tmp_path, out=out.append)

        assert result["paused"]
        assert any(line.startswith("Paused:     401: Invalid API key.") for line in out)

    def test_reports_refused_sessions(self, tmp_path):
        session = SessionFile(tmp_path / "session_a.jsonl")
        session.start()
        session.shot(1)
        config = make_config()
        refused = error_response(400, "22P02", "invalid input syntax for type real")
        client = _fake_client(FakeSupabase(script=[refused]))(config)
        push_module.push(config, tmp_path, client, out=lambda _line: None)
        out = []

        result = commands.cmd_status(config, tmp_path, out=out.append)

        assert result["rejected"] == ["session_a.jsonl"]
        assert "Refused:    session_a.jsonl: 400 22P02: invalid input syntax for type real" in out
        assert any("0 up to date | 0 with new data | 1 refused" in line for line in out)

    def test_legacy_key_is_described_not_shown(self, tmp_path):
        key = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.c2ln"
        out = []
        commands.cmd_status(make_config(supabase_key=key), tmp_path, out=out.append)
        assert key not in "\n".join(out)
        assert any("legacy anon JWT" in line for line in out)


class TestOnOff:
    def test_off_then_on(self, tmp_path):
        path = tmp_path / "supabase.json"
        save_supabase_config(make_config(), path)
        out = []
        assert commands.cmd_set_enabled(load_supabase_config(path), path, False, out=out.append)
        assert load_supabase_config(path).enabled is False
        assert commands.cmd_set_enabled(load_supabase_config(path), path, True, out=out.append)
        assert load_supabase_config(path).enabled is True
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_needs_setup_first(self, tmp_path):
        out = []
        assert not commands.cmd_set_enabled(None, tmp_path / "supabase.json", True, out=out.append)
        assert not (tmp_path / "supabase.json").exists()

    @pytest.mark.parametrize("state", ["on", "off"])
    def test_cli(self, tmp_path, state):
        save_supabase_config(make_config(), tmp_path / "supabase.json")
        rc = cli.main(["supabase", state, "--config", str(tmp_path / "cloud.json")])
        assert rc == 0
        assert load_supabase_config(tmp_path / "supabase.json").enabled is (state == "on")
