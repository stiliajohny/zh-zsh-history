"""SQLite journal for recorded shell commands."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 4

_V1_STATEMENTS = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE commands (
        id TEXT PRIMARY KEY,
        command TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        exit_code INTEGER,
        duration_ms INTEGER,
        cwd TEXT,
        session_id TEXT,
        origin TEXT NOT NULL DEFAULT 'local' CHECK (origin IN ('local', 'import')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1)
    )
    """,
    "CREATE INDEX idx_commands_started_at ON commands (started_at)",
    "CREATE INDEX idx_commands_exit_code ON commands (exit_code)",
    """
    CREATE VIRTUAL TABLE commands_fts USING fts5(
        command,
        content='commands',
        content_rowid='rowid'
    )
    """,
    """
    CREATE TRIGGER commands_ai AFTER INSERT ON commands BEGIN
        INSERT INTO commands_fts(rowid, command) VALUES (new.rowid, new.command);
    END
    """,
    """
    CREATE TRIGGER commands_ad AFTER DELETE ON commands BEGIN
        INSERT INTO commands_fts(commands_fts, rowid, command)
        VALUES ('delete', old.rowid, old.command);
    END
    """,
    """
    CREATE TRIGGER commands_au AFTER UPDATE ON commands BEGIN
        INSERT INTO commands_fts(commands_fts, rowid, command)
        VALUES ('delete', old.rowid, old.command);
        INSERT INTO commands_fts(rowid, command) VALUES (new.rowid, new.command);
    END
    """,
)

_COMMAND_COLUMNS = (
    "id",
    "command",
    "started_at",
    "ended_at",
    "exit_code",
    "duration_ms",
    "cwd",
    "session_id",
    "origin",
    "revision",
    "device_id",
    "hostname",
    "username",
    "git_branch",
    "git_commit",
    "environment",
    "sync_state",
)


def default_db_path() -> Path:
    """Return the journal path from ZH_DB or the XDG data directory."""
    override = os.environ.get("ZH_DB")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "zh" / "history.sqlite"


def connect(path: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Open a WAL connection. The caller closes it."""
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    connection.execute("PRAGMA journal_mode = WAL").fetchone()
    return connection


def init_db(path: Path) -> Path:
    """Create the journal file, apply migrations, and restrict its mode to 0600.

    The parent directory is created when missing. Its mode is set to 0700 only
    when this call created that directory, so an existing shared parent is left
    alone. Symlinked database paths are refused.
    """
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError("database path must not be a symlink")
    parent = path.parent
    parent_created = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if parent_created:
        os.chmod(parent, 0o700)
    _assert_sqlite(path)
    connection = connect(path)
    try:
        migrate(connection)
        ensure_local_device(connection)
    finally:
        connection.close()
    _tighten(path)
    return path


def migrate(connection: sqlite3.Connection) -> None:
    """Apply pending schema migrations. Completed versions are left in place."""
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(f"unsupported journal schema version {current}")
    if current >= SCHEMA_VERSION:
        return
    try:
        if current < 1:
            _apply_v1(connection)
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) < 2:
            _apply_v2(connection)
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) < 3:
            _apply_v3(connection)
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) < 4:
            _apply_v4(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _apply_v1(connection: sqlite3.Connection) -> None:
    """Create the original command journal. Later migrations extend these tables."""
    for statement in _V1_STATEMENTS:
        connection.execute(statement)
    _record_schema_version(connection, 1)


def _apply_v2(connection: sqlite3.Connection) -> None:
    """Add revisions, sessions, devices, outputs, sync state, imports and tombstones."""
    additions = {
        "device_id": "ALTER TABLE commands ADD COLUMN device_id TEXT",
        "hostname": "ALTER TABLE commands ADD COLUMN hostname TEXT",
        "git_branch": "ALTER TABLE commands ADD COLUMN git_branch TEXT",
        "git_commit": "ALTER TABLE commands ADD COLUMN git_commit TEXT",
        "environment": "ALTER TABLE commands ADD COLUMN environment TEXT",
        "sync_state": "ALTER TABLE commands ADD COLUMN sync_state TEXT NOT NULL DEFAULT 'pending'",
    }
    for name, statement in additions.items():
        if not _has_column(connection, "commands", name):
            connection.execute(statement)
    connection.execute(
        "UPDATE commands SET sync_state = 'imported' WHERE origin = 'import'"
    )
    _create_v2_tables(connection)
    if connection.execute("SELECT COUNT(*) FROM command_revisions").fetchone()[0] == 0:
        connection.execute(
            """
            INSERT INTO command_revisions (
                command_id, revision, recorded_at, command, started_at, ended_at,
                exit_code, duration_ms, cwd, session_id, device_id, hostname,
                git_branch, git_commit, environment, origin, sync_state
            )
            SELECT
                id, revision, started_at, command, started_at, ended_at,
                exit_code, duration_ms, cwd, session_id, device_id, hostname,
                git_branch, git_commit, environment, origin, sync_state
            FROM commands
            """
        )
    _record_schema_version(connection, 2)


def _apply_v3(connection: sqlite3.Connection) -> None:
    """Record the user who ran each command alongside the existing hostname."""
    if not _has_column(connection, "commands", "username"):
        connection.execute("ALTER TABLE commands ADD COLUMN username TEXT")
    if not _has_column(connection, "command_revisions", "username"):
        connection.execute("ALTER TABLE command_revisions ADD COLUMN username TEXT")
    _record_schema_version(connection, 3)


def _apply_v4(connection: sqlite3.Connection) -> None:
    """Mark commands that were read from a Zsh history file.

    Those rows stay out of the shell history refresh. A remote import leaves
    this column empty and can still be staged.
    """
    if not _has_column(connection, "commands", "import_kind"):
        connection.execute("ALTER TABLE commands ADD COLUMN import_kind TEXT")
    _record_schema_version(connection, 4)


def _create_v2_tables(connection: sqlite3.Connection) -> None:
    """Create version 2 tables and indexes when they are missing."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            device_id TEXT,
            started_at TEXT NOT NULL,
            hostname TEXT,
            pid INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            hostname TEXT,
            created_at TEXT NOT NULL,
            remote INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS command_revisions (
            command_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            recorded_at TEXT NOT NULL,
            command TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            exit_code INTEGER,
            duration_ms INTEGER,
            cwd TEXT,
            session_id TEXT,
            device_id TEXT,
            hostname TEXT,
            git_branch TEXT,
            git_commit TEXT,
            environment TEXT,
            origin TEXT NOT NULL,
            sync_state TEXT NOT NULL,
            PRIMARY KEY (command_id, revision),
            FOREIGN KEY (command_id) REFERENCES commands(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS outputs (
            id TEXT PRIMARY KEY,
            command_id TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            truncated INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            deleted_at TEXT,
            FOREIGN KEY (command_id) REFERENCES commands(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS history_imports (
            command_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            PRIMARY KEY (command_id, session_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tombstones (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            expires_at TEXT,
            revision INTEGER NOT NULL DEFAULT 1,
            sync_state TEXT NOT NULL DEFAULT 'pending'
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_commands_device_id ON commands (device_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_commands_sync_state ON commands (sync_state)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_revisions_sync_state ON command_revisions (sync_state)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_tombstones_sync_state ON tombstones (sync_state)"
    )


def _record_schema_version(connection: sqlite3.Connection, version: int) -> None:
    applied_at = utc_now()
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (version, applied_at),
    )
    connection.execute(f"PRAGMA user_version = {int(version)}")


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def record_command(
    connection: sqlite3.Connection,
    *,
    command: str,
    started_at: str,
    ended_at: str | None = None,
    exit_code: int | None = None,
    duration_ms: int | None = None,
    cwd: str | None = None,
    session_id: str | None = None,
    origin: str = "local",
    command_id: str | None = None,
    device_id: str | None = None,
    hostname: str | None = None,
    username: str | None = None,
    git_branch: str | None = None,
    git_commit: str | None = None,
    environment: dict | None = None,
    import_kind: str | None = None,
    infer_duration: bool = True,
) -> str:
    """Insert one command event and its first revision. Returns the event id."""
    if import_kind not in {None, "zsh"}:
        raise ValueError("import_kind must be zsh")
    fields = _command_fields(
        command=command,
        started_at=started_at,
        ended_at=ended_at,
        exit_code=exit_code,
        duration_ms=duration_ms,
        cwd=cwd,
        session_id=session_id,
        origin=origin,
        command_id=command_id,
        device_id=device_id,
        hostname=hostname,
        username=username,
        git_branch=git_branch,
        git_commit=git_commit,
        environment=environment,
        infer_duration=infer_duration,
    )
    sync_state = "imported" if origin == "import" else "pending"
    try:
        connection.execute(
            """
            INSERT INTO commands (
                id, command, started_at, ended_at, exit_code, duration_ms,
                cwd, session_id, origin, revision, device_id, hostname, username,
                git_branch, git_commit, environment, sync_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fields["id"],
                fields["command"],
                fields["started_at"],
                fields["ended_at"],
                fields["exit_code"],
                fields["duration_ms"],
                fields["cwd"],
                fields["session_id"],
                fields["origin"],
                fields["device_id"],
                fields["hostname"],
                fields["username"],
                fields["git_branch"],
                fields["git_commit"],
                fields["environment"],
                sync_state,
            ),
        )
        _insert_revision(
            connection, fields, revision=1, sync_state=sync_state, recorded_at=utc_now()
        )
        if import_kind is not None:
            connection.execute(
                "UPDATE commands SET import_kind = ? WHERE id = ?",
                (import_kind, fields["id"]),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    stored = _journal_file(connection)
    if stored is not None:
        _tighten(stored)
    return fields["id"]


def get_command(connection: sqlite3.Connection, command_id: str) -> dict | None:
    """Return one command event, or None when the id is absent."""
    columns = ", ".join(_COMMAND_COLUMNS)
    row = connection.execute(
        f"SELECT {columns} FROM commands WHERE id = ?",
        (command_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _journal_file(connection: sqlite3.Connection) -> Path | None:
    rows = connection.execute("PRAGMA database_list").fetchall()
    for row in rows:
        if row["name"] == "main" and row["file"]:
            return Path(row["file"])
    return None


def _assert_sqlite(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("rb") as handle:
        header = handle.read(16)
    if header != b"SQLite format 3\x00":
        raise ValueError(f"{path} is not a SQLite database")


def _tighten(path: Path) -> None:
    os.chmod(path, 0o600)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists() and not sidecar.is_symlink():
            os.chmod(sidecar, 0o600)


def _duration_ms(started_at: str, ended_at: str) -> int:
    started = _parse_time(started_at)
    ended = _parse_time(ended_at)
    milliseconds = int((ended - started).total_seconds() * 1000)
    if milliseconds < 0:
        raise ValueError("ended_at is earlier than started_at")
    return milliseconds


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamps must be ISO-8601") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def utc_now() -> str:
    """Return the current time as a UTC timestamp."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def ensure_local_device(connection: sqlite3.Connection) -> str:
    """Return this journal's device id, creating it once."""
    row = connection.execute(
        "SELECT value FROM sync_state WHERE key = 'device_id'"
    ).fetchone()
    if row is not None:
        return str(row["value"])
    device_id = str(uuid.uuid4())
    hostname = socket.gethostname()
    try:
        connection.execute(
            """
            INSERT INTO devices (id, name, hostname, created_at, remote, revoked_at)
            VALUES (?, ?, ?, ?, 0, NULL)
            """,
            (device_id, hostname, hostname, utc_now()),
        )
        connection.execute(
            "INSERT INTO sync_state (key, value) VALUES ('device_id', ?)",
            (device_id,),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        connection.rollback()
        row = connection.execute(
            "SELECT value FROM sync_state WHERE key = 'device_id'"
        ).fetchone()
        if row is None:
            raise
        return str(row["value"])
    return device_id


def start_session(
    connection: sqlite3.Connection,
    session_id: str | None = None,
    *,
    device_id: str | None = None,
    pid: int | None = None,
) -> str:
    """Create a shell session if it does not already exist."""
    if session_id is None:
        session_id = str(uuid.uuid4())
    else:
        session_id = str(uuid.UUID(session_id))
    if device_id is None:
        device_id = ensure_local_device(connection)
    connection.execute(
        """
        INSERT OR IGNORE INTO sessions (id, device_id, started_at, hostname, pid)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session_id, device_id, utc_now(), socket.gethostname(), pid),
    )
    connection.commit()
    return session_id


def complete_command(
    connection: sqlite3.Connection,
    command_id: str,
    *,
    exit_code: int,
    ended_at: str | None = None,
) -> None:
    """Store the exit code as a new revision. A second finish does not change it."""
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ValueError("exit_code must be an integer")
    current = get_command(connection, command_id)
    if current is None:
        raise ValueError("unknown command")
    if current["exit_code"] is not None:
        return
    ended = _normalize_time(ended_at or utc_now())
    duration = _duration_ms(current["started_at"], ended)
    revision = int(current["revision"]) + 1
    sync_state = "pending" if current["origin"] == "local" else current["sync_state"]
    fields = dict(current)
    fields.update(
        ended_at=ended,
        exit_code=exit_code,
        duration_ms=duration,
        sync_state=sync_state,
    )
    try:
        connection.execute(
            """
            UPDATE commands
            SET ended_at = ?, exit_code = ?, duration_ms = ?, revision = ?, sync_state = ?
            WHERE id = ?
            """,
            (ended, exit_code, duration, revision, sync_state, command_id),
        )
        _insert_revision(
            connection, fields, revision=revision, sync_state=sync_state, recorded_at=utc_now()
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def delete_command(
    connection: sqlite3.Connection,
    command_id: str,
    *,
    output_root: Path | None = None,
    tombstone_days: int = 30,
) -> bool:
    """Delete one command, its output files, and record a pending tombstone."""
    current = get_command(connection, command_id)
    if current is None:
        return False
    deleted_at = utc_now()
    expires_at = _normalize_time(
        (datetime.now(UTC) + timedelta(days=tombstone_days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    revision = int(current["revision"]) + 1
    outputs = list_outputs(connection, command_id)
    try:
        connection.execute(
            """
            INSERT INTO tombstones (id, kind, deleted_at, expires_at, revision, sync_state)
            VALUES (?, 'command', ?, ?, ?, 'pending')
            ON CONFLICT(id) DO UPDATE SET
                deleted_at = excluded.deleted_at,
                expires_at = excluded.expires_at,
                revision = excluded.revision,
                sync_state = 'pending'
            """,
            (command_id, deleted_at, expires_at, revision),
        )
        if output_root is not None:
            for output in outputs:
                _remove_output_file(output_root, output["relative_path"])
        connection.execute("DELETE FROM outputs WHERE command_id = ?", (command_id,))
        connection.execute("DELETE FROM command_revisions WHERE command_id = ?", (command_id,))
        connection.execute("DELETE FROM commands WHERE id = ?", (command_id,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return True


def list_outputs(connection: sqlite3.Connection, command_id: str) -> list[dict]:
    """Return output rows for one command."""
    rows = connection.execute(
        """
        SELECT id, command_id, relative_path, size_bytes, truncated, created_at, deleted_at
        FROM outputs
        WHERE command_id = ?
        ORDER BY created_at ASC
        """,
        (command_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def add_output(
    connection: sqlite3.Connection,
    *,
    command_id: str,
    relative_path: str,
    size_bytes: int,
    truncated: bool,
) -> str:
    """Record a compressed output file for a command."""
    output_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO outputs (
            id, command_id, relative_path, size_bytes, truncated, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (output_id, command_id, relative_path, size_bytes, int(truncated), utc_now()),
    )
    connection.commit()
    return output_id


def clean_journal(
    connection: sqlite3.Connection,
    *,
    older_than: datetime | None,
    output_root: Path | None,
    tombstone_days: int = 30,
) -> int:
    """Delete old commands and drop tombstones that have already been synced."""
    removed = 0
    if older_than is not None:
        rows = connection.execute("SELECT id, started_at FROM commands").fetchall()
        for row in rows:
            try:
                started = _parse_time(row["started_at"])
            except ValueError:
                continue
            if started < older_than:
                if delete_command(
                    connection,
                    row["id"],
                    output_root=output_root,
                    tombstone_days=tombstone_days,
                ):
                    removed += 1
    cutoff = utc_now()
    connection.execute(
        """
        DELETE FROM tombstones
        WHERE sync_state = 'synced' AND expires_at IS NOT NULL AND expires_at < ?
        """,
        (cutoff,),
    )
    connection.commit()
    return removed


def pending_events(connection: sqlite3.Connection, limit: int) -> list[dict]:
    """Return local revisions and tombstones that still need a push."""
    if limit < 1:
        raise ValueError("limit must be positive")
    revisions = connection.execute(
        """
        SELECT * FROM command_revisions
        WHERE sync_state = 'pending' AND origin = 'local'
        ORDER BY recorded_at ASC, revision ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    tombstones = connection.execute(
        """
        SELECT * FROM tombstones
        WHERE sync_state = 'pending'
        ORDER BY deleted_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    events: list[dict] = []
    for row in revisions:
        events.append(_revision_event(row))
    for row in tombstones:
        events.append(
            {
                "id": row["id"],
                "revision": int(row["revision"]),
                "kind": "tombstone",
                "device_id": None,
                "recorded_at": row["deleted_at"],
                "payload": {
                    "target_kind": row["kind"],
                    "deleted_at": row["deleted_at"],
                    "expires_at": row["expires_at"],
                },
            }
        )
    events.sort(key=lambda item: (item["recorded_at"], item["revision"], item["kind"]))
    return events[:limit]


def mark_events_synced(connection: sqlite3.Connection, events: list[dict]) -> None:
    """Mark only the acknowledged revisions and tombstones as synced."""
    try:
        for event in events:
            kind = event["kind"]
            if kind == "command":
                connection.execute(
                    """
                    UPDATE command_revisions
                    SET sync_state = 'synced'
                    WHERE command_id = ? AND revision = ? AND sync_state = 'pending'
                    """,
                    (event["id"], int(event["revision"])),
                )
                connection.execute(
                    """
                    UPDATE commands
                    SET sync_state = 'synced'
                    WHERE id = ? AND revision = ? AND sync_state = 'pending'
                    """,
                    (event["id"], int(event["revision"])),
                )
            elif kind == "tombstone":
                connection.execute(
                    """
                    UPDATE tombstones
                    SET sync_state = 'synced'
                    WHERE id = ? AND revision = ? AND sync_state = 'pending'
                    """,
                    (event["id"], int(event["revision"])),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def apply_remote_batch(
    connection: sqlite3.Connection,
    events: list[dict],
    next_cursor: str,
    *,
    output_root: Path | None = None,
) -> None:
    """Apply pulled events and advance the cursor in one transaction."""
    if not str(next_cursor).isdigit():
        raise ValueError("cursor must be a non-negative integer")
    try:
        for event in events:
            kind = event.get("kind")
            if kind == "command":
                _apply_remote_command(connection, event)
            elif kind == "tombstone":
                _apply_remote_tombstone(connection, event, output_root)
            else:
                raise ValueError("event kind must be command or tombstone")
        set_sync_value(connection, "pull_cursor", str(int(next_cursor)), commit=False)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def get_sync_value(connection: sqlite3.Connection, key: str) -> str | None:
    """Return one sync metadata value."""
    row = connection.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return str(row["value"])


def set_sync_value(
    connection: sqlite3.Connection,
    key: str,
    value: str,
    *,
    commit: bool = True,
) -> None:
    """Insert or replace one sync metadata value."""
    connection.execute(
        """
        INSERT INTO sync_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    if commit:
        connection.commit()


def sync_counts(connection: sqlite3.Connection) -> dict:
    """Summarise how much local sync work remains."""
    pending_revisions = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM command_revisions
            WHERE sync_state = 'pending' AND origin = 'local'
            """
        ).fetchone()[0]
    )
    pending_tombstones = int(
        connection.execute(
            "SELECT COUNT(*) FROM tombstones WHERE sync_state = 'pending'"
        ).fetchone()[0]
    )
    return {
        "pending": pending_revisions + pending_tombstones,
        "cursor": get_sync_value(connection, "pull_cursor") or "0",
        "last_error": get_sync_value(connection, "last_error"),
        "last_success": get_sync_value(connection, "last_success"),
    }


def list_devices(connection: sqlite3.Connection) -> list[dict]:
    """Return known devices. Credentials are not stored on these rows."""
    rows = connection.execute(
        """
        SELECT id, name, hostname, created_at, remote, revoked_at
        FROM devices
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def upsert_remote_device(
    connection: sqlite3.Connection,
    *,
    device_id: str,
    name: str,
    hostname: str | None,
    revoked_at: str | None,
) -> None:
    """Cache a device returned by the server without turning the local device remote."""
    device_id = str(uuid.UUID(device_id))
    existing = connection.execute(
        "SELECT remote FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    if existing is not None and int(existing["remote"]) == 0:
        connection.execute(
            """
            UPDATE devices
            SET name = ?, hostname = ?, revoked_at = ?
            WHERE id = ?
            """,
            (name, hostname, revoked_at, device_id),
        )
    else:
        connection.execute(
            """
            INSERT INTO devices (id, name, hostname, created_at, remote, revoked_at)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                hostname = excluded.hostname,
                revoked_at = excluded.revoked_at,
                remote = 1
            """,
            (device_id, name, hostname, utc_now(), revoked_at),
        )
    connection.commit()


def iter_commands(connection: sqlite3.Connection) -> list[dict]:
    """Return every stored command, oldest first."""
    columns = ", ".join(_COMMAND_COLUMNS)
    rows = connection.execute(
        f"SELECT {columns} FROM commands ORDER BY started_at ASC, rowid ASC"
    ).fetchall()
    return [dict(row) for row in rows]


def _command_fields(**kwargs: object) -> dict:
    command = kwargs["command"]
    if not isinstance(command, str) or command == "":
        raise ValueError("command must not be empty")
    if len(command) > 100_000:
        raise ValueError("command is too long")
    started_at = kwargs["started_at"]
    if not isinstance(started_at, str) or not started_at:
        raise ValueError("started_at is required")
    origin = kwargs["origin"]
    if origin not in {"local", "import"}:
        raise ValueError("origin must be local or import")
    exit_code = kwargs["exit_code"]
    duration_ms = kwargs["duration_ms"]
    _require_optional_int(exit_code, "exit_code")
    _require_optional_int(duration_ms, "duration_ms")
    if isinstance(duration_ms, int) and duration_ms < 0:
        raise ValueError("duration_ms must be zero or greater")
    command_id = kwargs["command_id"]
    if command_id is None:
        command_id = str(uuid.uuid4())
    else:
        command_id = str(uuid.UUID(str(command_id)))
    ended_at = kwargs["ended_at"]
    started_at = _normalize_time(started_at)
    ended_at = _normalize_time(ended_at) if isinstance(ended_at, str) else None
    if kwargs.get("infer_duration", True) and duration_ms is None and ended_at is not None:
        duration_ms = _duration_ms(started_at, ended_at)
    environment = kwargs["environment"]
    if environment is None:
        environment_text = None
    elif isinstance(environment, dict):
        environment_text = json.dumps(environment, sort_keys=True, separators=(",", ":"))
    else:
        raise ValueError("environment must be an object")
    return {
        "id": command_id,
        "command": command,
        "started_at": started_at,
        "ended_at": ended_at,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "cwd": kwargs["cwd"],
        "session_id": kwargs["session_id"],
        "origin": origin,
        "device_id": kwargs["device_id"],
        "hostname": kwargs["hostname"],
        "username": _optional_name(kwargs.get("username"), "username"),
        "git_branch": kwargs["git_branch"],
        "git_commit": kwargs["git_commit"],
        "environment": environment_text,
    }


def _insert_revision(
    connection: sqlite3.Connection,
    fields: dict,
    *,
    revision: int,
    sync_state: str,
    recorded_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO command_revisions (
            command_id, revision, recorded_at, command, started_at, ended_at,
            exit_code, duration_ms, cwd, session_id, device_id, hostname, username,
            git_branch, git_commit, environment, origin, sync_state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fields["id"],
            revision,
            recorded_at,
            fields["command"],
            fields["started_at"],
            fields["ended_at"],
            fields["exit_code"],
            fields["duration_ms"],
            fields["cwd"],
            fields["session_id"],
            fields["device_id"],
            fields["hostname"],
            fields["username"],
            fields["git_branch"],
            fields["git_commit"],
            fields["environment"],
            fields["origin"],
            sync_state,
        ),
    )


def _revision_event(row: sqlite3.Row) -> dict:
    return {
        "id": row["command_id"],
        "revision": int(row["revision"]),
        "kind": "command",
        "device_id": row["device_id"],
        "recorded_at": row["recorded_at"],
        "payload": {
            "command": row["command"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "exit_code": row["exit_code"],
            "duration_ms": row["duration_ms"],
            "cwd": row["cwd"],
            "session_id": row["session_id"],
            "hostname": row["hostname"],
            "username": row["username"],
            "git_branch": row["git_branch"],
            "git_commit": row["git_commit"],
        },
    }


def _apply_remote_command(connection: sqlite3.Connection, event: dict) -> None:
    command_id = str(uuid.UUID(str(event["id"])))
    revision = int(event["revision"])
    if revision < 1:
        raise ValueError("revision must be positive")
    if _tombstone_exists(connection, command_id):
        return
    payload = event["payload"]
    if not isinstance(payload, dict) or not payload.get("command"):
        raise ValueError("command must not be empty")
    existing = connection.execute(
        """
        SELECT sync_state FROM command_revisions
        WHERE command_id = ? AND revision = ?
        """,
        (command_id, revision),
    ).fetchone()
    if existing is not None:
        if existing["sync_state"] == "pending":
            connection.execute(
                """
                UPDATE command_revisions SET sync_state = 'synced'
                WHERE command_id = ? AND revision = ?
                """,
                (command_id, revision),
            )
            connection.execute(
                """
                UPDATE commands SET sync_state = 'synced'
                WHERE id = ? AND revision = ? AND origin = 'local'
                """,
                (command_id, revision),
            )
        return
    current = get_command(connection, command_id)
    started_at = _normalize_time(str(payload["started_at"]))
    ended_raw = payload.get("ended_at")
    ended_at = _normalize_time(ended_raw) if isinstance(ended_raw, str) else None
    exit_code = payload.get("exit_code")
    duration_ms = payload.get("duration_ms")
    _require_optional_int(exit_code, "exit_code")
    _require_optional_int(duration_ms, "duration_ms")
    if duration_ms is None and ended_at is not None:
        duration_ms = _duration_ms(started_at, ended_at)
    device_id = event.get("device_id")
    fields = {
        "id": command_id,
        "command": str(payload["command"]),
        "started_at": started_at,
        "ended_at": ended_at,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "cwd": payload.get("cwd"),
        "session_id": payload.get("session_id"),
        "device_id": device_id,
        "hostname": payload.get("hostname"),
        "username": payload.get("username"),
        "git_branch": payload.get("git_branch"),
        "git_commit": payload.get("git_commit"),
        "environment": None,
    }
    if current is None:
        fields["origin"] = "import"
        connection.execute(
            """
            INSERT INTO commands (
                id, command, started_at, ended_at, exit_code, duration_ms,
                cwd, session_id, origin, revision, device_id, hostname, username,
                git_branch, git_commit, environment, sync_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'import', ?, ?, ?, ?, ?, ?, NULL, 'imported')
            """,
            (
                command_id,
                fields["command"],
                started_at,
                ended_at,
                exit_code,
                duration_ms,
                fields["cwd"],
                fields["session_id"],
                revision,
                device_id,
                fields["hostname"],
                fields["username"],
                fields["git_branch"],
                fields["git_commit"],
            ),
        )
        _insert_revision(
            connection, fields, revision=revision, sync_state="imported", recorded_at=utc_now()
        )
        return
    fields["origin"] = current["origin"]
    state = "synced" if current["origin"] == "local" else "imported"
    if revision >= int(current["revision"]):
        connection.execute(
            """
            UPDATE commands
            SET command = ?, started_at = ?, ended_at = ?, exit_code = ?, duration_ms = ?,
                cwd = ?, session_id = ?, revision = ?, device_id = COALESCE(?, device_id),
                hostname = COALESCE(?, hostname), username = COALESCE(?, username),
                git_branch = COALESCE(?, git_branch),
                git_commit = COALESCE(?, git_commit), sync_state = ?
            WHERE id = ?
            """,
            (
                fields["command"],
                started_at,
                ended_at,
                exit_code,
                duration_ms,
                fields["cwd"],
                fields["session_id"],
                revision,
                device_id,
                fields["hostname"],
                fields["username"],
                fields["git_branch"],
                fields["git_commit"],
                state,
                command_id,
            ),
        )
    _insert_revision(connection, fields, revision=revision, sync_state=state, recorded_at=utc_now())


def _apply_remote_tombstone(
    connection: sqlite3.Connection,
    event: dict,
    output_root: Path | None,
) -> None:
    command_id = str(uuid.UUID(str(event["id"])))
    revision = int(event["revision"])
    payload = event.get("payload") or {}
    deleted_at = payload.get("deleted_at") or utc_now()
    expires_at = payload.get("expires_at")
    outputs = list_outputs(connection, command_id)
    if output_root is not None:
        for output in outputs:
            _remove_output_file(output_root, output["relative_path"])
    connection.execute("DELETE FROM outputs WHERE command_id = ?", (command_id,))
    connection.execute("DELETE FROM command_revisions WHERE command_id = ?", (command_id,))
    connection.execute("DELETE FROM commands WHERE id = ?", (command_id,))
    connection.execute(
        """
        INSERT INTO tombstones (id, kind, deleted_at, expires_at, revision, sync_state)
        VALUES (?, 'command', ?, ?, ?, 'synced')
        ON CONFLICT(id) DO UPDATE SET
            deleted_at = excluded.deleted_at,
            expires_at = excluded.expires_at,
            revision = excluded.revision,
            sync_state = 'synced'
        """,
        (command_id, deleted_at, expires_at, revision),
    )


def _tombstone_exists(connection: sqlite3.Connection, command_id: str) -> bool:
    row = connection.execute("SELECT 1 FROM tombstones WHERE id = ?", (command_id,)).fetchone()
    return row is not None


def _remove_output_file(root: Path, relative_path: str) -> None:
    candidate = (root / relative_path).resolve()
    base = root.resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError("output path escapes the output directory")
    if candidate.is_symlink():
        candidate.unlink()
        return
    candidate.unlink(missing_ok=True)


def _optional_name(value: object, name: str) -> str | None:
    """Return a short origin name, or None when it was not captured."""
    if value is None:
        return None
    if not isinstance(value, str) or value == "" or len(value) > 256:
        raise ValueError(f"{name} must be a short string")
    return value


def _optional_name(value: object, name: str) -> str | None:
    """Return a short optional name, or None when it was omitted."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = value.strip()
    if text == "":
        return None
    if len(text) > 256:
        raise ValueError(f"{name} is too long")
    return text


def _require_optional_int(value: object, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")


def _normalize_time(value: str) -> str:
    return _parse_time(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
