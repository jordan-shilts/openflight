"""Tests for the server's Supabase link: online/offline state, probing, catch-up, pending."""

import threading
import time
import uuid
from pathlib import Path

import pytest
from supabase_fakes import FakeSupabase, SessionFile, error_response, make_config

from openflight.cloud.client import CloudNetworkError, HttpResponse
from openflight.cloud.supabase import push
from openflight.cloud.supabase.client import PROBE_TIMEOUT_S, SupabaseClient
from openflight.cloud.supabase.link import (
    NEEDS_ATTENTION,
    NOT_SET_UP,
    OFFLINE,
    ONLINE,
    LinkStatus,
    LinkTiming,
    SupabaseLink,
)

PROBE_S = 30.0
PUSH_S = 10.0


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Harness:
    """A link driven step by step, with fake clocks and a fake project."""

    def __init__(self, log_dir: Path, config=None):
        self.log_dir = Path(log_dir)
        self.config = config or make_config()
        self.set_up = True
        self.fake = FakeSupabase()
        self.transport = self.fake  # what the probe and pushes send through
        self.clock = _Clock(1000.0)
        self.wall = _Clock(1_800_000_000.0)
        self.pushes = 0
        self.changes = []
        self.sessions = {}
        self.link = SupabaseLink(
            self.resolve,
            run_push=self.run_push,
            client_factory=self.client,
            on_change=self.changes.append,
            timing=LinkTiming(
                probe_interval_s=PROBE_S,
                push_interval_s=PUSH_S,
                refresh_interval_s=PROBE_S,
                clock=self.clock,
                wall_clock=self.wall,
            ),
        )

    def resolve(self):
        if not self.set_up:
            return None, None, "Supabase is not set up"
        return self.config, self.log_dir, ""

    def client(self, config):
        return SupabaseClient(
            config.supabase_url,
            config.supabase_key,
            config.supabase_device_token,
            request_fn=lambda *args, **kwargs: self.transport(*args, **kwargs),
        )

    def run_push(self, config, log_dir, stop):
        """What the detached ``supabase push --background`` does, in-process."""
        self.pushes += 1
        push.push(
            config,
            log_dir,
            self.client(config),
            respect_pause=True,
            out=lambda _line: None,
            now_fn=self.wall,
        )

    def session(self, name="session_20261114_090000_range.jsonl", session_uuid=None):
        if name not in self.sessions:
            session = SessionFile(self.log_dir / name)
            session.start(session_uuid=session_uuid or str(uuid.uuid4()))
            self.sessions[name] = session
        return self.sessions[name]

    def shot(self, number, name="session_20261114_090000_range.jsonl"):
        self.session(name).shot(number)
        self.link.notify_shot()

    def advance(self, seconds):
        self.clock.now += seconds
        self.wall.now += seconds

    def step(self):
        self.link.step()
        return self.link.status()

    def uploaded_shots(self):
        return sorted(row["shot_number"] for row in self.fake.shots.values())


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def _online(h):
    h.shot(1)
    status = h.step()
    assert status.state == ONLINE
    return status


class TestStartup:
    def test_probes_then_sends_what_is_waiting(self, h):
        h.session().shot(1)

        status = h.step()

        assert [r.method for r in h.fake.requests] == ["GET", "POST", "POST"]
        assert h.fake.probes[0].url == "https://abcdefghijklmnopqrst.supabase.co/rest/v1/"
        assert h.fake.probes[0].headers["apikey"] == make_config().supabase_key
        assert h.fake.probes[0].timeout == PROBE_TIMEOUT_S
        assert status == LinkStatus(ONLINE, 0, "")
        assert h.uploaded_shots() == [1]

    def test_starts_offline_when_the_probe_gets_no_answer(self, h):
        h.fake.offline = True
        h.session().shot(1)
        status = h.step()
        assert status == LinkStatus(
            OFFLINE, 1, "no answer from https://abcdefghijklmnopqrst.supabase.co"
        )
        assert h.pushes == 0


class TestOnline:
    def test_each_shot_pushes_at_most_once_per_interval(self, h):
        _online(h)
        pushes = h.pushes

        h.advance(1)
        h.shot(2)
        h.step()
        h.advance(2)
        h.shot(3)
        h.step()
        assert h.pushes == pushes  # inside the interval: waiting

        h.advance(PUSH_S)
        status = h.step()
        assert h.pushes == pushes + 1
        assert h.uploaded_shots() == [1, 2, 3]
        assert status == LinkStatus(ONLINE, 0, "")

    def test_no_polling_while_online(self, h):
        _online(h)
        requests = len(h.fake.requests)
        for _ in range(10):
            h.advance(PROBE_S)
            h.step()
        assert len(h.fake.requests) == requests

    def test_next_wake_is_the_push_interval_after_a_shot(self, h):
        _online(h)
        h.advance(3)
        h.shot(2)
        h.step()
        assert h.link._seconds_to_next_step() == pytest.approx(PUSH_S - 3)


class TestOnlineOfflineOnline:
    def test_transitions(self, h):
        _online(h)

        # The internet goes: the next shot's push gets no answer.
        h.fake.offline = True
        h.advance(PUSH_S)
        h.shot(2)
        status = h.step()
        assert status.state == OFFLINE
        assert status.pending == 1
        assert "network error" in status.message

        # Offline: shots wait, nothing is sent, no push is started.
        pushes = h.pushes
        h.shot(3)
        h.advance(1)
        assert h.step().pending == 2
        assert h.pushes == pushes

        # Back: the next probe answers and one push sends the backlog.
        h.fake.offline = False
        h.advance(PROBE_S)
        status = h.step()
        assert status == LinkStatus(ONLINE, 0, "")
        assert h.pushes == pushes + 1
        assert h.uploaded_shots() == [1, 2, 3]

        # And per-shot pushes resume.
        h.advance(PUSH_S)
        h.shot(4)
        h.step()
        assert h.uploaded_shots() == [1, 2, 3, 4]

    def test_a_5xx_counts_as_offline_and_the_probe_waits_for_it(self, h):
        _online(h)
        h.fake.script = [error_response(503, "", "Service Unavailable")]
        h.advance(PUSH_S)
        h.shot(2)
        assert h.step().state == OFFLINE

        h.fake.script = [HttpResponse(503, {}, b"")]
        h.advance(PROBE_S)
        assert h.step().state == OFFLINE
        assert h.uploaded_shots() == [1]

    def test_reported_changes(self, h):
        _online(h)
        h.fake.offline = True
        h.advance(PUSH_S)
        h.shot(2)
        h.step()
        h.fake.offline = False
        h.advance(PROBE_S)
        h.step()

        states = [change.state for change in h.changes]
        transitions = [s for i, s in enumerate(states) if i == 0 or s != states[i - 1]]
        assert transitions == [OFFLINE, ONLINE, OFFLINE, ONLINE]  # starts by checking
        assert all(a != b for a, b in zip(h.changes, h.changes[1:]))


class TestProbeInterval:
    def test_one_probe_every_interval_while_offline(self, h):
        h.fake.offline = True
        h.session().shot(1)
        h.step()
        assert len(h.fake.probes) == 1

        for seconds in (1, 10, 28):
            h.clock.now = 1000.0 + seconds
            h.step()
        assert len(h.fake.probes) == 1

        h.clock.now = 1000.0 + PROBE_S
        h.step()
        assert len(h.fake.probes) == 2
        assert h.link._seconds_to_next_step() == pytest.approx(PROBE_S)

    def test_shots_while_offline_do_not_probe_early(self, h):
        h.fake.offline = True
        h.session().shot(1)
        h.step()
        for number in range(2, 8):
            h.advance(1)
            h.shot(number)
            h.step()
        assert len(h.fake.probes) == 1
        assert h.link.status().pending == 7


class TestCatchUp:
    def test_backlog_goes_oldest_first_in_batches(self, h):
        h.fake.offline = True
        older, newer = "session_20261113_090000_range.jsonl", "session_20261114_090000_range.jsonl"
        h.session(older)
        h.session(newer)
        h.step()
        for number in range(1, push.BATCH_SIZE + 6):
            h.shot(number, older)
        for number in range(1, 4):
            h.shot(number, newer)
        assert h.step().pending == push.BATCH_SIZE + 8

        h.fake.offline = False
        h.fake.requests.clear()
        h.advance(PROBE_S)
        status = h.step()

        sent = [
            (r.table, r.body[0].get("session_id", r.body[0]["id"]), len(r.body))
            for r in h.fake.requests
            if r.method == "POST"
        ]
        older_id = h.fake.upserts("golf_sessions")[0].body[0]["id"]
        newer_id = h.fake.upserts("golf_sessions")[1].body[0]["id"]
        assert sent == [
            ("golf_sessions", older_id, 1),
            ("golf_shots", older_id, push.BATCH_SIZE),
            ("golf_shots", older_id, 5),
            ("golf_sessions", newer_id, 1),
            ("golf_shots", newer_id, 3),
        ]
        assert status.pending == 0


class TestNotSetUp:
    def test_no_request_at_all(self, h):
        h.set_up = False
        h.session().shot(1)
        for _ in range(5):
            h.shot(2)
            h.advance(PROBE_S)
            assert h.step() == LinkStatus(NOT_SET_UP, None, "Supabase is not set up")
        assert h.fake.requests == []
        assert h.pushes == 0

    def test_setting_it_up_sends_the_backlog(self, h):
        h.set_up = False
        h.session().shot(1)
        h.step()
        h.set_up = True
        h.advance(1)
        assert h.step().state == ONLINE
        assert h.uploaded_shots() == [1]


class TestNeedsAttention:
    def test_refused_pi_is_never_reported_offline(self, h):
        h.fake.script = [HttpResponse(200, {}, b"{}"), error_response(401, "42501", "RLS")]
        h.session().shot(1)

        status = h.step()

        assert status.state == NEEDS_ATTENTION
        assert status.reason == "unauthorized"
        assert status.pending == 1
        assert "42501" in status.message

    def test_missing_tables_say_so(self, h):
        h.fake.script = [HttpResponse(200, {}, b"{}"), error_response(404, "PGRST205", "no table")]
        h.session().shot(1)
        h.step()
        h.advance(1)
        status = h.step()  # now from the pause, not the push's record
        assert (status.state, status.reason) == (NEEDS_ATTENTION, "schema_missing")

    def test_no_requests_while_paused_even_with_the_internet_gone(self, h):
        h.fake.script = [HttpResponse(200, {}, b"{}"), error_response(403, "", "forbidden")]
        h.session().shot(1)
        h.step()
        h.fake.offline = True
        requests = len(h.fake.requests)
        for _ in range(10):
            h.advance(PROBE_S)
            h.shot(2)
            assert h.step().state == NEEDS_ATTENTION
        assert len(h.fake.requests) == requests

    def test_a_probe_answering_401_is_not_offline(self, h):
        h.fake.script = [error_response(401, "", "Invalid API key")] * 2
        h.session().shot(1)
        assert h.step().state == NEEDS_ATTENTION

    def test_lifting_the_pause_resumes(self, h):
        h.fake.script = [HttpResponse(200, {}, b"{}"), error_response(401)]
        h.session().shot(1)
        h.step()

        push.clear_pause(h.log_dir)  # what `supabase test` does once registered
        h.advance(PUSH_S)
        status = h.step()

        assert status.state == ONLINE
        assert h.uploaded_shots() == [1]


class TestPending:
    def test_after_appends_and_after_uploads(self, h):
        h.fake.offline = True
        h.session().shot(1)
        assert h.step().pending == 1
        h.shot(2)
        h.shot(3)
        assert h.link.status().pending == 3
        assert push.pending_shot_count(h.log_dir) == 3

        h.fake.offline = False
        h.advance(PROBE_S)
        assert h.step().pending == 0
        assert push.pending_shot_count(h.log_dir) == 0

        h.advance(1)
        h.shot(4)
        assert h.link.status().pending == 1

    def test_counts_only_allowlisted_complete_shot_lines(self, h):
        session = h.session()
        session.shot(1)
        session.raw_capture()
        session.write({"ts": "t", "type": "iq_blocks", "note": "shot_detected"})
        session.write_partial({"type": "shot_detected", "shot_number": 2})
        assert push.pending_shot_count(h.log_dir) == 1

    def test_a_refused_session_is_not_pending(self, h):
        h.fake.script = [HttpResponse(200, {}, b"{}"), error_response(400, "22P02", "bad")]
        h.session().shot(1)
        h.step()
        assert push.pending_shot_count(h.log_dir) == 0

    def test_a_notified_shot_already_counted_on_disk_is_not_counted_twice(self, h):
        h.fake.offline = True
        h.shot(1)  # on disk and notified before the first count
        h.advance(1)
        assert h.step().pending == 1


def _failing_post(fake, post_number, error):
    """A transport whose Nth POST raises ``error``; everything else reaches ``fake``."""
    posts = {"n": 0}

    def transport(method, url, data=None, headers=None, timeout=None):
        if method == "POST":
            posts["n"] += 1
            if posts["n"] == post_number:
                raise error
        return fake(method, url, data=data, headers=headers, timeout=timeout)

    return transport


class TestDurability:
    """A shot counts as uploaded only once Supabase answered 2xx for its batch."""

    SHOTS = range(1, 2 * push.BATCH_SIZE + 1)
    SECOND_BATCH = list(range(push.BATCH_SIZE + 1, 2 * push.BATCH_SIZE + 1))

    def test_cut_connection_mid_batch_resends_that_batch(self, h):
        h.fake.offline = True
        h.session()
        h.step()
        for number in self.SHOTS:
            h.shot(number)

        # Back online, but the connection drops during the second shot batch
        # (POSTs: session row, batch 1, batch 2).
        h.fake.offline = False
        h.transport = _failing_post(h.fake, 3, CloudNetworkError("Connection reset by peer"))
        h.advance(PROBE_S)
        status = h.step()
        assert status.state == OFFLINE
        assert status.pending == push.BATCH_SIZE
        assert len(h.fake.shots) == push.BATCH_SIZE

        h.transport = h.fake
        h.fake.requests.clear()
        h.advance(PROBE_S)
        assert h.step() == LinkStatus(ONLINE, 0, "")
        assert [row["shot_number"] for row in h.fake.sent_rows("golf_shots")] == self.SECOND_BATCH
        assert len(h.fake.shots) == 2 * push.BATCH_SIZE

    def test_crash_mid_batch_loses_nothing(self, h):
        session = h.session()
        for number in self.SHOTS:
            session.shot(number)
        crashing = h.client(h.config)
        h.transport = _failing_post(h.fake, 3, SystemExit("killed mid-request"))

        with pytest.raises(SystemExit):
            push.push(h.config, h.log_dir, crashing, out=lambda _line: None)
        assert len(h.fake.shots) == push.BATCH_SIZE
        first_batch = dict(h.fake.shots)

        h.transport = h.fake
        h.fake.requests.clear()
        push.push(h.config, h.log_dir, h.client(h.config), out=lambda _line: None)

        assert [row["shot_number"] for row in h.fake.sent_rows("golf_shots")] == self.SECOND_BATCH
        assert len(h.fake.shots) == 2 * push.BATCH_SIZE
        assert all(h.fake.shots[key] == row for key, row in first_batch.items())
        assert push.pending_shot_count(h.log_dir) == 0


class TestThread:
    def test_shutdown_stops_the_probe(self, tmp_path):
        fake = FakeSupabase(offline=True)
        probed = threading.Event()

        def client(config):
            probed.set()
            return SupabaseClient(
                config.supabase_url, config.supabase_key, "t" * 64, request_fn=fake
            )

        link = SupabaseLink(
            lambda: (make_config(), tmp_path, ""),
            run_push=lambda *args: None,
            client_factory=client,
            timing=LinkTiming(probe_interval_s=0.01, refresh_interval_s=0.01),
        )
        link.start()
        assert probed.wait(5)
        link.stop(timeout_s=5)

        assert not link._thread.is_alive()
        probes = len(fake.probes)
        time.sleep(0.1)
        assert len(fake.probes) == probes

    def test_notify_wakes_the_thread_to_push(self, tmp_path):
        session = SessionFile(tmp_path / "session_20261114_090000_range.jsonl")
        session.start()
        pushed = threading.Event()
        fake = FakeSupabase()

        def run_push(config, log_dir, stop):
            client = SupabaseClient(
                config.supabase_url, config.supabase_key, "t" * 64, request_fn=fake
            )
            push.push(config, log_dir, client, respect_pause=True, out=lambda _line: None)
            if fake.shots:
                pushed.set()

        link = SupabaseLink(
            lambda: (make_config(), tmp_path, ""),
            run_push=run_push,
            client_factory=lambda config: SupabaseClient(
                config.supabase_url, config.supabase_key, "t" * 64, request_fn=fake
            ),
            timing=LinkTiming(push_interval_s=0.0, refresh_interval_s=60.0),
        )
        link.start()
        try:
            session.shot(1)
            link.notify_shot()
            assert pushed.wait(5)
        finally:
            link.stop(timeout_s=5)

    def test_a_failing_step_does_not_end_the_thread(self, tmp_path):
        calls = []

        def resolve():
            calls.append(1)
            raise RuntimeError("disk gone")

        link = SupabaseLink(resolve, timing=LinkTiming(refresh_interval_s=0.01))
        link.start()
        try:
            deadline = time.monotonic() + 5
            while len(calls) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            link.stop(timeout_s=5)
        assert len(calls) >= 3

    def test_notify_never_blocks_while_a_step_runs(self, tmp_path):
        release = threading.Event()

        def slow_resolve():
            release.wait(5)
            return None, None, "Supabase is not set up"

        link = SupabaseLink(slow_resolve)
        link.start()
        try:
            started = time.monotonic()
            for _ in range(100):
                link.notify_shot()
            assert time.monotonic() - started < 0.5
        finally:
            release.set()
            link.stop(timeout_s=5)
