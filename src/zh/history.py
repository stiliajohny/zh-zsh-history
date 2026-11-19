"""Stage imported commands for the shell's own history list."""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from zh.database import utc_now

_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def history_dir(db_path: Path) -> Path:
    """Return the directory that holds per-session import files."""
    return db_path.expanduser().parent / "history-import"


def prepare_history_import(
    connection: sqlite3.Connection, db_path: Path, session_id: str
) -> Path | None:
    """Write commands this session has not imported yet.

    A pending file is left in place until the shell marks it. Reading that file
    again after a crash can insert the same lines into the in-memory history a
    second time. The journal record of the import is written only after the
    shell reports that it applied the file.
    """
    _check_session(session_id)
    directory = history_dir(db_path)
    staging = directory / f"{session_id}.hist"
    ids_path = directory / f"{session_id}.ids"
    pending = directory / f"{session_id}.pending"
    if pending.is_file() and staging.is_file() and ids_path.is_file():
        return staging
    rows = connection.execute(
        """
        SELECT id, command FROM commands
        WHERE origin = 'import'
          AND import_kind IS NULL
          AND id NOT IN (
              SELECT command_id FROM history_imports WHERE session_id = ?
          )
        ORDER BY started_at ASC, rowid ASC
        """,
        (session_id,),
    ).fetchall()
    if not rows:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    payload = b"".join(row["command"].encode("utf-8") + b"\0" for row in rows)
    ids = "".join(f"{row['id']}\n" for row in rows)
    _write_private(staging, payload, text=False)
    _write_private(ids_path, ids.encode("utf-8"), text=False)
    _write_private(pending, session_id.encode("utf-8"), text=False)
    return staging


def mark_history_import(connection: sqlite3.Connection, db_path: Path, session_id: str) -> None:
    """Record that this session has imported the staged commands."""
    _check_session(session_id)
    ids_path = history_dir(db_path) / f"{session_id}.ids"
    ids = []
    if ids_path.is_file():
        ids = [line for line in ids_path.read_text(encoding="utf-8").splitlines() if line]
    recorded_at = utc_now()
    try:
        for command_id in ids:
            connection.execute(
                """
                INSERT OR IGNORE INTO history_imports (command_id, session_id, imported_at)
                VALUES (?, ?, ?)
                """,
                (command_id, session_id, recorded_at),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    for name in (f"{session_id}.pending", f"{session_id}.hist", f"{session_id}.ids"):
        path = history_dir(db_path) / name
        if path.is_file() and not path.is_symlink():
            path.unlink()


def _check_session(session_id: str) -> None:
    if not _SESSION_ID.fullmatch(session_id):
        raise ValueError("session id contains unsupported characters")


def _write_private(path: Path, payload: bytes, *, text: bool) -> None:
    del text
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
