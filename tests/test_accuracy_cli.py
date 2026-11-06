"""Tests for the ``openflight-accuracy`` command line."""

import json

import pytest
from accuracy_logs import make_shot, write_session

from openflight.accuracy import cli


def _write_quietly(capsys, *args, **kwargs):
    """Write a session, discarding the logger's own start/end messages."""
    path = write_session(*args, **kwargs)
    capsys.readouterr()
    return path


def test_report_prints_markdown_and_writes_json(tmp_path, capsys):
    _write_quietly(capsys, tmp_path, "range", shots=[make_shot(), make_shot()])
    json_path = tmp_path / "report.json"

    status = cli.main(["report", str(tmp_path), "--json", str(json_path)])

    assert status == 0
    assert capsys.readouterr().out.startswith("# OpenFlight accuracy and reliability report\n")
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert [session["shots"] for session in report["sessions"]] == [2]
    assert report["drift"] == []


def test_report_writes_markdown_to_a_file_instead_of_stdout(tmp_path, capsys):
    session = _write_quietly(capsys, tmp_path, "range", shots=[make_shot()])
    output = tmp_path / "report.md"

    assert cli.main(["report", str(session), "--output", str(output)]) == 0

    assert capsys.readouterr().out == ""
    assert "## Reliability" in output.read_text(encoding="utf-8")


def test_report_reads_the_session_log_directory_by_default(tmp_path, monkeypatch, capsys):
    _write_quietly(capsys, tmp_path, "range", shots=[make_shot()])
    monkeypatch.setattr(cli, "DEFAULT_LOG_DIR", tmp_path)

    assert cli.main(["report"]) == 0

    assert "from 1 session (1 shot)" in capsys.readouterr().out


def test_mock_sessions_need_include_mock(tmp_path, capsys):
    _write_quietly(capsys, tmp_path, "mock", mode="mock", shots=[make_shot()])

    assert cli.main(["report", str(tmp_path)]) == 0
    skipped = capsys.readouterr().out
    assert cli.main(["report", str(tmp_path), "--include-mock"]) == 0
    included = capsys.readouterr().out

    assert "Skipped 1 mock session;" in skipped
    assert "No sessions to report on." in skipped
    assert "## Reliability" in included


def test_missing_path_is_an_error(tmp_path, capsys):
    assert cli.main(["report", str(tmp_path / "nowhere")]) == 1

    assert "no session log or directory" in capsys.readouterr().err


def test_directory_without_sessions_is_an_error(tmp_path, capsys):
    assert cli.main(["report", str(tmp_path)]) == 1

    assert "no session_*.jsonl files found" in capsys.readouterr().err


def test_a_subcommand_is_required():
    with pytest.raises(SystemExit) as raised:
        cli.main([])

    assert raised.value.code == 2
