"""Saved rig setups: taped once for the simulator, camera-measured on the course."""

import json
import math

import pytest

from openflight.rig_setup import COURSE, SIMULATOR, RigSetupError, RigSetupStore


@pytest.fixture
def store(tmp_path):
    return RigSetupStore(tmp_path / "rig.json")


def taped(store, distance=1.4, width=15.0, swings=3, pitch=None):
    """A taped simulator that has seen enough swings to teach the lens scale."""
    store.update({"setup": SIMULATOR, "ball_distance_m": distance})
    for _ in range(swings):
        store.observe(diameter_px=width, pitch_deg=pitch)


def test_a_new_rig_starts_in_the_simulator_with_nothing_measured(store):
    geometry = store.geometry()

    assert geometry.setup_id == SIMULATOR
    assert geometry.ball_distance_m is None
    assert geometry.distance_source == "none"
    assert geometry.tee_range_m is None  # launch waits rather than guessing
    assert geometry.net_range_m == pytest.approx(4.6)


def test_the_course_has_no_net_and_measures_itself_by_default(store):
    store.update({"active": COURSE})

    assert store.geometry().net_range_m is None
    assert store.snapshot()["setups"][COURSE]["auto_distance"] is True


def test_a_taped_distance_becomes_the_slant_range_to_the_ball(store):
    store.update({"ball_distance_m": 1.4, "radar_height_m": 0.25})

    geometry = store.geometry()

    assert geometry.ball_distance_m == pytest.approx(1.4)
    assert geometry.distance_source == "manual"
    assert geometry.tee_range_m == pytest.approx(math.hypot(1.4, 0.25 - 0.040))


def test_three_simulator_swings_teach_the_camera(store):
    store.update({"ball_distance_m": 1.4})
    assert store.snapshot()["camera_ready"] is False
    assert store.snapshot()["scale_samples_needed"] == 3

    for _ in range(3):
        store.observe(diameter_px=15.0)

    assert store.snapshot()["camera_ready"] is True
    assert store.snapshot()["scale_samples_needed"] == 0


def test_the_course_distance_comes_from_how_big_the_ball_looks(store):
    taped(store, distance=1.4, width=15.0)
    store.update({"active": COURSE})

    geometry = store.observe(diameter_px=10.0)  # looks smaller, so it is further away

    assert geometry.ball_distance_m == pytest.approx(1.4 * 15.0 / 10.0)
    assert geometry.distance_source == "camera"


def test_a_course_swing_never_teaches_the_camera(store):
    taped(store, distance=1.4, width=15.0)
    store.update({"active": COURSE, "setup": COURSE, "ball_distance_m": 2.0})

    for _ in range(30):
        store.observe(diameter_px=40.0)

    store.update({"setup": COURSE, "auto_distance": True})
    assert store.observe(diameter_px=15.0).ball_distance_m == pytest.approx(1.4)


def test_the_simulator_does_not_learn_before_it_is_taped(store):
    for _ in range(5):
        store.observe(diameter_px=15.0)

    assert store.snapshot()["camera_ready"] is False


def test_correcting_the_tape_number_forgets_what_the_old_one_taught(store):
    taped(store, distance=1.4)

    store.update({"setup": SIMULATOR, "ball_distance_m": 1.5})

    assert store.snapshot()["camera_ready"] is False


def test_retyping_the_same_tape_number_keeps_the_scale(store):
    taped(store, distance=1.4)

    store.update({"setup": SIMULATOR, "ball_distance_m": 1.4})

    assert store.snapshot()["camera_ready"] is True


def test_auto_before_the_camera_is_taught_uses_the_typed_value(store):
    store.update({"active": COURSE, "setup": COURSE, "ball_distance_m": 1.7})
    store.update({"setup": COURSE, "auto_distance": True})

    geometry = store.observe(diameter_px=15.0)

    assert geometry.ball_distance_m == pytest.approx(1.7)
    assert geometry.distance_source == "manual"


def test_auto_with_nothing_to_go_on_leaves_the_distance_empty(store):
    store.update({"active": COURSE})

    assert store.observe(diameter_px=15.0).tee_range_m is None


def test_adjusting_by_hand_leaves_auto_and_auto_comes_back(store):
    taped(store, distance=1.4, width=15.0)
    store.update({"active": COURSE})
    store.observe(diameter_px=10.0)

    store.update({"setup": COURSE, "ball_distance_m": 2.2})
    assert store.geometry().ball_distance_m == pytest.approx(2.2)
    assert store.geometry().distance_source == "manual"

    store.update({"setup": COURSE, "auto_distance": True})
    assert store.geometry().distance_source == "camera"
    assert store.geometry().ball_distance_m == pytest.approx(2.1)


def test_a_swing_hit_while_adjusted_by_hand_is_ready_when_auto_is_tapped(store):
    taped(store, distance=1.4, width=15.0)
    store.update({"active": COURSE, "setup": COURSE, "ball_distance_m": 2.2})

    assert store.observe(diameter_px=10.0).ball_distance_m == pytest.approx(2.2)

    store.update({"setup": COURSE, "auto_distance": True})
    assert store.geometry().distance_source == "camera"
    assert store.geometry().ball_distance_m == pytest.approx(2.1)


def test_switching_location_forgets_where_the_last_ball_was(store):
    taped(store)
    store.update({"active": COURSE})
    store.observe(diameter_px=10.0)

    store.update({"active": SIMULATOR})
    store.update({"active": COURSE})

    assert store.geometry().distance_source == "none"


def test_a_camera_distance_that_makes_no_sense_keeps_the_last_good_one(store):
    taped(store, distance=1.4, width=15.0)
    store.update({"active": COURSE})
    store.observe(diameter_px=10.0)

    geometry = store.observe(diameter_px=59.0)  # would put the ball 36 cm away

    assert geometry.ball_distance_m == pytest.approx(2.1)


@pytest.mark.parametrize("junk", [None, float("nan"), float("inf"), True, "15", 5.9, 60.1])
def test_junk_readings_are_ignored(store, junk):
    store.update({"ball_distance_m": 1.4})

    store.observe(diameter_px=junk, pitch_deg=junk)

    assert store.snapshot()["scale_samples_needed"] == 3


def test_the_case_tilt_is_measured_against_the_simulator(store):
    taped(store, pitch=-3.0)
    assert store.geometry().tilt_offset_deg == 0.0

    store.update({"active": COURSE})
    geometry = store.observe(diameter_px=15.0, pitch_deg=1.5)

    assert geometry.tilt_offset_deg == pytest.approx(4.5)


def test_a_wild_tilt_offset_is_capped(store):
    taped(store, pitch=-30.0)
    store.update({"active": COURSE})

    assert store.observe(pitch_deg=30.0).tilt_offset_deg == pytest.approx(20.0)


def test_no_simulator_tilt_reference_means_no_correction(store):
    store.update({"active": COURSE})

    assert store.observe(pitch_deg=5.0).tilt_offset_deg == 0.0


def test_only_recent_swings_are_kept(tmp_path):
    store = RigSetupStore(tmp_path / "rig.json")
    store.update({"ball_distance_m": 1.0})

    for _ in range(30):
        store.observe(diameter_px=10.0)

    assert len(json.loads((tmp_path / "rig.json").read_text())["scale_samples"]) == 25


def test_everything_survives_a_restart(tmp_path):
    path = tmp_path / "nested" / "rig.json"
    first = RigSetupStore(path)
    taped(first, distance=1.4, width=15.0, pitch=-3.0)
    first.update(
        {
            "active": COURSE,
            "setup": SIMULATOR,
            "net_distance_m": 3.5,
            "net_enabled": False,
            "radar_height_m": 0.3,
        }
    )

    second = RigSetupStore(path)

    assert second.snapshot()["active"] == COURSE
    assert second.snapshot()["setups"][SIMULATOR]["ball_distance_m"] == pytest.approx(1.4)
    assert second.snapshot()["setups"][SIMULATOR]["net_distance_m"] == pytest.approx(3.5)
    assert second.snapshot()["setups"][SIMULATOR]["net_enabled"] is False
    assert second.snapshot()["radar_height_m"] == pytest.approx(0.3)
    assert second.snapshot()["camera_ready"] is True
    assert second.observe(diameter_px=10.0).ball_distance_m == pytest.approx(2.1)


@pytest.mark.parametrize("contents", ["not json", "[1, 2]", ""])
def test_a_broken_file_falls_back_to_defaults(tmp_path, contents):
    path = tmp_path / "rig.json"
    path.write_text(contents)

    assert RigSetupStore(path).geometry().distance_source == "none"


def test_bad_saved_fields_keep_their_defaults_and_good_ones_survive(tmp_path):
    path = tmp_path / "rig.json"
    path.write_text(
        json.dumps(
            {
                "active": "mars",
                "setups": {
                    SIMULATOR: {
                        "ball_distance_m": 99,
                        "net_enabled": "no",
                        "net_distance_m": 3.0,
                    },
                    COURSE: "nope",
                },
                "radar_height_m": -1,
                "scale_samples": [20.0, "x", None, 21.0],
            }
        )
    )

    snapshot = RigSetupStore(path).snapshot()

    assert snapshot["active"] == SIMULATOR
    assert snapshot["setups"][SIMULATOR]["ball_distance_m"] is None
    assert snapshot["setups"][SIMULATOR]["net_enabled"] is True
    assert snapshot["setups"][SIMULATOR]["net_distance_m"] == pytest.approx(3.0)
    assert snapshot["radar_height_m"] == pytest.approx(0.1524)
    assert snapshot["scale_samples_needed"] == 1


def test_a_failed_save_keeps_the_change_in_memory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    store = RigSetupStore(blocker / "rig.json")

    store.update({"ball_distance_m": 1.4})

    assert store.geometry().ball_distance_m == pytest.approx(1.4)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("nope", "JSON object"),
        ({"bogus": 1}, "unknown setting: bogus"),
        ({"active": "mars"}, "active must be one of"),
        ({"setup": "mars"}, "setup must be one of"),
        ({"ball_distance_m": "1.4"}, "ball_distance_m must be a number"),
        ({"ball_distance_m": True}, "ball_distance_m must be a number"),
        ({"ball_distance_m": float("nan")}, "ball_distance_m must be a number"),
        ({"ball_distance_m": 0.5}, "ball_distance_m must be between"),
        ({"ball_distance_m": 6.1}, "ball_distance_m must be between"),
        ({"net_distance_m": 0.9}, "net_distance_m must be between"),
        ({"radar_height_m": 2.0}, "radar_height_m must be between"),
        ({"auto_distance": "yes"}, "auto_distance must be true or false"),
        ({"net_enabled": 1}, "net_enabled must be true or false"),
    ],
)
def test_bad_changes_are_refused(store, payload, message):
    with pytest.raises(RigSetupError, match=message):
        store.update(payload)


def test_one_bad_value_applies_nothing(store):
    with pytest.raises(RigSetupError):
        store.update({"active": COURSE, "ball_distance_m": 1.4, "net_distance_m": 99})

    assert store.snapshot()["active"] == SIMULATOR
    assert store.snapshot()["setups"][COURSE]["ball_distance_m"] is None


def test_a_change_can_target_a_setup_that_is_not_active(store):
    store.update({"setup": COURSE, "net_enabled": True})

    assert store.snapshot()["active"] == SIMULATOR
    assert store.snapshot()["setups"][COURSE]["net_enabled"] is True


# -- the straight line (horizontal launch zero) ----------------------------


def shots(store, phases):
    for phase in phases:
        assert store.observe_horizontal_phase(phase) is True


def test_a_new_rig_has_no_straight_line(store):
    assert store.geometry().horizontal_phase_reference_rad is None
    assert store.snapshot()["straight_line"] == {"set": False, "shots": 0, "shots_needed": 3}


def test_the_screen_counts_the_shots_it_still_needs(store):
    shots(store, [-0.80, -0.78])

    assert store.snapshot()["straight_line"] == {"set": False, "shots": 2, "shots_needed": 1}


def test_setting_straight_takes_the_mean_start_line_of_the_recent_shots(store):
    shots(store, [-0.80, -0.76, -0.84])

    snapshot = store.update({"straight_line": "set"})

    assert snapshot["straight_line"]["set"] is True
    assert store.geometry().horizontal_phase_reference_rad == pytest.approx(-0.80, abs=1e-3)


def test_only_the_last_five_shots_count(store):
    shots(store, [2.0, 2.0, 2.0, -0.8, -0.8, -0.8, -0.8, -0.8])

    store.update({"straight_line": "set"})

    assert store.snapshot()["straight_line"]["shots"] == 5
    assert store.geometry().horizontal_phase_reference_rad == pytest.approx(-0.8)


def test_the_mean_wraps_around_pi_instead_of_averaging_through_zero(store):
    shots(store, [3.10, -3.10, 3.12, -3.12])

    store.update({"straight_line": "set"})

    assert abs(store.geometry().horizontal_phase_reference_rad) > 3.0


def test_setting_straight_needs_three_shots(store):
    shots(store, [-0.8, -0.8])

    with pytest.raises(RigSetupError, match="3 shots"):
        store.update({"straight_line": "set"})

    assert store.geometry().horizontal_phase_reference_rad is None


def test_clearing_brings_back_no_horizontal_reading(store):
    shots(store, [-0.8, -0.8, -0.8])
    store.update({"straight_line": "set"})

    snapshot = store.update({"straight_line": "clear"})

    assert snapshot["straight_line"]["set"] is False
    assert store.geometry().horizontal_phase_reference_rad is None


def test_the_straight_line_survives_a_restart_but_the_shot_tally_does_not(store, tmp_path):
    shots(store, [-0.8, -0.8, -0.8])
    store.update({"straight_line": "set"})

    reopened = RigSetupStore(tmp_path / "rig.json")

    assert reopened.geometry().horizontal_phase_reference_rad == pytest.approx(-0.8)
    assert reopened.snapshot()["straight_line"] == {"set": True, "shots": 0, "shots_needed": 3}


def test_an_unknown_straight_line_action_is_refused(store):
    with pytest.raises(RigSetupError, match="set or clear"):
        store.update({"straight_line": "maybe"})


def test_a_refused_straight_line_leaves_the_rest_of_the_change_unapplied(store):
    with pytest.raises(RigSetupError):
        store.update({"ball_distance_m": 1.4, "straight_line": "set"})

    assert store.geometry().ball_distance_m is None


@pytest.mark.parametrize("phase", [None, "x", True, float("nan"), 4.0, -4.0])
def test_an_unusable_start_line_is_ignored(store, phase):
    assert store.observe_horizontal_phase(phase) is False
    assert store.snapshot()["straight_line"]["shots"] == 0


def test_a_saved_straight_line_that_makes_no_sense_is_dropped(tmp_path):
    path = tmp_path / "rig.json"
    path.write_text(json.dumps({"horizontal_zero_rad": 99}))

    assert RigSetupStore(path).geometry().horizontal_phase_reference_rad is None


def test_the_straight_line_is_the_same_in_both_setups(store):
    shots(store, [-0.8, -0.8, -0.8])
    store.update({"straight_line": "set"})

    store.update({"active": COURSE})

    assert store.geometry().horizontal_phase_reference_rad == pytest.approx(-0.8)
