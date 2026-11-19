"""Search and summarise the local command journal."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

_STATUSES = {"all", "failed", "ok", "running"}
_COLUMNS = (
    "id",
    "command",
    "started_at",
    "ended_at",
    "exit_code",
    "duration_ms",
    "cwd",
    "hostname",
    "username",
    "session_id",
    "origin",
    "revision",
)
_MAX_BUCKETS = 96


def search_commands(
    connection: sqlite3.Connection,
    *,
    query: str = "",
    status: str = "all",
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
    device_id: str | None = None,
    cwd: str | None = None,
    exit_code: int | None = None,
) -> dict:
    """Return commands matching a substring, status, and optional time range.

    The FTS5 index is maintained on write for later ranked search. This browser
    search uses substring matching so a fragment in the middle of a command
    still matches, and so characters such as quotes do not become query syntax.
    `since` and `until` are inclusive ISO-8601 instants. Either bound may be omitted.
    """
    if isinstance(limit, bool) or isinstance(offset, bool):
        raise ValueError("limit and offset must be integers")
    if not isinstance(limit, int) or not isinstance(offset, int):
        raise ValueError("limit and offset must be integers")
    if limit < 1 or limit > 100:
        raise ValueError("limit must be from 1 to 100")
    if offset < 0:
        raise ValueError("offset must be zero or greater")

    where, params, _start, _end = _where(
        query,
        status,
        since,
        until,
        device_id=device_id,
        cwd=cwd,
        exit_code=exit_code,
    )
    columns = ", ".join(_COLUMNS)
    rows = connection.execute(
        f"""
        SELECT {columns}
        FROM commands
        {where}
        ORDER BY started_at DESC, rowid DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    total = int(
        connection.execute(f"SELECT COUNT(*) FROM commands {where}", params).fetchone()[0]
    )
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def command_stats(
    connection: sqlite3.Connection,
    *,
    query: str = "",
    status: str = "all",
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """Count commands by completion state for the same filters as search."""
    where, params, _start, _end = _where(query, status, since, until)
    row = connection.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN exit_code IS NOT NULL AND exit_code != 0 THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN exit_code = 0 THEN 1 ELSE 0 END) AS succeeded,
            SUM(CASE WHEN exit_code IS NULL AND ended_at IS NULL THEN 1 ELSE 0 END) AS running,
            SUM(CASE WHEN exit_code IS NULL AND ended_at IS NOT NULL THEN 1 ELSE 0 END) AS unknown
        FROM commands
        {where}
        """,
        params,
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "failed": int(row["failed"] or 0),
        "succeeded": int(row["succeeded"] or 0),
        "running": int(row["running"] or 0),
        "unknown": int(row["unknown"] or 0),
    }


def command_activity(
    connection: sqlite3.Connection,
    *,
    query: str = "",
    status: str = "all",
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """Count commands in time buckets using the same filters as search."""
    where, params, start, end = _where(query, status, since, until)
    rows = connection.execute(
        f"SELECT started_at, exit_code, ended_at FROM commands {where}",
        params,
    ).fetchall()
    return _activity(rows, start, end)


def _where(
    query: str,
    status: str,
    since: str | None,
    until: str | None,
    *,
    device_id: str | None = None,
    cwd: str | None = None,
    exit_code: int | None = None,
) -> tuple[str, list[object], datetime | None, datetime | None]:
    """Build the shared filter clause. Raises ValueError for a bad filter."""
    if status not in _STATUSES:
        raise ValueError("status must be all, failed, ok, or running")
    if isinstance(exit_code, bool) or (exit_code is not None and not isinstance(exit_code, int)):
        raise ValueError("exit_code must be an integer")
    start = _parse_bound(since, "since")
    end = _parse_bound(until, "until")
    if start is not None and end is not None and start > end:
        raise ValueError("since must not be after until")

    clauses: list[str] = []
    params: list[object] = []
    text = query.strip()
    if text:
        clauses.append("command LIKE ? ESCAPE '\\'")
        params.append(_like_pattern(text))
    if status == "failed":
        clauses.append("exit_code IS NOT NULL AND exit_code != 0")
    elif status == "ok":
        clauses.append("exit_code = 0")
    elif status == "running":
        clauses.append("exit_code IS NULL AND ended_at IS NULL")
    if start is not None:
        clauses.append("unixepoch(replace(started_at, 'Z', '+00:00')) >= ?")
        params.append(int(start.timestamp()))
    if end is not None:
        clauses.append("unixepoch(replace(started_at, 'Z', '+00:00')) <= ?")
        params.append(int(end.timestamp()))
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if cwd:
        clauses.append("(cwd = ? OR cwd LIKE ? ESCAPE '\\')")
        params.append(cwd)
        params.append(_prefix_pattern(cwd))
    if exit_code is not None:
        clauses.append("exit_code = ?")
        params.append(exit_code)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params, start, end


def _parse_bound(value: str | None, name: str) -> datetime | None:
    """Parse an optional ISO-8601 instant as UTC."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    text = value.strip()
    if text == "":
        return None
    if len(text) > 40:
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _activity(rows: list, start: datetime | None, end: datetime | None) -> dict:
    """Place command instants into a bounded set of buckets."""
    events: list[tuple[datetime, int | None, str | None]] = []
    for row in rows:
        try:
            moment = _parse_bound(row["started_at"], "started_at")
        except ValueError:
            continue
        if moment is None:
            continue
        events.append((moment, row["exit_code"], row["ended_at"]))

    if not events and start is None and end is None:
        return {"since": None, "until": None, "step_seconds": 3600, "points": []}
    if events:
        moments = [moment for moment, _code, _ended in events]
        if start is None:
            start = min(moments)
        if end is None:
            end = max(moments)
    if start is None or end is None:
        return {"since": None, "until": None, "step_seconds": 3600, "points": []}
    if end < start:
        end = start

    step = _step_seconds(start, end)
    span = max(int((end - start).total_seconds()), 0)
    count = span // step + 1
    while count > _MAX_BUCKETS:
        step *= 2
        count = span // step + 1
    points = [
        {
            "start": (start + timedelta(seconds=step * index)).isoformat(),
            "succeeded": 0,
            "failed": 0,
            "running": 0,
            "unknown": 0,
        }
        for index in range(count)
    ]
    for moment, code, ended in events:
        if moment < start or moment > end:
            continue
        index = int((moment - start).total_seconds()) // step
        index = min(max(index, 0), count - 1)
        if code is None and not ended:
            key = "running"
        elif code is None:
            key = "unknown"
        elif code == 0:
            key = "succeeded"
        else:
            key = "failed"
        points[index][key] += 1
    return {
        "since": start.isoformat(),
        "until": end.isoformat(),
        "step_seconds": step,
        "points": points,
    }


def _step_seconds(start: datetime, end: datetime) -> int:
    """Pick a bucket width that keeps a short range readable."""
    span = max(int((end - start).total_seconds()), 60)
    if span <= 2 * 3600:
        return 5 * 60
    if span <= 48 * 3600:
        return 3600
    if span <= 90 * 86400:
        return 86400
    return 7 * 86400


def _like_pattern(query: str) -> str:
    return f"%{_escape_like(query)}%"


def _prefix_pattern(query: str) -> str:
    return f"{_escape_like(query)}/%"


def _escape_like(query: str) -> str:
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
