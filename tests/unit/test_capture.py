"""Tests for PTY capture and stored output files."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from zh.capture import load_output, record_pty
from zh.config import DEFAULTS
from zh.database import connect, get_command, init_db


def test_record_pty_keeps_exit_code_and_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A recorded command keeps its exit code and a readable output file."""
    monkeypatch.setenv("ZH_CONFIG", str(tmp_path / "config.json"))
    db = tmp_path / "history.sqlite"
    code = record_pty(db, [sys.executable, "-c", "print('captured')"], dict(DEFAULTS))
    assert code == 0
    connection = connect(db)
    try:
        row = connection.execute("SELECT id, exit_code FROM commands").fetchone()
        assert row is not None
        assert row["exit_code"] == 0
        stored = get_command(connection, row["id"])
        assert stored is not None
        assert "print" in stored["command"]
        output = connection.execute(
            "SELECT relative_path FROM outputs WHERE command_id = ?",
            (row["id"],),
        ).fetchone()
    finally:
        connection.close()
    assert output is not None
    assert b"captured" in load_output(db, output["relative_path"])


def test_record_pty_missing_command(tmp_path: Path) -> None:
    """A missing executable is reported and nothing is stored."""
    db = tmp_path / "history.sqlite"
    init_db(db)
    with pytest.raises(FileNotFoundError):
        record_pty(db, ["zh-missing-command-name"], dict(DEFAULTS))
    connection = connect(db)
    try:
        count = connection.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
    finally:
        connection.close()
    assert count == 0


def test_record_limits_output_and_a_filter_discards_it(tmp_path: Path) -> None:
    """A capture is size-limited, and a rejected command still runs without being stored."""
    db = tmp_path / "history.sqlite"
    config = dict(DEFAULTS)
    config["output_max_bytes"] = 4
    code = record_pty(
        db,
        [sys.executable, "-c", "import sys; sys.stdout.write('abcdefghijklmnopqrstuvwxyz')"],
        config,
    )
    assert code == 0
    connection = connect(db)
    try:
        row = connection.execute(
            "SELECT id, exit_code FROM commands"
        ).fetchone()
        assert row is not None
        stored = connection.execute(
            "SELECT relative_path, size_bytes, truncated FROM outputs WHERE command_id = ?",
            (row["id"],),
        ).fetchone()
    finally:
        connection.close()
    assert stored is not None
    assert int(stored["truncated"]) == 1
    assert int(stored["size_bytes"]) <= 4
    output_file = tmp_path / "outputs" / stored["relative_path"]
    assert stat.S_IMODE(output_file.stat().st_mode) == 0o600
    filtered = dict(DEFAULTS)
    filtered["exclude_regex"] = ["skip-me"]
    code = record_pty(db, [sys.executable, "-c", "print('skip-me')"], filtered)
    assert code == 0
    connection = connect(db)
    try:
        count = connection.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_load_output_rejects_parent_paths(tmp_path: Path) -> None:
    """Output reads cannot leave the output directory."""
    db = tmp_path / "history.sqlite"
    init_db(db)
    with pytest.raises(ValueError, match="outside"):
        load_output(db, "../history.sqlite")
