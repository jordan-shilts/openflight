"""The Supabase uploader against ``scripts/supabase/schema.sql`` in a real PostgreSQL.

A throwaway PostgreSQL 16 cluster plays the project. A small stand-in for
PostgREST turns each request into the SQL PostgREST runs for it: as role
``anon``, with the request headers in ``request.headers``, inserting the
payload's columns from ``json_populate_recordset`` with ``ON CONFLICT (id) DO
UPDATE``. Row-level security, the device token check and the column types are
then the schema's own.

Skipped unless PostgreSQL's server binaries are installed. Run as root, the
cluster runs as the ``postgres`` user through ``runuser``.
"""

import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from supabase_fakes import PROJECT_URL, PUBLISHABLE_KEY, SessionFile, shot_entry

from openflight.cloud.client import HttpResponse
from openflight.cloud.supabase import commands, push as push_module
from openflight.cloud.supabase.client import SupabaseClient
from openflight.cloud.supabase.config import SupabaseConfig, generate_device_token, token_sha256
from openflight.cloud.supabase.push import STOP_UNAUTHORIZED

SCHEMA = Path(__file__).resolve().parents[1] / "scripts" / "supabase" / "schema.sql"
PG_BIN_CANDIDATES = [Path("/usr/lib/postgresql/16/bin")]


def _pg_bin():
    for candidate in PG_BIN_CANDIDATES:
        if (candidate / "initdb").exists() and (candidate / "pg_ctl").exists():
            return candidate
    initdb = shutil.which("initdb")
    return Path(initdb).parent if initdb else None


def _run_as():
    """Command prefix to run PostgreSQL as an unprivileged user, or None if impossible."""
    if os.geteuid() != 0:
        return []
    try:
        pwd.getpwnam("postgres")
    except KeyError:
        return None
    runuser = shutil.which("runuser")
    return [runuser, "-u", "postgres", "--"] if runuser else None


class PostgresCluster:
    def __init__(self, bin_dir: Path, prefix):
        self.bin_dir = bin_dir
        self.prefix = prefix
        self.root = Path(tempfile.mkdtemp(prefix="openflight-pg-"))
        if prefix:
            entry = pwd.getpwnam("postgres")
            os.chown(self.root, entry.pw_uid, entry.pw_gid)
        self.data = self.root / "data"

    def _cmd(self, *args, **kwargs):
        return subprocess.run(
            [*self.prefix, *args], capture_output=True, text=True, check=False, **kwargs
        )

    def start(self):
        result = self._cmd(
            str(self.bin_dir / "initdb"), "-D", str(self.data), "-A", "trust", "-U", "postgres"
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        options = f"-k {self.root} -c listen_addresses='' -c fsync=off"
        result = self._cmd(
            str(self.bin_dir / "pg_ctl"),
            "-D",
            str(self.data),
            "-o",
            options,
            "-w",
            "-l",
            str(self.root / "log"),
            "start",
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr + result.stdout)

    def stop(self):
        self._cmd(str(self.bin_dir / "pg_ctl"), "-D", str(self.data), "-m", "immediate", "stop")
        shutil.rmtree(self.root, ignore_errors=True)

    def psql(self, sql: str):
        """Run ``sql``; returns (ok, stdout, stderr). Errors carry their SQLSTATE."""
        result = self._cmd(
            str(self.bin_dir / "psql"),
            "-h",
            str(self.root),
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-X",
            "-q",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            "VERBOSITY=verbose",
            input=sql,
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr

    def query(self, sql: str) -> str:
        ok, out, err = self.psql(sql)
        assert ok, err
        return out


class PostgrestStandIn:
    """Answers the client's two request shapes the way PostgREST would."""

    def __init__(self, cluster: PostgresCluster, apikey: str):
        self.cluster = cluster
        self.apikey = apikey

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        headers = {name.lower(): value for name, value in (headers or {}).items()}
        if headers.get("apikey") != self.apikey:
            return HttpResponse(401, {}, json.dumps({"message": "Invalid API key"}).encode())
        tag = "q" + secrets.token_hex(8)
        request_headers = json.dumps(headers).replace("'", "''")
        prelude = (
            "begin; set local role anon; "
            f"select set_config('request.headers', '{request_headers}', true) \\g /dev/null\n"
        )
        path = url.split("/rest/v1/", 1)[1]
        if path.startswith("rpc/"):
            sql = f"{prelude}select to_json(public.{path[4:]}()); commit;"
        else:
            table = path.split("?", 1)[0]
            rows = json.loads(data.decode("utf-8"))
            columns = ", ".join(rows[0])
            updates = ", ".join(f"{col} = excluded.{col}" for col in rows[0])
            sql = (
                f"{prelude}insert into public.{table} ({columns}) "
                f"select {columns} from json_populate_recordset(null::public.{table}, "
                f"${tag}${data.decode('utf-8')}${tag}$) "
                f"on conflict (id) do update set {updates}; commit;"
            )
        ok, out, err = self.cluster.psql(sql)
        if ok:
            return HttpResponse(200 if path.startswith("rpc/") else 201, {}, out.encode())
        code = re.search(r"ERROR:\s+([0-9A-Z]{5}):\s*(.*)", err)
        sqlstate, message = (code.group(1), code.group(2)) if code else ("XX000", err)
        # PostgREST answers an anonymous request's insufficient_privilege with 401.
        status = 401 if sqlstate == "42501" else 400
        body = json.dumps({"code": sqlstate, "message": message}).encode()
        return HttpResponse(status, {}, body)


@pytest.fixture(scope="module")
def cluster():
    bin_dir = _pg_bin()
    if bin_dir is None:
        pytest.skip("PostgreSQL server binaries are not installed")
    prefix = _run_as()
    if prefix is None:
        pytest.skip("running as root without a postgres user to run PostgreSQL as")
    pg = PostgresCluster(bin_dir, prefix)
    try:
        pg.start()
    except (RuntimeError, OSError) as exc:
        pg.stop()
        pytest.skip(f"could not start PostgreSQL: {exc}")
    pg.query("create role anon nologin; create role authenticated nologin;")
    pg.query(SCHEMA.read_text())
    yield pg
    pg.stop()


def _config(token):
    return SupabaseConfig(PROJECT_URL, PUBLISHABLE_KEY, token, "garage pi", True)


def _client(cluster, config):
    return SupabaseClient(
        config.supabase_url,
        config.supabase_key,
        config.supabase_device_token,
        request_fn=PostgrestStandIn(cluster, PUBLISHABLE_KEY),
    )


def _push(cluster, config, log_dir, **kwargs):
    out = []
    summary = push_module.push(config, log_dir, _client(cluster, config), out=out.append, **kwargs)
    return summary, out


def test_end_to_end_against_schema_sql(cluster, tmp_path):
    token = generate_device_token()
    config = _config(token)
    session = SessionFile(tmp_path / "session_20261114_090000_range.jsonl")
    session.start()
    session.raw_capture()
    session.shot(1, carry_spin_adjusted=162.5, club_angle_deg=-3.0, spin_rpm=float("nan"))
    session.shot(2)

    # Not registered yet: row-level security refuses, and the run pauses.
    summary, _ = _push(cluster, config, tmp_path)
    assert summary.stopped == STOP_UNAUTHORIZED
    assert not commands.cmd_test(config, tmp_path, _client(cluster, config), out=lambda _: None)

    # The owner runs the exact line setup prints.
    cluster.query(commands.registration_sql("garage pi", token_sha256(token)))
    assert commands.cmd_test(config, tmp_path, _client(cluster, config), out=lambda _: None)

    summary, _ = _push(cluster, config, tmp_path, respect_pause=True)
    assert summary.stopped is None
    assert summary.shots_sent == 2

    device = cluster.query(
        f"select id from golf_devices where token_sha256 = '{token_sha256(token)}'"
    )
    assert cluster.query("select count(*) from golf_shots") == "2"
    assert cluster.query("select distinct device_id from golf_shots") == device
    assert cluster.query("select device_id from golf_sessions") == device
    assert (
        cluster.query(
            "select carry_yards, attack_angle_deg, spin_rpm is null, club from golf_shots "
            "where shot_number = 1"
        )
        == "162.5|-3|t|7-iron"
    )
    assert cluster.query("select data->>'type' from golf_shots limit 1") == "shot_detected"
    assert (
        cluster.query("select count(*) from golf_shots where data::text like '%i_samples%'") == "0"
    )

    # Appended shots and the session end go up; resending changes nothing.
    session.shot(3)
    session.end()
    _push(cluster, config, tmp_path)
    _push(cluster, config, tmp_path, resend=True)
    assert cluster.query("select count(*) from golf_shots") == "3"
    assert cluster.query("select count(*) from golf_sessions") == "1"
    assert (
        cluster.query(
            "select ended_at is not null, info->'session_end'->'stats' is not null from golf_sessions"
        )
        == "t|t"
    )


def test_another_device_cannot_overwrite_these_rows(cluster, tmp_path):
    owner, intruder = generate_device_token(), generate_device_token()
    for name, token in (("owner", owner), ("intruder", intruder)):
        cluster.query(commands.registration_sql(name, token_sha256(token)))
    session = SessionFile(tmp_path / "session_20261114_120000_range.jsonl")
    session.start(session_uuid="9a1c3d5e-7f80-4a2b-9c3d-5e7f80a2b4c6")
    session.shot(1, ball_speed_mph=150.0)
    _push(cluster, _config(owner), tmp_path)

    intruder_dir = tmp_path / "intruder"
    intruder_dir.mkdir()
    copy = SessionFile(intruder_dir / session.path.name)
    copy.start(session_uuid="9a1c3d5e-7f80-4a2b-9c3d-5e7f80a2b4c6")
    copy.shot(1, ball_speed_mph=10.0)
    summary, _ = _push(cluster, _config(intruder), intruder_dir)

    assert summary.stopped == STOP_UNAUTHORIZED
    assert (
        cluster.query(
            "select ball_speed_mph from golf_shots s join golf_sessions g on g.id = s.session_id "
            "where g.id = '9a1c3d5e-7f80-4a2b-9c3d-5e7f80a2b4c6'"
        )
        == "150"
    )


def test_a_shot_with_every_field_fits_the_columns(cluster, tmp_path):
    token = generate_device_token()
    cluster.query(commands.registration_sql("full", token_sha256(token)))
    session = SessionFile(tmp_path / "session_20261114_130000_range.jsonl")
    session.start(session_uuid="0b2d4f60-8a1c-4e3f-a5b7-c9d1e3f5a7b9")
    entry = shot_entry(
        4,
        launch_angle_vertical=12.5,
        launch_angle_horizontal=-2.25,
        spin_axis_deg=4.0,
        face_angle_deg=1.5,
        dynamic_loft_deg=18.0,
        club_path_deg=-1.0,
        smash_factor=1.45,
        profile_name="Jo's",
    )
    session.write(entry)
    summary, out = _push(cluster, _config(token), tmp_path)
    assert summary.stopped is None, out
    assert (
        cluster.query(
            "select launch_v_deg, launch_h_deg, spin_axis_deg, face_angle_deg, dynamic_loft_deg, "
            "club_path_deg, smash_factor, profile_name, shot_at is not null from golf_shots "
            "where shot_number = 4"
        )
        == "12.5|-2.25|4|1.5|18|-1|1.45|Jo's|t"
    )
