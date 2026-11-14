"""Test doubles for the Supabase uploader: a fake PostgREST and a session writer."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

from openflight.cloud.client import CloudNetworkError, HttpResponse
from openflight.cloud.supabase.config import SupabaseConfig
from openflight.clubs import ClubType
from openflight.launch_monitor import Shot

PROJECT_URL = "https://abcdefghijklmnopqrst.supabase.co"
PUBLISHABLE_KEY = "sb_publishable_TestKey_123-abc"
DEVICE_TOKEN = "a" * 64
SESSION_UUID = "3f6c2a8e-9b1d-4c47-8e2a-5d7f0b6c1a24"


def make_config(**overrides) -> SupabaseConfig:
    values = dict(
        supabase_url=PROJECT_URL,
        supabase_key=PUBLISHABLE_KEY,
        supabase_device_token=DEVICE_TOKEN,
        supabase_device_name="garage pi",
        enabled=True,
    )
    values.update(overrides)
    return SupabaseConfig(**values)


@dataclass
class Request:
    method: str
    url: str
    headers: Dict[str, str]
    body: Any
    timeout: Optional[float] = None

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    @property
    def table(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def query(self) -> Dict[str, List[str]]:
        return parse_qs(urlsplit(self.url).query)


@dataclass
class FakeSupabase:
    """A stand-in for ``urllib_request`` that behaves like PostgREST upserts.

    Rows are stored by id, so a repeated upsert overwrites rather than adds.
    ``script`` holds responses (or exceptions) to return, in order, before the
    default behaviour resumes. ``offline`` makes every request fail as if the
    Pi had no internet.
    """

    device_id: Optional[str] = "8d0c1f9e-0f57-4f0e-9d0e-6b3b8f1f2a10"
    requests: List[Request] = field(default_factory=list)
    tables: Dict[str, Dict[str, Dict[str, Any]]] = field(
        default_factory=lambda: {"golf_sessions": {}, "golf_shots": {}}
    )
    script: List[Any] = field(default_factory=list)
    offline: bool = False

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        body = json.loads(data.decode("utf-8")) if data else None
        request = Request(method, url, dict(headers or {}), body, timeout)
        self.requests.append(request)
        if self.offline:
            raise CloudNetworkError("Temporary failure in name resolution")
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if method == "GET":
            return HttpResponse(200, {}, b'{"swagger": "2.0"}')
        if "/rpc/" in request.path:
            return HttpResponse(200, {}, json.dumps(self.device_id).encode("utf-8"))
        for row in body:
            self.tables[request.table][row["id"]] = row
        return HttpResponse(201, {}, b"")

    def upserts(self, table: str) -> List[Request]:
        return [r for r in self.requests if r.method == "POST" and r.table == table]

    @property
    def probes(self) -> List[Request]:
        return [r for r in self.requests if r.method == "GET"]

    def sent_rows(self, table: str) -> List[Dict[str, Any]]:
        return [row for r in self.upserts(table) for row in r.body]

    @property
    def shots(self) -> Dict[str, Dict[str, Any]]:
        return self.tables["golf_shots"]

    @property
    def sessions(self) -> Dict[str, Dict[str, Any]]:
        return self.tables["golf_sessions"]


def error_response(status: int, code: str = "", message: str = "") -> HttpResponse:
    body = json.dumps({"code": code, "message": message}).encode("utf-8")
    return HttpResponse(status, {}, body)


def shot_entry(shot_number: Optional[int], **fields) -> Dict[str, Any]:
    """A ``shot_detected`` log entry built from a real ``Shot.to_dict()``."""
    shot = Shot(
        ball_speed_mph=fields.pop("ball_speed_mph", 120.0),
        timestamp=fields.pop("timestamp", datetime(2026, 11, 14, 9, 30, 0)),
        club=fields.pop("club", ClubType.IRON_7),
        club_speed_mph=fields.pop("club_speed_mph", 85.0),
        shot_number=shot_number,
    )
    data = shot.to_dict()
    data.update(fields)
    return {"ts": "2026-11-14T09:30:01", "type": "shot_detected", **data}


class SessionFile:
    """Appends session-log lines the way ``SessionLogger`` writes them."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def write(self, entry: Dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def write_partial(self, entry: Dict[str, Any]) -> None:
        """The first half of a line, as a reader sees it mid-write."""
        line = json.dumps(entry)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line[: len(line) // 2])

    def finish_partial(self, entry: Dict[str, Any]) -> None:
        line = json.dumps(entry)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line[len(line) // 2 :] + "\n")

    def start(self, session_uuid: Optional[str] = SESSION_UUID) -> None:
        entry = {
            "ts": "2026-11-14T09:00:00.000001",
            "type": "session_start",
            "session_id": "20261114_090000",
            "start_time": "2026-11-14T09:00:00",
            "mode": "rolling-buffer",
            "config": {"min_speed": 15},
        }
        if session_uuid is not None:
            entry["session_uuid"] = session_uuid
        self.write(entry)

    def shot(self, shot_number: Optional[int], **fields) -> None:
        self.write(shot_entry(shot_number, **fields))

    def raw_capture(self, shot_number: int = 1, samples: int = 4096) -> None:
        self.write(
            {
                "ts": "2026-11-14T09:30:00",
                "type": "rolling_buffer_capture",
                "shot_number": shot_number,
                "i_samples": [2048] * samples,
                "q_samples": [2047] * samples,
            }
        )

    def end(self) -> None:
        self.write(
            {
                "ts": "2026-11-14T10:00:00.5",
                "type": "session_end",
                "end_time": "2026-11-14T10:00:00",
                "stats": {"shots_detected": 2, "errors": 0},
            }
        )
