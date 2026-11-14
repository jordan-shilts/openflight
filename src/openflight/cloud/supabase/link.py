"""Whether this Pi can reach its Supabase project right now, for the server.

The server keeps one ``SupabaseLink``. A logged shot calls ``notify_shot()``,
which only records it and wakes a daemon thread; that thread does the rest, off
the shot path:

- ``online``: the last push or probe got an answer. Each shot starts a
  detached background push (``openflight-cloud supabase push --background``),
  at most one every ``push_interval_s`` and one at a time. No polling.
- ``offline``: a push or probe got no answer (DNS, connect, timeout, e.g. the
  Pi on its own hotspot) or Supabase answered 5xx. No pushes; one GET probe
  every ``probe_interval_s``. When it answers, one push sends the backlog
  (oldest first, in batches) and per-shot pushes resume.
- ``needs_attention``: the project refused this Pi (wrong key, unregistered
  token, no tables). No requests while the pause ``push`` recorded is in
  force; ``supabase test`` or a new config lifts it.
- ``not_set_up``: no Supabase config, uploads off, or session logging off. No
  requests at all.

Shots live in the session logs on disk until Supabase accepts them, so a state
change never loses one. Pushes record what they learned in the log dir's state
file (``push.load_state``); the thread reads results back from there,
including those of the systemd timer's pushes.
"""

import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .. import trigger
from . import push
from .client import SupabaseClient
from .config import SupabaseConfig

ONLINE = push.LINK_ONLINE
OFFLINE = push.LINK_OFFLINE
NEEDS_ATTENTION = push.LINK_NEEDS_ATTENTION
NOT_SET_UP = "not_set_up"

PROBE_INTERVAL_S = 30.0
PUSH_INTERVAL_S = 10.0
# How often the thread re-reads the config, the pause and the state file when
# nothing wakes it. Local files only: no network.
REFRESH_INTERVAL_S = 30.0
# How often a wait for a running push checks for shutdown.
PUSH_POLL_S = 0.2

# Returns the active config and log dir, or (None, None, why not).
Resolver = Callable[[], Tuple[Optional[SupabaseConfig], Optional[Path], str]]
PushRunner = Callable[[SupabaseConfig, Path, threading.Event], None]


@dataclass(frozen=True)
class LinkStatus:
    """What the server reports (``GET /api/cloud/status``, ``cloud_status``)."""

    state: str
    # Allowlisted shots on disk not yet in Supabase; None when unknown or not set up.
    pending: Optional[int]
    message: str = ""
    # For needs_attention: "unauthorized" (wrong key or unregistered device) or
    # "schema_missing" (schema.sql not run). Empty otherwise.
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe form."""
        return asdict(self)


@dataclass(frozen=True)
class LinkTiming:
    """Intervals and clocks, swapped in tests."""

    probe_interval_s: float = PROBE_INTERVAL_S
    push_interval_s: float = PUSH_INTERVAL_S
    refresh_interval_s: float = REFRESH_INTERVAL_S
    clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], float] = time.time


def run_detached_push(config: SupabaseConfig, log_dir: Path, stop: threading.Event) -> None:
    """Start a detached background push and wait for it, or for ``stop``."""
    proc = trigger.start_supabase_push(config, log_dir)
    if proc is None:
        return
    while proc.poll() is None:
        if stop.wait(PUSH_POLL_S):
            return  # shutting down: the push finishes on its own


class SupabaseLink:
    """Tracks reachability and pending shots; starts pushes and probes."""

    def __init__(
        self,
        resolve: Resolver,
        *,
        run_push: PushRunner = run_detached_push,
        client_factory: Optional[Callable[[SupabaseConfig], SupabaseClient]] = None,
        on_change: Optional[Callable[[LinkStatus], None]] = None,
        timing: LinkTiming = LinkTiming(),
    ):
        self._resolve = resolve
        self._run_push = run_push
        self._client_factory = client_factory or _default_client
        self._on_change = on_change
        self._timing = timing
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._status = LinkStatus(NOT_SET_UP, None)
        self._push_wanted = True  # catch up on anything left from before
        self._next_probe_at = 0.0
        self._last_push_at: Optional[float] = None
        self._recorded_at = 0.0  # state-file ``link`` entries up to here are applied
        self._count: Optional[int] = None  # shots pending at the last count of the disk
        self._notified = 0  # shots logged since start
        self._notified_at_count = 0  # ...as of that count
        self._reported: Optional[LinkStatus] = None

    # Called from the server -------------------------------------------------

    def notify_shot(self) -> None:
        """A shot was logged. O(1); never blocks or raises."""
        with self._lock:
            self._notified += 1
            self._push_wanted = True
        self._wake.set()

    def status(self) -> LinkStatus:
        """The current state and pending count."""
        with self._lock:
            current = self._status
            return LinkStatus(
                current.state, self._pending_locked(), current.message, current.reason
            )

    def start(self) -> None:
        """Start the daemon thread (once)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="supabase-link", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        """Stop probing and pushing; a push already running finishes on its own."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout_s)

    # The thread --------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # a bad config or disk hiccup must not end the thread
            self._changed()  # a shot moves the pending count even when nothing else changes
            self._wake.wait(self._seconds_to_next_step())
            self._wake.clear()

    def _seconds_to_next_step(self) -> float:
        now = self._timing.clock()
        deadlines = [now + self._timing.refresh_interval_s]
        with self._lock:
            state = self._status.state
            if state == OFFLINE:
                deadlines.append(self._next_probe_at)
            elif state == ONLINE and self._push_wanted and self._last_push_at is not None:
                deadlines.append(self._last_push_at + self._timing.push_interval_s)
        return max(0.0, min(deadlines) - now)

    def step(self) -> None:
        """Decide and do the next thing. Called by the thread; tests call it directly."""
        config, log_dir, why_not = self._resolve()
        if config is None or log_dir is None:
            with self._lock:
                self._count = None
                self._push_wanted = True  # send everything once it is set up
            self._set(NOT_SET_UP, why_not)
            return
        now = self._timing.clock()
        if self._state() == NOT_SET_UP:
            # Just set up (or starting): probe first, then send the backlog.
            self._set(OFFLINE, "checking the connection")
            self._next_probe_at = now
        if self._count is None:
            self._recount(log_dir)
        self._adopt_recorded(log_dir)

        pause = push.active_pause(log_dir, config, self._timing.wall_clock())
        if pause is not None:
            self._set(
                NEEDS_ATTENTION, str(pause.get("message") or ""), str(pause.get("reason") or "")
            )
            return
        if self._state() == NEEDS_ATTENTION:
            self._want_push()  # the pause ended or was lifted: try again

        if self._state() == OFFLINE:
            if now < self._next_probe_at:
                return
            self._next_probe_at = now + self._timing.probe_interval_s
            if not self._client_factory(config).reachable():
                self._set(OFFLINE, f"no answer from {config.supabase_url}")
                return
            self._set(ONLINE, "back online")
            self._want_push()

        with self._lock:
            due = self._last_push_at is None or (
                now >= self._last_push_at + self._timing.push_interval_s
            )
            if not (self._push_wanted and due):
                return
            self._push_wanted = False
        self._last_push_at = now
        started = self._timing.wall_clock()
        self._run_push(config, log_dir, self._stop)
        self._adopt_recorded(log_dir, since=started)
        self._recount(log_dir)
        if self._state() == OFFLINE:
            self._next_probe_at = self._timing.clock() + self._timing.probe_interval_s

    # Helpers -------------------------------------------------------------------

    def _state(self) -> str:
        with self._lock:
            return self._status.state

    def _want_push(self) -> None:
        with self._lock:
            self._push_wanted = True

    def _pending_locked(self) -> Optional[int]:
        if self._count is None:
            return None
        return self._count + self._notified - self._notified_at_count

    def _recount(self, log_dir: Path) -> None:
        with self._lock:
            notified = self._notified
        # A shot logged while this reads may be counted twice until the next count.
        count = push.pending_shot_count(log_dir)
        with self._lock:
            self._count = count
            self._notified_at_count = notified
        self._changed()

    def _adopt_recorded(self, log_dir: Path, since: float = 0.0) -> None:
        """Take a push's result from the state file, if newer than what we know."""
        link = push.load_state(log_dir).get("link")
        if not isinstance(link, dict):
            return
        at = link.get("at")
        state = link.get("state")
        if not isinstance(at, (int, float)) or at < since or at <= self._recorded_at:
            return
        if state not in (ONLINE, OFFLINE, NEEDS_ATTENTION):
            return
        self._recorded_at = at
        reason = str(link.get("reason") or "") if state == NEEDS_ATTENTION else ""
        self._set(state, str(link.get("message") or ""), reason)

    def _set(self, state: str, message: str, reason: str = "") -> None:
        with self._lock:
            self._status = LinkStatus(state, None, message, reason)
        self._changed()

    def _changed(self) -> None:
        """Report the status if it differs from the last one reported."""
        current = self.status()
        with self._lock:
            if current == self._reported:
                return
            self._reported = current
        if self._on_change is not None:
            try:
                self._on_change(current)
            except Exception:  # pylint: disable=broad-exception-caught
                pass


def _default_client(config: SupabaseConfig) -> SupabaseClient:
    return SupabaseClient(config.supabase_url, config.supabase_key, config.supabase_device_token)
