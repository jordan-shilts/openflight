"""``openflight-accuracy`` command-line entry point.

openflight-accuracy report [PATH ...]   # reliability, consistency and drift
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..session_logger import SessionLogger
from .report import build_report, render_markdown
from .sessions import load_sessions

DEFAULT_LOG_DIR = SessionLogger.DEFAULT_LOG_DIR


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openflight-accuracy",
        description="Analyze OpenFlight session logs for accuracy and reliability.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser(
        "report",
        help="Report reliability, sensor consistency and drift across sessions.",
    )
    report.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[DEFAULT_LOG_DIR],
        help=f"Session logs or directories of them (default: {DEFAULT_LOG_DIR}).",
    )
    report.add_argument(
        "--include-mock",
        action="store_true",
        help="Include mock-mode sessions, which are skipped by default.",
    )
    report.add_argument("--output", "-o", type=Path, help="Write the Markdown report here.")
    report.add_argument(
        "--json", type=Path, dest="json_path", help="Also write the report as JSON."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return its exit status."""
    args = _build_parser().parse_args(argv)
    try:
        sessions = load_sessions(args.paths)
    except FileNotFoundError as error:
        print(f"openflight-accuracy: {error}", file=sys.stderr)
        return 1
    if not sessions:
        print("openflight-accuracy: no session_*.jsonl files found", file=sys.stderr)
        return 1

    report = build_report(sessions, include_mock=args.include_mock)
    markdown = render_markdown(report)
    if args.output:
        args.output.write_text(markdown, encoding="utf-8")
    else:
        sys.stdout.write(markdown)
    if args.json_path:
        payload = json.dumps(report, indent=2, allow_nan=False) + "\n"
        args.json_path.write_text(payload, encoding="utf-8")
    return 0
