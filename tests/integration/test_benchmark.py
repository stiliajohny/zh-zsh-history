"""Scale checks for the local journal. Set ZH_BENCH=100000 or ZH_BENCH=1000000."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from zh.database import connect, init_db
from zh.search import search_commands


def test_search_scales(tmp_path: Path) -> None:
    """Insert a large journal in one transaction and search it."""
    raw = os.environ.get("ZH_BENCH", "")
    if not raw:
        pytest.skip("set ZH_BENCH to a row count")
    count = int(raw)
    path = init_db(tmp_path / "history.sqlite")
    connection = connect(path)
    try:
        started = time.perf_counter()
        connection.execute("BEGIN")
        connection.executemany(
            """
            INSERT INTO commands (
                id, command, started_at, ended_at, exit_code, duration_ms,
                cwd, session_id, origin, revision, sync_state
            ) VALUES (
                ?, ?, '2026-11-18T12:00:00.000000Z', NULL, 0, 1, '/tmp', NULL, 'local', 1, 'pending'
            )
            """,
            [(f"{index:032x}"[:32], f"echo {index}") for index in range(count)],
        )
        connection.commit()
        insert_seconds = time.perf_counter() - started
        found = search_commands(connection, query=f"echo {count // 2}", limit=5)
        search_seconds = time.perf_counter() - started - insert_seconds
    finally:
        connection.close()
    assert found["total"] == 1
    print(f"rows={count} insert_seconds={insert_seconds:.3f} search_seconds={search_seconds:.3f}")
