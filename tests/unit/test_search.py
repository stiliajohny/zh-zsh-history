"""Tests for journal search and stats."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zh.database import connect, init_db, record_command
from zh.search import command_activity, command_stats, search_commands

STARTED = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def at(seconds: int) -> str:
    """Return an ISO timestamp offset from a fixed start."""
    return (STARTED + timedelta(seconds=seconds)).isoformat()


def journal(tmp_path: Path):
    """Create a journal with succeeded, failed, and running commands."""
    path = init_db(tmp_path / "history.sqlite")
    connection = connect(path)
    record_command(
        connection,
        command="git status",
        started_at=at(0),
        ended_at=at(1),
        exit_code=0,
        cwd="/work",
    )
    record_command(
        connection,
        command="git push",
        started_at=at(10),
        ended_at=at(12),
        exit_code=1,
        cwd="/work",
    )
    record_command(connection, command="sleep 5", started_at=at(20), cwd="/tmp")
    record_command(
        connection,
        command="100%_done",
        started_at=at(30),
        ended_at=at(31),
        exit_code=0,
    )
    return connection


def test_search_orders_newest_first_and_filters_status(tmp_path: Path) -> None:
    """Newest commands come first, and status filters do not overlap."""
    connection = journal(tmp_path)
    try:
        all_commands = search_commands(connection)
        assert [item["command"] for item in all_commands["items"]] == [
            "100%_done",
            "sleep 5",
            "git push",
            "git status",
        ]
        failed = search_commands(connection, status="failed")
        assert [item["command"] for item in failed["items"]] == ["git push"]
        running = search_commands(connection, status="running")
        assert [item["command"] for item in running["items"]] == ["sleep 5"]
        succeeded = search_commands(connection, status="ok")
        assert "git push" not in [item["command"] for item in succeeded["items"]]
        assert "sleep 5" not in [item["command"] for item in succeeded["items"]]
    finally:
        connection.close()


def test_search_escapes_like_wildcards_and_is_case_insensitive(tmp_path: Path) -> None:
    """User text is a literal substring, including % and _."""
    connection = journal(tmp_path)
    try:
        assert [item["command"] for item in search_commands(connection, query="GIT")["items"]] == [
            "git push",
            "git status",
        ]
        assert [item["command"] for item in search_commands(connection, query="%")["items"]] == [
            "100%_done"
        ]
        assert search_commands(connection, query="_")["total"] == 1
        assert search_commands(connection, query="a_c")["total"] == 0
        assert search_commands(connection, query="no-such-command")["total"] == 0
    finally:
        connection.close()


def test_search_rejects_bad_paging_and_reports_stats(tmp_path: Path) -> None:
    """Paging bounds are enforced, and stats count each state once."""
    connection = journal(tmp_path)
    try:
        page = search_commands(connection, limit=1, offset=1)
        assert page["total"] == 4
        assert len(page["items"]) == 1
        assert command_stats(connection) == {
            "total": 4,
            "failed": 1,
            "succeeded": 2,
            "running": 1,
            "unknown": 0,
        }
        with pytest.raises(ValueError):
            search_commands(connection, status="crashed")
        with pytest.raises(ValueError):
            search_commands(connection, limit=101)
        with pytest.raises(ValueError):
            search_commands(connection, offset=-1)
    finally:
        connection.close()


def test_search_filters_by_time_range(tmp_path: Path) -> None:
    """A start, an end, or both keep only commands inside that range."""
    connection = journal(tmp_path)
    try:
        from_push = search_commands(connection, since=at(10))
        assert [item["command"] for item in from_push["items"]] == [
            "100%_done",
            "sleep 5",
            "git push",
        ]
        through_push = search_commands(connection, until=at(10))
        assert [item["command"] for item in through_push["items"]] == ["git push", "git status"]
        middle = search_commands(connection, since=at(10), until=at(20))
        assert [item["command"] for item in middle["items"]] == ["sleep 5", "git push"]
        stats = command_stats(connection, since=at(10), until=at(20))
        assert stats == {"total": 2, "failed": 1, "succeeded": 0, "running": 1, "unknown": 0}
        activity = command_activity(connection, since=at(0), until=at(30))
        assert sum(point["succeeded"] for point in activity["points"]) == 2
        assert sum(point["failed"] for point in activity["points"]) == 1
        assert sum(point["running"] for point in activity["points"]) == 1
        with pytest.raises(ValueError, match="after"):
            search_commands(connection, since=at(20), until=at(10))
        with pytest.raises(ValueError, match="ISO-8601"):
            search_commands(connection, since="yesterday")
    finally:
        connection.close()
