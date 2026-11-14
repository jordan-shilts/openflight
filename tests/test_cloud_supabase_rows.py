"""Tests for mapping session-log entries to golf_sessions / golf_shots rows."""

import uuid
from datetime import datetime

import pytest
from supabase_fakes import SESSION_UUID, shot_entry

from openflight.cloud import filtering
from openflight.cloud.supabase import rows

SHOT_COLUMNS = {
    "id",
    "session_id",
    "shot_number",
    "shot_at",
    "club",
    "profile_name",
    "ball_speed_mph",
    "club_speed_mph",
    "smash_factor",
    "launch_v_deg",
    "launch_h_deg",
    "spin_rpm",
    "spin_axis_deg",
    "carry_yards",
    "face_angle_deg",
    "club_path_deg",
    "attack_angle_deg",
    "dynamic_loft_deg",
    "data",
}


def _local(naive: str) -> str:
    return datetime.fromisoformat(naive).astimezone().isoformat()


class TestSessionId:
    def test_uses_embedded_session_uuid_like_flightweb(self):
        assert rows.session_id(SESSION_UUID.upper(), "session_x.jsonl") == SESSION_UUID

    def test_fallback_is_flightweb_derivation_without_device_id(self):
        expected = filtering.session_id_for(None, "", "session_20250101_120000_range.jsonl")
        assert rows.session_id(None, "session_20250101_120000_range.jsonl") == expected
        assert str(uuid.UUID(expected)) == expected

    def test_fallback_is_stable_per_file(self):
        assert rows.session_id(None, "a.jsonl") == rows.session_id(None, "a.jsonl")
        assert rows.session_id(None, "a.jsonl") != rows.session_id(None, "b.jsonl")

    def test_corrupt_embedded_uuid_uses_fallback(self):
        assert rows.session_id("not-a-uuid", "a.jsonl") == rows.session_id(None, "a.jsonl")


class TestShotId:
    def test_deterministic_from_session_and_shot_number(self):
        first = rows.shot_id(SESSION_UUID, 3, line_offset=100)
        assert first == rows.shot_id(SESSION_UUID, 3, line_offset=999)
        assert first == str(uuid.uuid5(uuid.UUID(SESSION_UUID), "shot:3"))

    def test_differs_by_shot_and_session(self):
        other_session = str(uuid.uuid4())
        assert rows.shot_id(SESSION_UUID, 3, 0) != rows.shot_id(SESSION_UUID, 4, 0)
        assert rows.shot_id(SESSION_UUID, 3, 0) != rows.shot_id(other_session, 3, 0)

    def test_unnumbered_shot_uses_its_line_offset(self):
        assert rows.shot_id(SESSION_UUID, None, 10) == rows.shot_id(SESSION_UUID, None, 10)
        assert rows.shot_id(SESSION_UUID, None, 10) != rows.shot_id(SESSION_UUID, None, 11)


class TestTimestamps:
    def test_naive_local_time_gets_the_pi_offset(self):
        value = rows.to_timestamptz("2026-11-14T09:30:00.123456")
        assert value == _local("2026-11-14T09:30:00.123456")
        assert datetime.fromisoformat(value).tzinfo is not None

    def test_aware_time_is_kept(self):
        assert rows.to_timestamptz("2026-11-14T09:30:00+02:00") == "2026-11-14T09:30:00+02:00"

    @pytest.mark.parametrize("value", [None, "", "yesterday", 1731577800, "2026-13-40T99:00:00"])
    def test_unusable_values_are_null(self, value):
        assert rows.to_timestamptz(value) is None


class TestShotRow:
    def test_has_exactly_the_schema_columns_and_no_device_id(self):
        row = rows.shot_row(shot_entry(1), SESSION_UUID, 0)
        assert set(row) == SHOT_COLUMNS

    def test_maps_shot_to_dict_keys(self):
        entry = shot_entry(
            7,
            ball_speed_mph=131.5,
            club_speed_mph=92.25,
            smash_factor=1.43,
            launch_angle_vertical=14.2,
            launch_angle_horizontal=-1.5,
            spin_rpm=5200.0,
            spin_axis_deg=-3.5,
            carry_spin_adjusted=168.4,
            estimated_carry_yards=150.0,
            face_angle_deg=0.8,
            dynamic_loft_deg=22.1,
            profile_name="Jordan",
        )
        row = rows.shot_row(entry, SESSION_UUID, 0)
        assert row["shot_number"] == 7
        assert row["session_id"] == SESSION_UUID
        assert row["club"] == "7-iron"
        assert row["profile_name"] == "Jordan"
        assert row["ball_speed_mph"] == 131.5
        assert row["club_speed_mph"] == 92.25
        assert row["smash_factor"] == 1.43
        assert row["launch_v_deg"] == 14.2
        assert row["launch_h_deg"] == -1.5
        assert row["spin_rpm"] == 5200.0
        assert row["spin_axis_deg"] == -3.5
        assert row["carry_yards"] == 168.4
        assert row["face_angle_deg"] == 0.8
        assert row["dynamic_loft_deg"] == 22.1
        assert row["shot_at"] == _local("2026-11-14T09:30:00")

    def test_data_is_the_whole_entry(self):
        entry = shot_entry(2, spin_candidates=[{"rpm": 3000, "snr": 4.1}])
        assert rows.shot_row(entry, SESSION_UUID, 0)["data"] == entry

    def test_missing_values_are_null(self):
        row = rows.shot_row({"type": "shot_detected"}, SESSION_UUID, 42)
        for column in SHOT_COLUMNS - {"id", "session_id", "data"}:
            assert row[column] is None, column
        assert row["id"] == rows.shot_id(SESSION_UUID, None, 42)

    def test_non_numbers_booleans_and_non_finite_are_null(self):
        entry = shot_entry(
            1,
            spin_rpm=float("nan"),
            carry_spin_adjusted=float("inf"),
            smash_factor=True,
            launch_angle_vertical="12.0",
        )
        row = rows.shot_row(entry, SESSION_UUID, 0)
        assert row["spin_rpm"] is None
        assert row["carry_yards"] is None
        assert row["smash_factor"] is None
        assert row["launch_v_deg"] is None

    def test_data_has_no_nan_or_infinity(self):
        entry = shot_entry(1, spin_snr=float("nan"), spin_candidates=[{"snr": float("-inf")}])
        data = rows.shot_row(entry, SESSION_UUID, 0)["data"]
        assert data["spin_snr"] is None
        assert data["spin_candidates"] == [{"snr": None}]

    def test_boolean_shot_number_is_not_a_number(self):
        entry = dict(shot_entry(None), shot_number=True)
        row = rows.shot_row(entry, SESSION_UUID, 5)
        assert row["shot_number"] is None
        assert row["id"] == rows.shot_id(SESSION_UUID, None, 5)

    def test_carry_is_the_committed_carry_not_the_table_estimate(self):
        entry = shot_entry(1, carry_spin_adjusted=None, estimated_carry_yards=150.0)
        assert rows.shot_row(entry, SESSION_UUID, 0)["carry_yards"] is None


class TestClubDelivery:
    def test_attack_angle_prefers_the_measured_club_angle(self):
        entry = shot_entry(
            1,
            club_angle_deg=-4.5,
            experimental_fused_attack_angle_deg=-2.0,
            experimental_fused_attack_angle_confidence="high",
        )
        assert rows.shot_row(entry, SESSION_UUID, 0)["attack_angle_deg"] == -4.5

    @pytest.mark.parametrize("tier", ["high", "medium"])
    def test_attack_angle_falls_back_to_trusted_camera_tiers(self, tier):
        entry = shot_entry(
            1,
            club_angle_deg=None,
            experimental_fused_attack_angle_deg=-2.0,
            experimental_fused_attack_angle_confidence=tier,
        )
        assert rows.shot_row(entry, SESSION_UUID, 0)["attack_angle_deg"] == -2.0

    @pytest.mark.parametrize("tier", ["low", "withheld", None])
    def test_untrusted_camera_attack_angle_is_null(self, tier):
        entry = shot_entry(
            1,
            club_angle_deg=None,
            experimental_fused_attack_angle_deg=-2.0,
            experimental_fused_attack_angle_confidence=tier,
        )
        assert rows.shot_row(entry, SESSION_UUID, 0)["attack_angle_deg"] is None

    def test_club_path_uses_the_same_trusted_rule(self):
        measured = shot_entry(1, club_path_deg=2.5, experimental_fused_club_path_deg=9.0)
        camera = shot_entry(
            1,
            club_path_deg=None,
            experimental_fused_club_path_deg=-1.5,
            experimental_fused_club_path_confidence="medium",
        )
        untrusted = shot_entry(
            1,
            club_path_deg=None,
            experimental_fused_club_path_deg=-1.5,
            experimental_fused_club_path_confidence="low",
        )
        assert rows.shot_row(measured, SESSION_UUID, 0)["club_path_deg"] == 2.5
        assert rows.shot_row(camera, SESSION_UUID, 0)["club_path_deg"] == -1.5
        assert rows.shot_row(untrusted, SESSION_UUID, 0)["club_path_deg"] is None

    def test_garbage_delivery_values_do_not_raise(self):
        entry = shot_entry(1, club_angle_deg="steep", club_path_deg=float("nan"))
        row = rows.shot_row(entry, SESSION_UUID, 0)
        assert row["attack_angle_deg"] is None
        assert row["club_path_deg"] is None


class TestSessionRow:
    def test_from_start_and_end_entries(self):
        start = {"type": "session_start", "start_time": "2026-11-14T09:00:00", "mode": "mock"}
        end = {"type": "session_end", "end_time": "2026-11-14T10:00:00", "stats": {"errors": 0}}
        row = rows.session_row(SESSION_UUID, "session_x.jsonl", start, end)
        assert set(row) == {"id", "started_at", "ended_at", "info"}
        assert row["id"] == SESSION_UUID
        assert row["started_at"] == _local("2026-11-14T09:00:00")
        assert row["ended_at"] == _local("2026-11-14T10:00:00")
        assert row["info"] == {
            "file": "session_x.jsonl",
            "session_start": start,
            "session_end": end,
        }

    def test_open_session_has_no_end(self):
        start = {"type": "session_start", "start_time": "2026-11-14T09:00:00"}
        row = rows.session_row(SESSION_UUID, "f.jsonl", start, None)
        assert row["ended_at"] is None
        assert row["info"]["session_end"] is None

    def test_falls_back_to_entry_ts(self):
        start = {"type": "session_start", "ts": "2026-11-14T09:00:01"}
        assert rows.session_row(SESSION_UUID, "f", start, None)["started_at"] == _local(
            "2026-11-14T09:00:01"
        )
