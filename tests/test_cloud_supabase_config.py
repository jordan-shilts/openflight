"""Tests for the Supabase upload config: file permissions, URL/key checks, the token."""

import base64
import hashlib
import json
import stat

import pytest

from openflight.cloud.config import CloudConfig, load_config, save_config
from openflight.cloud.supabase import config as supabase_config
from openflight.cloud.supabase.config import (
    SupabaseConfig,
    check_publishable_key,
    config_path_for,
    generate_device_token,
    load_supabase_config,
    normalize_project_url,
    save_supabase_config,
    token_sha256,
    uses_bearer,
)


def _jwt(claims):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(claims)}.c2lnbmF0dXJl"


class TestPersistence:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "supabase.json"
        config = SupabaseConfig(
            supabase_url="https://abcdefghijklmnopqrst.supabase.co",
            supabase_key="sb_publishable_abc",
            supabase_device_token="t" * 64,
            supabase_device_name="garage pi",
            enabled=False,
        )
        save_supabase_config(config, path)
        assert load_supabase_config(path) == config

    def test_file_is_owner_only(self, tmp_path):
        path = tmp_path / "nested" / "supabase.json"
        save_supabase_config(SupabaseConfig(supabase_device_token="secret"), path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_tightens_an_existing_loose_file(self, tmp_path):
        path = tmp_path / "supabase.json"
        path.write_text("{}")
        path.chmod(0o644)
        save_supabase_config(SupabaseConfig(supabase_device_token="secret"), path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_absent_file_is_none(self, tmp_path):
        assert load_supabase_config(tmp_path / "missing.json") is None

    def test_default_path_is_read_at_call_time(self, tmp_path, monkeypatch):
        path = tmp_path / "elsewhere.json"
        save_supabase_config(SupabaseConfig(supabase_url="https://x.supabase.co"), path)
        monkeypatch.setattr(supabase_config, "SUPABASE_CONFIG_PATH", path)
        assert load_supabase_config().supabase_url == "https://x.supabase.co"

    def test_non_object_file_raises_value_error(self, tmp_path):
        path = tmp_path / "supabase.json"
        path.write_text("[1, 2]")
        with pytest.raises(ValueError):
            load_supabase_config(path)

    def test_only_json_true_enables(self, tmp_path):
        path = tmp_path / "supabase.json"
        path.write_text(json.dumps({"enabled": "false"}))
        assert load_supabase_config(path).enabled is False
        path.write_text(json.dumps({}))
        assert load_supabase_config(path).enabled is True

    def test_sits_beside_the_flightweb_config(self, tmp_path):
        assert config_path_for(tmp_path / "cloud.json") == tmp_path / "supabase.json"

    def test_flightweb_and_supabase_files_are_independent(self, tmp_path):
        cloud = tmp_path / "cloud.json"
        save_config(CloudConfig(device_token="fw", device_id="d"), cloud)
        save_supabase_config(SupabaseConfig(supabase_device_token="sb"), config_path_for(cloud))
        save_config(CloudConfig(device_token="fw2", device_id="d"), cloud)
        assert load_config(cloud).device_token == "fw2"
        assert load_supabase_config(config_path_for(cloud)).supabase_device_token == "sb"

    def test_repr_never_shows_the_token(self):
        config = SupabaseConfig(supabase_device_token="supersecrettoken")
        assert "supersecrettoken" not in repr(config)


class TestActive:
    def test_needs_url_key_and_token(self):
        full = SupabaseConfig("https://a.supabase.co", "sb_publishable_x", "t" * 64)
        assert full.is_active()
        assert not SupabaseConfig("https://a.supabase.co", "sb_publishable_x", "").is_active()
        assert not SupabaseConfig("", "sb_publishable_x", "t" * 64).is_active()

    def test_disabled_is_configured_but_inactive(self):
        config = SupabaseConfig("https://a.supabase.co", "sb_publishable_x", "t" * 64, "", False)
        assert config.is_configured()
        assert not config.is_active()


class TestProjectUrl:
    @pytest.mark.parametrize(
        "raw",
        [
            "https://abcdefghijklmnopqrst.supabase.co",
            "https://abcdefghijklmnopqrst.supabase.co/",
            "  HTTPS://ABCDEFGHIJKLMNOPQRST.supabase.co  ",
            "https://abcdefghijklmnopqrst.supabase.co/rest/v1/",
        ],
    )
    def test_accepts_and_normalizes(self, raw):
        assert normalize_project_url(raw) == "https://abcdefghijklmnopqrst.supabase.co"

    @pytest.mark.parametrize(
        "raw",
        [
            "http://abcdefghijklmnopqrst.supabase.co",
            "https://abcdefghijklmnopqrst.supabase.co.evil.example",
            "https://supabase.co",
            "https://abc.supabase.co/rest/v1/golf_shots",
            "abcdefghijklmnopqrst",
            "",
        ],
    )
    def test_rejects_other_shapes(self, raw):
        with pytest.raises(ValueError):
            normalize_project_url(raw)

    def test_dashboard_url_suggests_the_project_url(self):
        with pytest.raises(ValueError, match=r"https://abcdefghijklmnopqrst\.supabase\.co"):
            normalize_project_url("https://supabase.com/dashboard/project/abcdefghijklmnopqrst")


class TestKey:
    def test_accepts_publishable_key(self):
        assert check_publishable_key("  sb_publishable_AbC-123_x \n") == "sb_publishable_AbC-123_x"

    def test_refuses_secret_key(self):
        with pytest.raises(ValueError, match="secret key"):
            check_publishable_key("sb_secret_abc123")

    def test_accepts_legacy_anon_jwt(self):
        key = _jwt({"role": "anon", "iss": "supabase"})
        assert check_publishable_key(key) == key

    def test_refuses_legacy_service_role_jwt(self):
        with pytest.raises(ValueError, match="service_role"):
            check_publishable_key(_jwt({"role": "service_role"}))

    @pytest.mark.parametrize(
        "raw", ["", "hello", "sb_publishable_", "sb_publishable_a b", "eyJnot-a-jwt", "eyJ.x.y"]
    )
    def test_refuses_anything_else(self, raw):
        with pytest.raises(ValueError):
            check_publishable_key(raw)

    def test_bearer_only_for_legacy_jwt(self):
        assert uses_bearer(_jwt({"role": "anon"}))
        assert not uses_bearer("sb_publishable_abc")


class TestToken:
    def test_token_is_64_hex_and_random(self):
        first, second = generate_device_token(), generate_device_token()
        assert len(first) == 64
        int(first, 16)
        assert first != second

    def test_hash_matches_schema_sql(self):
        # schema.sql: encode(sha256(convert_to(token, 'UTF8')), 'hex')
        assert token_sha256("abc") == hashlib.sha256(b"abc").hexdigest()
