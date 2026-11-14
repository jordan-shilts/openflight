"""Tests for the Supabase REST client: headers, request shape, response handling."""

import json

import pytest
from supabase_fakes import DEVICE_TOKEN, PROJECT_URL, PUBLISHABLE_KEY, FakeSupabase, error_response

from openflight.cloud.client import CloudNetworkError, HttpResponse
from openflight.cloud.supabase.client import (
    PROBE_TIMEOUT_S,
    SHOTS_TABLE,
    UPSERT_PREFER,
    Outcome,
    SupabaseClient,
    classify,
)

LEGACY_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.c2ln"


def _client(fake, key=PUBLISHABLE_KEY):
    return SupabaseClient(PROJECT_URL, key, DEVICE_TOKEN, request_fn=fake)


class TestUpsertRequest:
    def test_posts_json_array_to_table_with_on_conflict_id(self):
        fake = FakeSupabase()
        rows = [{"id": "a", "n": 1}, {"id": "b", "n": 2}]
        _client(fake).upsert(SHOTS_TABLE, rows)
        request = fake.requests[0]
        assert request.method == "POST"
        assert request.url == f"{PROJECT_URL}/rest/v1/golf_shots?on_conflict=id"
        assert request.body == rows

    def test_headers_for_publishable_key(self):
        fake = FakeSupabase()
        _client(fake).upsert(SHOTS_TABLE, [{"id": "a"}])
        assert fake.requests[0].headers == {
            "apikey": PUBLISHABLE_KEY,
            "x-openflight-device-token": DEVICE_TOKEN,
            "Prefer": "resolution=merge-duplicates,return=minimal",
            "Content-Type": "application/json",
        }

    def test_publishable_key_is_never_a_bearer_token(self):
        fake = FakeSupabase()
        _client(fake).upsert(SHOTS_TABLE, [{"id": "a"}])
        assert not any(name.lower() == "authorization" for name in fake.requests[0].headers)

    def test_legacy_jwt_is_also_sent_as_bearer(self):
        fake = FakeSupabase()
        _client(fake, key=LEGACY_JWT).upsert(SHOTS_TABLE, [{"id": "a"}])
        headers = fake.requests[0].headers
        assert headers["Authorization"] == f"Bearer {LEGACY_JWT}"
        assert headers["apikey"] == LEGACY_JWT
        assert headers["Prefer"] == UPSERT_PREFER

    def test_trailing_slash_on_url_is_ignored(self):
        fake = FakeSupabase()
        SupabaseClient(PROJECT_URL + "/", PUBLISHABLE_KEY, DEVICE_TOKEN, request_fn=fake).upsert(
            SHOTS_TABLE, [{"id": "a"}]
        )
        assert fake.requests[0].url.startswith(f"{PROJECT_URL}/rest/v1/")

    def test_refuses_to_send_nan(self):
        with pytest.raises(ValueError):
            _client(FakeSupabase()).upsert(SHOTS_TABLE, [{"id": "a", "x": float("nan")}])


class TestClassify:
    @pytest.mark.parametrize("status", [200, 201, 204])
    def test_success_with_empty_body(self, status):
        assert classify(HttpResponse(status, {}, b"")).outcome is Outcome.OK

    def test_success_status_with_html_is_not_trusted(self):
        # A captive portal answering in place of Supabase stored nothing.
        reply = classify(HttpResponse(200, {}, b"<html>Sign in to Wi-Fi</html>"))
        assert reply.outcome is Outcome.RETRY

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_statuses_are_unauthorized(self, status):
        reply = classify(error_response(status, "", "Invalid API key"))
        assert reply.outcome is Outcome.UNAUTHORIZED
        assert "Invalid API key" in reply.message

    def test_rls_violation_is_unauthorized_whatever_the_status(self):
        reply = classify(
            error_response(
                400, "42501", 'new row violates row-level security policy for table "golf_shots"'
            )
        )
        assert reply.outcome is Outcome.UNAUTHORIZED
        assert "42501" in reply.message

    @pytest.mark.parametrize("code", ["PGRST205", "PGRST202", "42P01"])
    def test_missing_schema(self, code):
        assert classify(error_response(404, code, "not found")).outcome is Outcome.SCHEMA_MISSING

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_transient_statuses_retry(self, status):
        assert classify(error_response(status)).outcome is Outcome.RETRY

    @pytest.mark.parametrize("status", [400, 409, 413, 422])
    def test_other_client_errors_are_rejections(self, status):
        reply = classify(error_response(status, "22P02", "invalid input syntax"))
        assert reply.outcome is Outcome.REJECTED
        assert reply.status == status

    def test_non_json_error_body(self):
        reply = classify(HttpResponse(502, {}, b"<html>Bad gateway</html>"))
        assert reply.outcome is Outcome.RETRY
        assert reply.message.startswith("502")

    def test_message_is_bounded(self):
        reply = classify(error_response(400, "X", "y" * 5000))
        assert len(reply.message) <= 300


class TestNetwork:
    def test_unreachable_server_is_retry_without_status(self):
        fake = FakeSupabase(script=[CloudNetworkError("Name or service not known")])
        reply = _client(fake).upsert(SHOTS_TABLE, [{"id": "a"}])
        assert reply.outcome is Outcome.RETRY
        assert reply.status is None
        assert "Name or service not known" in reply.message

    def test_token_never_appears_in_messages(self):
        fake = FakeSupabase(script=[CloudNetworkError("boom")])
        reply = _client(fake).upsert(SHOTS_TABLE, [{"id": "a"}])
        assert DEVICE_TOKEN not in reply.message


class TestRegisteredDevice:
    def test_calls_the_device_function_and_writes_nothing(self):
        fake = FakeSupabase()
        reply = _client(fake).registered_device()
        request = fake.requests[0]
        assert request.url == f"{PROJECT_URL}/rest/v1/rpc/golf_request_device"
        assert request.body == {}
        assert "Prefer" not in request.headers
        assert request.headers["x-openflight-device-token"] == DEVICE_TOKEN
        assert reply.outcome is Outcome.OK
        assert reply.body == fake.device_id
        assert fake.shots == {} and fake.sessions == {}

    def test_unregistered_token_is_ok_with_no_device(self):
        reply = _client(FakeSupabase(device_id=None)).registered_device()
        assert reply.outcome is Outcome.OK
        assert reply.body is None

    def test_unexpected_json_is_not_a_device(self):
        fake = FakeSupabase(script=[HttpResponse(200, {}, json.dumps({"x": 1}).encode())])
        assert _client(fake).registered_device().outcome is Outcome.RETRY


class TestReachable:
    def test_one_short_get_to_the_rest_root_with_the_key(self):
        fake = FakeSupabase()
        assert _client(fake).reachable() is True
        request = fake.requests[0]
        assert (request.method, request.url) == ("GET", f"{PROJECT_URL}/rest/v1/")
        assert request.headers["apikey"] == PUBLISHABLE_KEY
        assert request.body is None
        assert request.timeout == PROBE_TIMEOUT_S

    @pytest.mark.parametrize("status", [200, 401, 403, 404])
    def test_any_answer_below_500_is_reachable(self, status):
        assert _client(FakeSupabase(script=[error_response(status)])).reachable() is True

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_a_5xx_is_not(self, status):
        assert _client(FakeSupabase(script=[error_response(status)])).reachable() is False

    def test_no_answer_is_not(self):
        assert _client(FakeSupabase(offline=True)).reachable() is False
