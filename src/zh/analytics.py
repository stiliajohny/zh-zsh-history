"""Summaries over the local command journal."""

from __future__ import annotations

import sqlite3

from zh.search import command_stats, search_commands


def recent_commands(connection: sqlite3.Connection, *, limit: int = 20) -> dict:
    """Return the newest commands."""
    return search_commands(connection, limit=min(max(limit, 1), 100))


def failed_commands(connection: sqlite3.Connection, *, limit: int = 20) -> dict:
    """Return commands that finished with a non-zero exit code."""
    return search_commands(connection, status="failed", limit=min(max(limit, 1), 100))


def top_commands(connection: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    """Return the most repeated command texts."""
    bounded = min(max(limit, 1), 100)
    rows = connection.execute(
        """
        SELECT command, COUNT(*) AS count
        FROM commands
        GROUP BY command
        ORDER BY count DESC, command ASC
        LIMIT ?
        """,
        (bounded,),
    ).fetchall()
    return [{"command": row["command"], "count": int(row["count"])} for row in rows]


def stats(connection: sqlite3.Connection) -> dict:
    """Count commands by completion state."""
    return command_stats(connection)
