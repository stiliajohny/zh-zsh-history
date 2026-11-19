"""Tests for the local command journal."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zh.database import (_apply_v1, connect, default_db_path, get_command,
                         init_db, migrate, record_command)

STARTED = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def at(seconds: int) -> str:
    """Return an ISO timestamp offset from a fixed start."""
    return (STARTED + timedelta(seconds=seconds)).isoformat()


def test_default_path_uses_xdg_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The default journal lives under the XDG data directory."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("ZH_DB", raising=False)
    assert default_db_path() == tmp_path / "zh" / "history.sqlite"


def test_default_path_prefers_zh_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ZH_DB overrides the XDG location."""
    monkeypatch.setenv("ZH_DB", str(tmp_path / "custom.sqlite"))
    assert default_db_path() == tmp_path / "custom.sqlite"


def test_init_creates_private_wal_database(tmp_path: Path) -> None:
    """A new journal is WAL, migrated once, and mode 0600."""
    parent = tmp_path / "fresh"
    path = parent / "history.sqlite"
    init_db(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    connection = connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        init_db(path)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        connection.close()


def test_init_does_not_chmod_existing_parent(tmp_path: Path) -> None:
    """An existing directory keeps its mode when the journal is created inside it."""
    parent = tmp_path / "existing"
    parent.mkdir()
    os.chmod(parent, 0o755)
    init_db(parent / "history.sqlite")
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755


def test_init_rejects_symlink_and_foreign_file(tmp_path: Path) -> None:
    """The journal path must be a real SQLite file we can create or migrate."""
    target = tmp_path / "target.sqlite"
    target.write_bytes(b"")
    link = tmp_path / "link.sqlite"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        init_db(link)

    foreign = tmp_path / "notes.txt"
    foreign.write_text("not a database", encoding="utf-8")
    with pytest.raises(ValueError, match="not a SQLite database"):
        init_db(foreign)


def test_record_round_trip_and_duration(tmp_path: Path) -> None:
    """A recorded command keeps its text, exit code, and computed duration."""
    path = init_db(tmp_path / "history.sqlite")
    connection = connect(path)
    try:
        command_id = record_command(
            connection,
            command="git status",
            started_at=at(0),
            ended_at=at(2),
            exit_code=0,
            cwd="/work/zh",
            session_id="session-1",
        )
        stored = get_command(connection, command_id)
        assert stored is not None
        assert stored["command"] == "git status"
        assert stored["exit_code"] == 0
        assert stored["duration_ms"] == 2000
        assert stored["cwd"] == "/work/zh"
        assert stored["hostname"] is None
        assert stored["username"] is None
        assert get_command(connection, "00000000-0000-0000-0000-000000000000") is None
    finally:
        connection.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_record_rejects_bad_events(tmp_path: Path) -> None:
    """Empty commands, bad origins, and reversed times are not stored."""
    path = init_db(tmp_path / "history.sqlite")
    connection = connect(path)
    try:
        with pytest.raises(ValueError, match="empty"):
            record_command(connection, command="", started_at=at(0))
        with pytest.raises(ValueError, match="origin"):
            record_command(connection, command="ls", started_at=at(0), origin="remote")
        with pytest.raises(ValueError, match="exit_code"):
            record_command(connection, command="ls", started_at=at(0), exit_code=True)
        with pytest.raises(ValueError, match="earlier"):
            record_command(connection, command="ls", started_at=at(5), ended_at=at(1), exit_code=0)
        assert connection.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
    finally:
        connection.close()


def test_fts_index_follows_insert_update_and_delete(tmp_path: Path) -> None:
    """The FTS5 index matches the command table after each write."""
    path = init_db(tmp_path / "history.sqlite")
    connection = connect(path)
    try:
        command_id = record_command(connection, command="git status", started_at=at(0), exit_code=0)
        assert _fts_count(connection, '"git"') == 1
        connection.execute(
            "UPDATE commands SET command = ? WHERE id = ?",
            ("pytest -q", command_id),
        )
        connection.commit()
        assert _fts_count(connection, '"git"') == 0
        assert _fts_count(connection, '"pytest"') == 1
        connection.execute("DELETE FROM commands WHERE id = ?", (command_id,))
        connection.commit()
        assert _fts_count(connection, '"pytest"') == 0
    finally:
        connection.close()


def test_concurrent_records(tmp_path: Path) -> None:
    """Overlapping writers keep every command."""
    path = init_db(tmp_path / "history.sqlite")
    errors: list[BaseException] = []

    def write(index: int) -> None:
        connection = connect(path)
        try:
            record_command(connection, command=f"echo {index}", started_at=at(index), exit_code=0)
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=write, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    connection = connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 8
    finally:
        connection.close()


def test_v1_journal_migrates_forward(tmp_path: Path) -> None:
    """A version 1 journal gains revisions without losing the original command."""
    path = tmp_path / "history.sqlite"
    connection = connect(path)
    try:
        _apply_v1(connection)
        connection.execute(
            """
            INSERT INTO commands (
                id, command, started_at, ended_at, exit_code, duration_ms,
                cwd, session_id, origin, revision
            ) VALUES (
                '11111111-1111-1111-1111-111111111111', 'ls', ?, NULL, NULL,
                NULL, '/tmp', NULL, 'local', 1
            )
            """,
            (at(0),),
        )
        connection.commit()
        migrate(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        stored = get_command(connection, "11111111-1111-1111-1111-111111111111")
        assert stored is not None
        assert stored["command"] == "ls"
        assert stored["sync_state"] == "pending"
        assert connection.execute("SELECT COUNT(*) FROM command_revisions").fetchone()[0] == 1
        assert stored["username"] is None
        migrate(connection)
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 4
    finally:
        connection.close()


def _fts_count(connection: sqlite3.Connection, match: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM commands_fts WHERE commands_fts MATCH ?",
        (match,),
    ).fetchone()
    return int(row[0])
