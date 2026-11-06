"""Assemble and render the accuracy and reliability report."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from . import checks, stats
from .sessions import SessionLog


@dataclass(frozen=True)
class DriftSeries:
    """A per-sample series compared session to session.

    ``min_shift`` is the smallest change in rate or median worth reporting, so
    long histories do not flag differences too small to matter.
    """

    label: str
    binary: bool
    min_shift: float


DRIFT_SERIES = {
    "trigger_accepted": DriftSeries("Share of triggers accepted", True, 0.10),
    "vertical_angle_measured": DriftSeries(
        "Share of shots with a measured vertical angle", True, 0.10
    ),
    "iwr6843_accepted": DriftSeries("Share of IWR6843 captures with an accepted angle", True, 0.10),
    "lcmf_single_channel": DriftSeries("Share of IWR6843 angles from a single channel", True, 0.10),
    "ops_track_speed_ratio": DriftSeries("IWR6843 track speed / OPS243 ball speed", False, 0.02),
    "lcmf_agreement_deg": DriftSeries("LCMF channel disagreement (deg)", False, 0.5),
    "mount_pitch_deg": DriftSeries("Enclosure pitch (deg)", False, 0.25),
}


def build_report(sessions: list[SessionLog], *, include_mock: bool = False) -> dict:
    """Summarize each session and compare sessions for drift, oldest first."""
    used = [session for session in sessions if include_mock or not session.is_mock]
    drift, drift_skipped = _find_drift(used)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sessions_found": len(sessions),
        "mock_sessions_skipped": 0 if include_mock else sum(s.is_mock for s in sessions),
        "sessions": [summarize_session(session) for session in used],
        "drift": drift,
        "drift_skipped": drift_skipped,
    }


def summarize_session(session: SessionLog) -> dict:
    """Reliability and consistency figures for one session."""
    shots = session.shots
    return {
        "session_id": session.session_id,
        "path": str(session.path),
        "start_time": session.start_time,
        "mode": session.mode,
        "app_version": session.start.get("app_version"),
        "malformed_lines": session.malformed_lines,
        "shots": len(shots),
        "triggers": _trigger_summary(session.triggers),
        "launch_angle": {axis: _angle_summary(shots, axis) for axis in checks.ANGLE_AXES},
        "club_speed": _club_speed_summary(shots),
        "iwr6843": _iwr6843_summary(session.iwr6843_captures),
        "inclinometer": _inclinometer_summary(shots),
        "compensation": _compensation_summary(shots),
        "pipeline_ms": _pipeline_summary(shots),
        "errors": dict(Counter(_error_component(error) for error in session.errors)),
        "sim_forwarded": _sim_forwarded(session),
    }


def _rate(count: int, total: int) -> dict:
    interval = stats.wilson_interval(count, total)
    return {
        "count": count,
        "total": total,
        "rate": count / total if total else None,
        "ci95": list(interval) if interval else None,
    }


def _distribution(values: list[float]) -> dict:
    return {
        "n": len(values),
        "median": stats.median(values),
        "p10": stats.percentile(values, 10),
        "p90": stats.percentile(values, 90),
    }


def _present(values: Iterable[float | None]) -> list[float]:
    return [value for value in values if value is not None]


def _trigger_summary(triggers: list[dict]) -> dict:
    accepted = sum(1 for trigger in triggers if trigger.get("accepted"))
    reasons = Counter(
        trigger.get("reason") or "unspecified"
        for trigger in triggers
        if not trigger.get("accepted")
    )
    return {"accepted": _rate(accepted, len(triggers)), "rejected_reasons": dict(reasons)}


def _angle_summary(shots: list[dict], axis: str) -> dict:
    sources = Counter(checks.angle_source(shot, axis) or "missing" for shot in shots)
    measured = sum(1 for shot in shots if checks.angle_measured(shot, axis))
    return {"measured": _rate(measured, len(shots)), "sources": dict(sources)}


def _club_speed_summary(shots: list[dict]) -> dict:
    flags = [checks.club_speed_over_smash_limit(shot) for shot in shots]
    measured = [flag for flag in flags if flag is not None]
    return {
        "measured": _rate(len(measured), len(shots)),
        "over_smash_limit": _rate(sum(measured), len(measured)),
    }


def _iwr6843_summary(captures: list[dict]) -> dict:
    accepted = [capture for capture in captures if checks.iwr6843_accepted(capture)]
    single = sum(1 for capture in accepted if checks.lcmf_single_channel(capture))
    ratios = _present(checks.ops_track_speed_ratio(capture) for capture in captures)
    mismatched = sum(1 for ratio in ratios if checks.speed_mismatch(ratio))
    agreement = _present(checks.lcmf_agreement_deg(capture) for capture in captures)
    return {
        "captures": len(captures),
        "statuses": dict(Counter(checks.iwr6843_status(capture) for capture in captures)),
        "accepted": _rate(len(accepted), len(captures)),
        "single_channel": _rate(single, len(accepted)),
        "ops_track_speed_ratio": _distribution(ratios),
        "speed_mismatch": _rate(mismatched, len(ratios)),
        "agreement_deg": _distribution(agreement),
    }


def _inclinometer_summary(shots: list[dict]) -> dict:
    readings = [
        shot["inclinometer"] for shot in shots if isinstance(shot.get("inclinometer"), dict)
    ]
    applied = sum(1 for reading in readings if reading.get("applied"))
    not_applied = Counter(
        reading.get("status") or "unknown" for reading in readings if not reading.get("applied")
    )
    pitches = _present(checks.mount_pitch_deg(shot) for shot in shots)
    return {
        "applied": _rate(applied, len(readings)),
        "not_applied": dict(not_applied),
        "pitch_deg": {
            "n": len(pitches),
            "min": min(pitches, default=None),
            "max": max(pitches, default=None),
        },
    }


def _compensation_summary(shots: list[dict]) -> dict:
    corrected = sum(
        1 for shot in shots if checks.number(shot.get("ball_speed_raw_mph")) is not None
    )
    return {
        "ball_speed_corrected": _rate(corrected, len(shots)),
        "spin_sources": dict(Counter(shot.get("spin_source") or "none" for shot in shots)),
    }


def _pipeline_summary(shots: list[dict]) -> dict:
    """Per-stage processing time from each shot's ``pipeline_ms``.

    ``trigger_event`` latency is left out: it includes the idle wait for the swing.
    """
    stages: dict[str, list[float]] = {}
    for shot in shots:
        timings = shot.get("pipeline_ms")
        if not isinstance(timings, dict):
            continue
        for stage, value in timings.items():
            milliseconds = checks.number(value)
            if milliseconds is not None:
                stages.setdefault(str(stage), []).append(milliseconds)
    return {stage: _distribution(values) for stage, values in sorted(stages.items())}


def _error_component(error: dict) -> str:
    context = error.get("context")
    component = context.get("component") if isinstance(context, dict) else None
    return str(component or "unspecified")


def _sim_forwarded(session: SessionLog) -> dict | None:
    """Shots that reached a simulator; None when no simulator received any.

    ``sim_send`` numbers come from the simulator's own counter and repeat once
    per connector, so distinct numbers count forwarded shots.
    """
    if not session.sim_sends:
        return None
    forwarded = {entry.get("shot_number") for entry in session.sim_sends}
    return _rate(min(len(forwarded), len(session.shots)), len(session.shots))


def _session_samples(session: SessionLog) -> dict[str, list[float]]:
    samples: dict[str, list[float]] = {name: [] for name in DRIFT_SERIES}

    def add(name: str, value: float | bool | None) -> None:
        if value is not None:
            samples[name].append(float(value))

    for trigger in session.triggers:
        add("trigger_accepted", bool(trigger.get("accepted")))
    for shot in session.shots:
        add("vertical_angle_measured", checks.angle_measured(shot, "vertical"))
        add("mount_pitch_deg", checks.mount_pitch_deg(shot))
    for capture in session.iwr6843_captures:
        add("iwr6843_accepted", checks.iwr6843_accepted(capture))
        add("lcmf_single_channel", checks.lcmf_single_channel(capture))
        add("ops_track_speed_ratio", checks.ops_track_speed_ratio(capture))
        add("lcmf_agreement_deg", checks.lcmf_agreement_deg(capture))
    return samples


def _find_drift(sessions: list[SessionLog]) -> tuple[list[dict], list[dict]]:
    per_session = [_session_samples(session) for session in sessions]
    found: list[dict] = []
    skipped: list[dict] = []
    for name, series in DRIFT_SERIES.items():
        groups = [samples[name] for samples in per_session]
        if sum(len(group) >= stats.MIN_SESSION_SAMPLES for group in groups) < 2:
            reason = f"needs two sessions with at least {stats.MIN_SESSION_SAMPLES} samples"
            skipped.append({"series": name, "reason": reason})
            continue
        for shift in stats.find_session_shifts(
            groups, binary=series.binary, min_shift=series.min_shift
        ):
            found.append(
                {
                    "series": name,
                    "session_id": sessions[shift.session_index].session_id,
                    "path": str(sessions[shift.session_index].path),
                    "direction": "up" if shift.level > shift.reference else "down",
                    "reference": shift.reference,
                    "level": shift.level,
                    "p_value": shift.p_value,
                    "samples": shift.samples,
                    "reference_samples": shift.reference_samples,
                }
            )
    return found, skipped


def render_markdown(report: dict) -> str:
    """Human-readable report for the terminal or a Markdown file."""
    sessions = report["sessions"]
    total_shots = sum(session["shots"] for session in sessions)
    lines = [
        "# OpenFlight accuracy and reliability report",
        "",
        f"Generated {report['generated_at']} from {_plural(len(sessions), 'session')} "
        f"({_plural(total_shots, 'shot')}).",
    ]
    if report["mock_sessions_skipped"]:
        lines.append(
            f"Skipped {_plural(report['mock_sessions_skipped'], 'mock session')}; "
            "pass `--include-mock` to include them."
        )
    if not sessions:
        lines += ["", "No sessions to report on."]
        return "\n".join(lines) + "\n"
    lines += _reliability_table(sessions)
    lines += _consistency_table(sessions)
    lines += _compensation_table(sessions)
    lines += _drift_section(report)
    lines += _details_section(sessions)
    return "\n".join(lines) + "\n"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _fmt_rate(rate: dict | None) -> str:
    if not rate or not rate["total"]:
        return "—"
    return f"{rate['count']}/{rate['total']} ({rate['rate']:.0%})"


def _fmt_number(value: float | None, digits: int) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _fmt_counts(counts: dict) -> str:
    return ", ".join(f"{key} ({value})" for key, value in sorted(counts.items())) or "none"


def _table(title: str, header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["", f"## {title}", "", "| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join(["---"] + ["---:"] * (len(header) - 1)) + " |")
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _reliability_table(sessions: list[dict]) -> list[str]:
    header = [
        "Session",
        "Shots",
        "Triggers accepted",
        "Vertical angle measured",
        "Horizontal angle measured",
        "IWR6843 accepted",
        "Single-channel angles",
        "Sim forwarded",
        "Errors",
    ]
    rows = [
        [
            s["session_id"],
            str(s["shots"]),
            _fmt_rate(s["triggers"]["accepted"]),
            _fmt_rate(s["launch_angle"]["vertical"]["measured"]),
            _fmt_rate(s["launch_angle"]["horizontal"]["measured"]),
            _fmt_rate(s["iwr6843"]["accepted"]),
            _fmt_rate(s["iwr6843"]["single_channel"]),
            _fmt_rate(s["sim_forwarded"]),
            str(sum(s["errors"].values())),
        ]
        for s in sessions
    ]
    return _table("Reliability", header, rows)


def _consistency_table(sessions: list[dict]) -> list[str]:
    header = [
        "Session",
        "IWR6843/OPS243 speed (p10–p90)",
        "Outside ±15%",
        "LCMF disagreement (deg)",
        "Club speed implies smash > 1.50",
        "Enclosure pitch range (deg)",
    ]
    rows = []
    for s in sessions:
        ratio = s["iwr6843"]["ops_track_speed_ratio"]
        pitch = s["inclinometer"]["pitch_deg"]
        spread = (
            "—"
            if pitch["n"] == 0
            else f"{pitch['min']:+.2f} to {pitch['max']:+.2f} ({pitch['max'] - pitch['min']:.2f})"
        )
        rows.append(
            [
                s["session_id"],
                (
                    "—"
                    if not ratio["n"]
                    else f"{ratio['median']:.3f} ({ratio['p10']:.3f}–{ratio['p90']:.3f})"
                ),
                _fmt_rate(s["iwr6843"]["speed_mismatch"]),
                _fmt_number(s["iwr6843"]["agreement_deg"]["median"], 2),
                _fmt_rate(s["club_speed"]["over_smash_limit"]),
                spread,
            ]
        )
    return _table("Consistency", header, rows)


def _compensation_table(sessions: list[dict]) -> list[str]:
    header = ["Session", "Ball-speed cosine correction", "Spin source", "Inclinometer tilt applied"]
    rows = [
        [
            s["session_id"],
            _fmt_rate(s["compensation"]["ball_speed_corrected"]),
            _fmt_counts(s["compensation"]["spin_sources"]),
            _fmt_rate(s["inclinometer"]["applied"]),
        ]
        for s in sessions
    ]
    return _table("Compensation in effect", header, rows)


def _fmt_p(p_value: float) -> str:
    return "p < 0.0001" if p_value < 0.0001 else f"p = {p_value:.4f}"


def _drift_section(report: dict) -> list[str]:
    lines = [
        "",
        "## Drift across sessions",
        "",
        "Each session is compared with the sessions before it, back to the last shift: "
        "Fisher's exact test for rates, Mann-Whitney for measurements. A shift needs "
        f"p < {stats.SHIFT_ALPHA} and a change large enough to matter.",
        "",
    ]
    for shift in report["drift"]:
        series = DRIFT_SERIES[shift["series"]]
        level_format = "{:.0%}" if series.binary else "{:.3f}"
        verb = "rose" if shift["direction"] == "up" else "dropped"
        lines.append(
            f"- **{series.label}** {verb} from {level_format.format(shift['reference'])} "
            f"to {level_format.format(shift['level'])} in {shift['session_id']} "
            f"({shift['samples']} samples against {shift['reference_samples']}, "
            f"{_fmt_p(shift['p_value'])})."
        )
    if not report["drift"]:
        lines.append("- No shift found in any series with enough data.")
    for skipped in report["drift_skipped"]:
        label = DRIFT_SERIES[skipped["series"]].label
        lines.append(f"- Not checked: {label}. {skipped['reason'].capitalize()}.")
    return lines


def _details_section(sessions: list[dict]) -> list[str]:
    lines = ["", "## Details"]
    for s in sessions:
        timing = ", ".join(
            f"{stage} {dist['median']:.0f}/{dist['p90']:.0f}"
            for stage, dist in s["pipeline_ms"].items()
        )
        lines += [
            "",
            f"### {s['session_id']} ({s['mode']}, {s['path']})",
            "",
            f"- Trigger rejections: {_fmt_counts(s['triggers']['rejected_reasons'])}",
            f"- IWR6843 statuses: {_fmt_counts(s['iwr6843']['statuses'])}",
            f"- Inclinometer not applied: {_fmt_counts(s['inclinometer']['not_applied'])}",
            f"- Errors by component: {_fmt_counts(s['errors'])}",
            f"- Processing time median/p90 ms: {timing or 'none logged'}",
        ]
        if s["malformed_lines"]:
            lines.append(f"- Unreadable log lines skipped: {s['malformed_lines']}")
    return lines
