"""Tests for the incremental Supabase push: offsets, privacy, failures, locking."""

import fcntl
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from supabase_fakes import (
    PROJECT_URL,
    SESSION_UUID,
    FakeSupabase,
    SessionFile,
    error_response,
    make_config,
    shot_entry,
)

from openflight.cloud import filtering
from openflight.cloud.client import CloudNetworkError
from openflight.cloud.supabase import push as push_module, rows
from openflight.cloud.supabase.client import SupabaseClient
from openflight.cloud.supabase.push import (
    AGAIN_NAME,
    BATCH_SIZE,
    LOCK_NAME,
    PAUSE_S,
    STOP_BUSY,
    STOP_INACTIVE,
    STOP_PAUSED,
    STOP_RETRY,
    STOP_SCHEMA_MISSING,
    STOP_UNAUTHORIZED,
    SessionProgress,
    sidecar_path,
)
from openflight.clubs import ClubType
from openflight.launch_monitor import Shot
from openflight.session_logger import SessionLogger

NOW = 1_800_000_000.0


def _push(log_dir, fake, config=None, out=None, **kwargs):
    config = config or make_config()
    client = SupabaseClient(
        config.supabase_url, config.supabase_key, config.supabase_device_token, request_fn=fake
    )
    lines = [] if out is None else out
    kwargs.setdefault("now_fn", lambda: NOW)
    return push_module.push(config, log_dir, client, out=lines.append, **kwargs)


def _session(log_dir, name="session_20261114_090000_range.jsonl", session_uuid=SESSION_UUID):
    session = SessionFile(Path(log_dir) / name)
    session.start(session_uuid=session_uuid)
    return session


def _shot_numbers(fake):
    return [row["shot_number"] for row in fake.sent_rows("golf_shots")]


def _failing_at(fake, call_number, response):
    """A transport that answers ``response`` to its Nth request and passes the rest to ``fake``."""
    calls = {"n": 0}

    def transport(method, url, data=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == call_number:
            return response
        return fake(method, url, data=data, headers=headers, timeout=timeout)

    return transport


def uuid4():
    return str(uuid.uuid4())


class _CountingHandle:
    """Wraps a binary session file and counts the bytes iterated from it."""

    def __init__(self, handle, counter):
        self._handle = handle
        self._counter = counter

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()

    def seek(self, position):
        self._handle.seek(position)

    def __iter__(self):
        for line in self._handle:
            self._counter.append(len(line))
            yield line


class TestIncremental:
    def test_first_push_sends_session_row_then_shots(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1)
        session.shot(2)
        fake = FakeSupabase()

        summary = _push(tmp_path, fake)

        assert [r.table for r in fake.requests] == ["golf_sessions", "golf_shots"]
        assert list(fake.sessions) == [SESSION_UUID]
        assert _shot_numbers(fake) == [1, 2]
        assert summary.shots_sent == 2
        assert summary.stopped is None

    def test_only_lines_appended_since_the_last_run_are_sent(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1)
        session.shot(2)
        fake = FakeSupabase()
        _push(tmp_path, fake)

        session.shot(3)
        before = len(fake.requests)
        _push(tmp_path, fake)

        new_requests = fake.requests[before:]
        assert [r.table for r in new_requests] == ["golf_shots"]
        assert [row["shot_number"] for row in new_requests[0].body] == [3]

    def test_already_read_raw_data_is_never_read_again(self, tmp_path, monkeypatch):
        session = _session(tmp_path)
        session.shot(1)
        session.raw_capture(samples=20000)
        fake = FakeSupabase()
        _push(tmp_path, fake)
        progress = SessionProgress.load(sidecar_path(session.path))
        assert progress.offset == session.path.stat().st_size

        session.shot(2)
        appended = session.path.stat().st_size - progress.offset
        read_sizes = []
        real_open = Path.open

        def counting_open(self, mode="r", *args, **kwargs):
            handle = real_open(self, mode, *args, **kwargs)
            if self.suffix == ".jsonl" and "b" in mode:
                return _CountingHandle(handle, read_sizes)
            return handle

        monkeypatch.setattr(Path, "open", counting_open)
        _push(tmp_path, fake)

        assert sum(read_sizes) == appended
        assert _shot_numbers(fake) == [1, 2]

    def test_nothing_new_makes_no_request(self, tmp_path):
        _session(tmp_path).shot(1)
        fake = FakeSupabase()
        _push(tmp_path, fake)
        out = []
        _push(tmp_path, fake, out=out)
        assert len(fake.requests) == 2
        assert out == ["Supabase: nothing new to upload."]

    def test_line_being_written_waits_for_the_next_run(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1)
        second = shot_entry(2)
        session.write_partial(second)
        fake = FakeSupabase()

        _push(tmp_path, fake)
        assert _shot_numbers(fake) == [1]
        progress = SessionProgress.load(sidecar_path(session.path))
        assert progress.offset < session.path.stat().st_size

        session.finish_partial(second)
        _push(tmp_path, fake)
        assert _shot_numbers(fake) == [1, 2]
        assert fake.shots[rows.shot_id(SESSION_UUID, 2, 0)]["data"] == second

    def test_session_end_updates_the_session_row(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1)
        fake = FakeSupabase()
        _push(tmp_path, fake)
        assert fake.sessions[SESSION_UUID]["ended_at"] is None

        session.end()
        _push(tmp_path, fake)

        row = fake.sessions[SESSION_UUID]
        assert row["ended_at"] == rows.to_timestamptz("2026-11-14T10:00:00")
        assert row["started_at"] == rows.to_timestamptz("2026-11-14T09:00:00")
        assert row["info"]["session_start"]["session_uuid"] == SESSION_UUID
        assert row["info"]["session_end"]["stats"] == {"shots_detected": 2, "errors": 0}

    def test_session_row_is_sent_before_any_shot(self, tmp_path):
        session = _session(tmp_path)
        _push(tmp_path, FakeSupabase())  # session row only
        session.shot(1)
        fake = FakeSupabase()
        sidecar_path(session.path).unlink()
        _push(tmp_path, fake)
        assert [r.table for r in fake.requests] == ["golf_sessions", "golf_shots"]

    def test_resending_everything_is_an_idempotent_upsert(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1)
        session.shot(2)
        session.end()
        fake = FakeSupabase()
        _push(tmp_path, fake)
        first_ids = [row["id"] for row in fake.sent_rows("golf_shots")]
        stored = json.dumps(fake.tables, sort_keys=True)

        _push(tmp_path, fake, resend=True)

        second_ids = [row["id"] for row in fake.sent_rows("golf_shots")][len(first_ids) :]
        assert second_ids == first_ids
        assert json.dumps(fake.tables, sort_keys=True) == stored

    def test_shots_go_in_batches(self, tmp_path):
        session = _session(tmp_path)
        for number in range(1, 2 * BATCH_SIZE + 21):
            session.shot(number)
        fake = FakeSupabase()

        _push(tmp_path, fake)

        sizes = [len(r.body) for r in fake.upserts("golf_shots")]
        assert sizes == [BATCH_SIZE, BATCH_SIZE, 20]
        assert len(fake.shots) == 2 * BATCH_SIZE + 20

    def test_a_shot_repeated_in_one_batch_is_sent_once(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1, ball_speed_mph=100.0)
        session.shot(1, ball_speed_mph=101.0)
        fake = FakeSupabase()
        _push(tmp_path, fake)
        sent = fake.sent_rows("golf_shots")
        assert len(sent) == 1
        assert sent[0]["ball_speed_mph"] == 101.0

    def test_oldest_session_goes_first(self, tmp_path):
        old_uuid = uuid4()
        _session(tmp_path, "session_20261113_090000_range.jsonl", old_uuid).shot(1)
        _session(tmp_path, "session_20261114_090000_range.jsonl").shot(1)
        fake = FakeSupabase()
        _push(tmp_path, fake)
        assert [r.body[0]["id"] for r in fake.upserts("golf_sessions")] == [old_uuid, SESSION_UUID]
        assert [r.body[0]["session_id"] for r in fake.upserts("golf_shots")] == [
            old_uuid,
            SESSION_UUID,
        ]

    def test_session_without_uuid_uses_the_fallback_id(self, tmp_path):
        name = "session_20240101_090000_range.jsonl"
        _session(tmp_path, name, session_uuid=None).shot(1)
        fake = FakeSupabase()
        _push(tmp_path, fake)
        expected = rows.session_id(None, name)
        assert list(fake.sessions) == [expected]
        assert fake.sent_rows("golf_shots")[0]["session_id"] == expected

    def test_truncated_file_starts_over(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1)
        session.shot(2)
        fake = FakeSupabase()
        _push(tmp_path, fake)

        session.path.write_text("")
        session.start(session_uuid=str(uuid4()))
        session.shot(1)
        _push(tmp_path, fake)

        assert len(fake.sessions) == 2

    def test_unreadable_sidecar_starts_over(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1)
        sidecar_path(session.path).write_text("{not json")
        fake = FakeSupabase()
        _push(tmp_path, fake)
        assert _shot_numbers(fake) == [1]

    def test_no_session_logs_yet(self, tmp_path):
        out = []
        summary = _push(tmp_path / "missing", FakeSupabase(), out=out)
        assert summary.stopped is None
        assert "no session logs yet" in out[0]

    def test_inactive_config_sends_nothing(self, tmp_path):
        _session(tmp_path).shot(1)
        fake = FakeSupabase()
        summary = _push(tmp_path, fake, config=make_config(enabled=False))
        assert summary.stopped == STOP_INACTIVE
        assert fake.requests == []

    def test_a_session_logged_by_the_real_session_logger(self, tmp_path):
        logger = SessionLogger(log_dir=tmp_path, location="range")
        logger.start_session(mode="mock")
        for number, speed in ((1, 140.0), (2, 150.0)):
            logger.log_shot(
                Shot(
                    ball_speed_mph=speed,
                    timestamp=datetime(2026, 11, 14, 9, 30, number),
                    club=ClubType.DRIVER,
                    shot_number=number,
                )
            )
        logger.end_session()
        fake = FakeSupabase()

        _push(tmp_path, fake)

        session_uuid = json.loads(logger.session_path.read_text().splitlines()[0])["session_uuid"]
        assert list(fake.sessions) == [session_uuid]
        assert fake.sessions[session_uuid]["ended_at"] is not None
        assert sorted(row["ball_speed_mph"] for row in fake.shots.values()) == [140.0, 150.0]
        assert {row["club"] for row in fake.shots.values()} == {"driver"}


class TestPrivacy:
    RAW_TYPES = (
        "rolling_buffer_capture",
        "iq_blocks",
        "kld7_buffer",
        "iwr6843_capture",
        "iq_reading",
        "connection",
        "config_change",
        "reading_accepted",
    )

    def test_only_allowlisted_entries_leave_the_pi(self, tmp_path):
        session = _session(tmp_path)
        session.raw_capture()
        for entry_type in self.RAW_TYPES:
            session.write({"ts": "t", "type": entry_type, "secret_marker": "RAW-DATA"})
        session.shot(1)
        session.end()
        fake = FakeSupabase()

        _push(tmp_path, fake)

        sent = json.dumps([r.body for r in fake.requests])
        assert "RAW-DATA" not in sent
        assert "i_samples" not in sent
        for entry_type in self.RAW_TYPES:
            assert f'"{entry_type}"' not in sent
        assert len(fake.shots) == 1

    def test_uses_the_shared_allowlist(self, tmp_path, monkeypatch):
        # Narrowing filtering's allowlist must narrow Supabase too (no fork).
        monkeypatch.setattr(
            filtering, "KEEP_ENTRY_TYPES", filtering.KEEP_ENTRY_TYPES - {"shot_detected"}
        )
        _session(tmp_path).shot(1)
        fake = FakeSupabase()
        _push(tmp_path, fake)
        assert fake.upserts("golf_shots") == []

    def test_oversize_shot_line_is_dropped_and_reported(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1, readings=["x" * (filtering.MAX_LINE_BYTES + 10)])
        session.shot(2)
        fake = FakeSupabase()
        out = []
        summary = _push(tmp_path, fake, out=out)
        assert _shot_numbers(fake) == [2]
        assert summary.oversize_shots == 1
        assert any("over 32 KB not sent" in line for line in out)


class TestFailures:
    def test_network_error_keeps_the_offset_and_retries_next_run(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1)
        fake = FakeSupabase(script=[CloudNetworkError("offline")])

        summary = _push(tmp_path, fake)

        assert summary.stopped == STOP_RETRY
        assert not sidecar_path(session.path).exists()
        _push(tmp_path, fake)
        assert _shot_numbers(fake) == [1]
        assert list(fake.sessions) == [SESSION_UUID]

    def test_server_error_on_shots_resends_them_next_run(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1)
        fake = FakeSupabase()

        summary = _push(tmp_path, _failing_at(fake, 2, error_response(503)))

        assert summary.stopped == STOP_RETRY
        assert list(fake.sessions) == [SESSION_UUID]
        assert fake.shots == {}
        assert not sidecar_path(session.path).exists()
        retry = FakeSupabase()
        _push(tmp_path, retry)
        assert [r.table for r in retry.requests] == ["golf_sessions", "golf_shots"]
        assert _shot_numbers(retry) == [1]

    def test_failure_after_some_batches_keeps_their_progress(self, tmp_path):
        session = _session(tmp_path)
        for number in range(1, 2 * BATCH_SIZE + 21):
            session.shot(number)
        fake = FakeSupabase()

        # Session row, batch 1, then batch 2 fails.
        summary = _push(tmp_path, _failing_at(fake, 3, error_response(500)))
        assert summary.stopped == STOP_RETRY
        assert len(fake.shots) == BATCH_SIZE

        retry = FakeSupabase()
        _push(tmp_path, retry)
        assert [r.table for r in retry.requests] == ["golf_shots", "golf_shots"]
        assert len(retry.shots) == BATCH_SIZE + 20
        assert min(retry.shots[i]["shot_number"] for i in retry.shots) == BATCH_SIZE + 1

    def test_unauthorized_pauses_background_runs(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1)
        fake = FakeSupabase(script=[error_response(401, "42501", "row-level security")])
        out = []

        summary = _push(tmp_path, fake, out=out)

        assert summary.stopped == STOP_UNAUTHORIZED
        assert summary.needs_owner
        assert "not registered" in out[-1]
        state = push_module.load_state(tmp_path)
        assert state["paused"]["reason"] == STOP_UNAUTHORIZED
        assert state["paused"]["until"] == NOW + PAUSE_S

        background = _push(tmp_path, fake, respect_pause=True, now_fn=lambda: NOW + 60)
        assert background.stopped == STOP_PAUSED
        assert len(fake.requests) == 1
        assert not sidecar_path(session.path).exists()

    def test_pause_ends_after_its_time(self, tmp_path):
        _session(tmp_path).shot(1)
        fake = FakeSupabase(script=[error_response(403, "", "forbidden")])
        _push(tmp_path, fake)
        later = _push(tmp_path, fake, respect_pause=True, now_fn=lambda: NOW + PAUSE_S + 1)
        assert later.stopped is None
        assert _shot_numbers(fake) == [1]
        assert "paused" not in push_module.load_state(tmp_path)

    def test_a_manual_run_ignores_the_pause(self, tmp_path):
        _session(tmp_path).shot(1)
        fake = FakeSupabase(script=[error_response(401)])
        _push(tmp_path, fake)
        manual = _push(tmp_path, fake, now_fn=lambda: NOW + 60)
        assert manual.stopped is None
        assert _shot_numbers(fake) == [1]

    def test_changing_the_config_lifts_the_pause(self, tmp_path):
        _session(tmp_path).shot(1)
        fake = FakeSupabase(script=[error_response(401)])
        _push(tmp_path, fake)
        fixed = make_config(supabase_key="sb_publishable_TheRightKey")
        result = _push(tmp_path, fake, config=fixed, respect_pause=True, now_fn=lambda: NOW + 60)
        assert result.stopped is None

    def test_missing_schema_pauses_with_instructions(self, tmp_path):
        _session(tmp_path).shot(1)
        fake = FakeSupabase(script=[error_response(404, "PGRST205", "Could not find the table")])
        out = []
        summary = _push(tmp_path, fake, out=out)
        assert summary.stopped == STOP_SCHEMA_MISSING
        assert "schema.sql" in out[-1]
        assert push_module.load_state(tmp_path)["paused"]["reason"] == STOP_SCHEMA_MISSING

    def test_rejected_session_is_skipped_and_others_continue(self, tmp_path):
        older = _session(tmp_path, "session_20261113_090000_range.jsonl", str(uuid4()))
        older.shot(1)
        newer = _session(tmp_path)
        newer.shot(1)
        fake = FakeSupabase()
        # The older session goes first: its row is fine, its shots are refused.
        refused = error_response(400, "22P02", "invalid input syntax")
        out = []
        summary = _push(tmp_path, _failing_at(fake, 2, refused), out=out)

        assert summary.rejected == [older.path.name]
        assert summary.stopped is None
        assert [row["session_id"] for row in fake.shots.values()] == [SESSION_UUID]
        assert SessionProgress.load(sidecar_path(older.path)).rejected["status"] == 400
        assert any("refused this session" in line for line in out)

        older.shot(2)
        quiet = FakeSupabase()
        _push(tmp_path, quiet)
        assert quiet.requests == []

        _push(tmp_path, quiet, resend=True)
        assert sorted(_shot_numbers(quiet)) == [1, 1, 2]

    def test_success_records_the_last_upload(self, tmp_path):
        _session(tmp_path).shot(1)
        _push(tmp_path, FakeSupabase())
        state = push_module.load_state(tmp_path)
        assert state["last_upload_shots"] == 1
        assert "last_error" not in state


class TestLock:
    def test_busy_while_another_run_holds_the_lock(self, tmp_path):
        _session(tmp_path).shot(1)
        fd = os.open(str(tmp_path / LOCK_NAME), os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        fake = FakeSupabase()
        out = []
        try:
            summary = _push(tmp_path, fake, out=out)
        finally:
            os.close(fd)
        assert summary.stopped == STOP_BUSY
        assert fake.requests == []
        assert (tmp_path / AGAIN_NAME).exists()
        assert "another upload is running" in out[0]

    def test_holder_makes_another_pass_for_a_run_that_found_it_busy(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1)
        fake = FakeSupabase()
        inner = {}

        def transport(method, url, data=None, headers=None, timeout=None):
            reply = fake(method, url, data=data, headers=headers, timeout=timeout)
            if not inner:
                # A shot lands and its push starts while this run is mid-upload.
                session.shot(2)
                inner["summary"] = _push(tmp_path, FakeSupabase())
            return reply

        outer = _push(tmp_path, transport)

        assert inner["summary"].stopped == STOP_BUSY
        assert outer.stopped is None
        assert _shot_numbers(fake) == [1, 2]
        assert not (tmp_path / AGAIN_NAME).exists()

    def test_concurrent_runs_never_upload_a_shot_twice(self, tmp_path):
        session = _session(tmp_path)
        for number in range(1, 6):
            session.shot(number)
        fake = FakeSupabase()
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()

        def slow(method, url, data=None, headers=None, timeout=None):
            entered.set()
            release.wait(5)
            with lock:
                return fake(method, url, data=data, headers=headers, timeout=timeout)

        results = {}
        first = threading.Thread(target=lambda: results.setdefault("a", _push(tmp_path, slow)))
        first.start()
        assert entered.wait(5)
        results["b"] = _push(tmp_path, slow)
        release.set()
        first.join(5)

        assert results["b"].stopped == STOP_BUSY
        ids = [row["id"] for row in fake.sent_rows("golf_shots")]
        assert len(ids) == len(set(ids)) == 5


class TestDryRun:
    def test_dry_run_sends_and_saves_nothing(self, tmp_path):
        session = _session(tmp_path)
        session.shot(1)
        session.raw_capture()
        fake = FakeSupabase()
        out = []

        summary = _push(tmp_path, fake, out=out, dry_run=True)

        assert fake.requests == []
        assert not sidecar_path(session.path).exists()
        assert not (tmp_path / push_module.STATE_NAME).exists()
        assert summary.shots_sent == 1
        assert out[0] == (
            f"Supabase (dry run): {session.path.name}: would send the session row and 1 shot(s)."
        )

    def test_dry_run_works_without_config(self, tmp_path):
        _session(tmp_path).shot(1)
        fake = FakeSupabase()
        summary = _push(tmp_path, fake, config=make_config(supabase_url=""), dry_run=True)
        assert summary.shots_sent == 1
        assert fake.requests == []


def test_final_report_names_the_project_on_network_errors(tmp_path):
    _session(tmp_path).shot(1)
    out = []
    _push(tmp_path, FakeSupabase(script=[CloudNetworkError("down")]), out=out)
    assert PROJECT_URL in out[-1]
    assert "retry" in out[-1]


@pytest.mark.parametrize("bad", ['{"offset": -5, "session_id": "x"}', '{"offset": true}', "[]"])
def test_bad_sidecar_values_do_not_break_loading(tmp_path, bad):
    path = tmp_path / "s.jsonl.supabase"
    path.write_text(bad)
    progress = SessionProgress.load(path)
    assert progress.offset == 0


class TestLinkRecord:
    """What a run tells the server's link through the state file."""

    def _link(self, tmp_path):
        return push_module.load_state(tmp_path).get("link")

    def test_success_is_online(self, tmp_path):
        _session(tmp_path).shot(1)
        _push(tmp_path, FakeSupabase())
        assert self._link(tmp_path) == {"state": "online", "message": "", "reason": "", "at": NOW}

    @pytest.mark.parametrize(
        "failure", [CloudNetworkError("Name or service not known"), error_response(503)]
    )
    def test_no_answer_or_5xx_is_offline(self, tmp_path, failure):
        _session(tmp_path).shot(1)
        _push(tmp_path, FakeSupabase(script=[failure]))
        assert self._link(tmp_path)["state"] == "offline"

    @pytest.mark.parametrize(
        "failure", [error_response(401), error_response(404, "PGRST205", "no table")]
    )
    def test_refusal_needs_attention(self, tmp_path, failure):
        _session(tmp_path).shot(1)
        summary = _push(tmp_path, FakeSupabase(script=[failure]))
        assert self._link(tmp_path)["state"] == "needs_attention"
        assert self._link(tmp_path)["reason"] == summary.stopped

    def test_a_run_without_requests_records_nothing(self, tmp_path):
        _session(tmp_path).shot(1)
        _push(tmp_path, FakeSupabase())
        offline = FakeSupabase(script=[CloudNetworkError("offline")])
        _push(tmp_path, offline, now_fn=lambda: NOW + 5)
        assert offline.requests == []
        assert self._link(tmp_path)["at"] == NOW  # nothing new: no request, no news
