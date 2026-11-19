"""Tests for privacy filters, hooks, history staging, and capture."""

from __future__ import annotations

import getpass
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zh.config import load_config, save_config
from zh.database import connect, get_command, init_db, record_command
from zh.history import mark_history_import, prepare_history_import
from zh.hooks import finish, start
from zh.security import filter_environment, rejection_reason

STARTED = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def at(seconds: int) -> str:
    """Return an ISO timestamp offset from a fixed start."""
    return (STARTED + timedelta(seconds=seconds)).isoformat()


def test_filters_reject_before_a_reason_is_needed() -> None:
    """Leading spaces, pause, and patterns explain why a command is dropped."""
    space = {"ignore_space": True, "paused": False, "exclude_regex": []}
    paused = {"ignore_space": True, "paused": True, "exclude_regex": []}
    patterned = {"ignore_space": False, "paused": False, "exclude_regex": ["token="]}
    broken = {"ignore_space": True, "paused": False, "exclude_regex": ["["]}
    assert rejection_reason(" secret", space) == "leading-space"
    assert rejection_reason("secret", paused) == "paused"
    assert rejection_reason("token=abc", patterned) == "pattern"
    assert rejection_reason("ls", broken) is None
    assert filter_environment({"SECRET": "1", "TERM": "xterm"}, ["TERM"]) == {"TERM": "xterm"}


def test_hook_stores_exit_code_and_skips_private_commands(tmp_path: Path, monkeypatch) -> None:
    """A finished command keeps its exit code, and filtered commands are not stored."""
    path = init_db(tmp_path / "history.sqlite")
    monkeypatch.setenv("SECRET", "nope")
    monkeypatch.setenv("TERM", "xterm")
    save_config({"env_allowlist": ["TERM"], "ignore_space": True, "exclude_regex": ["API_TOKEN"]})
    command_id = start(path, "session-1", str(tmp_path), "make test")
    assert command_id
    finish(path, command_id, 7)
    connection = connect(path)
    try:
        stored = get_command(connection, command_id)
        assert stored is not None
        assert stored["exit_code"] == 7
        assert stored["duration_ms"] is not None
        assert stored["hostname"] == socket.gethostname()
        assert stored["username"] == getpass.getuser()
        assert stored["environment"] == '{"TERM":"xterm"}'
        assert "SECRET" not in stored["environment"]
        assert connection.execute("SELECT COUNT(*) FROM command_revisions").fetchone()[0] == 2
    finally:
        connection.close()
    assert start(path, "session-1", str(tmp_path), " echo hidden") is None
    assert start(path, "session-1", str(tmp_path), "export API_TOKEN=abc") is None
    save_config({"paused": True})
    assert start(path, "session-1", str(tmp_path), "ls") is None
    connection = connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1
    finally:
        connection.close()


def test_locked_database_is_spooled_and_replayed(tmp_path: Path) -> None:
    """A busy journal does not drop the command or its exit code."""
    path = init_db(tmp_path / "history.sqlite")
    locker = sqlite3.connect(path)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        command_id = start(path, "session-2", str(tmp_path), "echo spool")
        assert command_id
        assert list((path.parent / "spool").glob("*.json"))
    finally:
        locker.commit()
        locker.close()
    finish(path, command_id, 0)
    connection = connect(path)
    try:
        stored = get_command(connection, command_id)
        assert stored is not None
        assert stored["exit_code"] == 0
        assert stored["command"] == "echo spool"
    finally:
        connection.close()
    assert list((path.parent / "spool").glob("*.json")) == []


def test_history_import_is_tracked_per_session_and_leaves_histfile(tmp_path: Path) -> None:
    """Remote commands are staged once per session and the history file is not rewritten."""
    path = init_db(tmp_path / "history.sqlite")
    histfile = tmp_path / "zsh_history"
    histfile.write_text("original\n", encoding="utf-8")
    connection = connect(path)
    try:
        record_command(
            connection, command="echo remote", started_at=at(0), exit_code=0, origin="import"
        )
        first = prepare_history_import(connection, path, "session-a")
        second = prepare_history_import(connection, path, "session-a")
        other = prepare_history_import(connection, path, "session-b")
        assert first == second
        assert other is not None
        assert first.read_bytes().split(b"\0")[0] == b"echo remote"
        mark_history_import(connection, path, "session-a")
        mark_history_import(connection, path, "session-a")
        assert prepare_history_import(connection, path, "session-a") is None
        assert prepare_history_import(connection, path, "session-b") == other
        mark_history_import(connection, path, "session-b")
        rows = connection.execute("SELECT COUNT(*) FROM history_imports").fetchone()[0]
        assert rows == 2
    finally:
        connection.close()
    assert histfile.read_text(encoding="utf-8") == "original\n"


def test_git_metadata_is_read_from_the_work_tree(tmp_path: Path) -> None:
    """A command records the branch and commit from .git without running git."""
    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "refs" / "heads" / "main").write_text("abc123\n", encoding="utf-8")
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    path = init_db(tmp_path / "history.sqlite")
    command_id = start(path, "session-3", str(tmp_path), "git status")
    connection = connect(path)
    try:
        stored = get_command(connection, command_id)
    finally:
        connection.close()
    assert stored["git_branch"] == "main"
    assert stored["git_commit"] == "abc123"
    assert load_config()["sync_enabled"] is False
