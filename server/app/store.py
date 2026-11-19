"""SQLite and PostgreSQL storage for the sync API."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAX_BATCH = 500
MAX_COMMAND = 100_000
MAX_PAYLOAD = 200_000
PENDING_TIMEOUT = timedelta(seconds=30)


def hash_token(token: str) -> str:
    """Hash a bearer token. Tokens are random, so a fast hash is enough."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def utc_now() -> str:
    """Return the current UTC time."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Store:
    """Account, device, and event store with a commit-safe pull cursor."""

    def __init__(self, connection, *, placeholder: str = "?") -> None:
        self.connection = connection
        self.placeholder = placeholder
        self.lock = threading.Lock()
        self.ensure_schema()

    def execute(self, sql: str, params: tuple | list = ()):
        """Run one statement and return its cursor."""
        if self.placeholder != "?":
            sql = sql.replace("?", self.placeholder)
        cursor = self.connection.cursor()
        cursor.execute(sql, tuple(params))
        return cursor

    def commit(self) -> None:
        """Commit the current transaction."""
        if self.placeholder == "?":
            self.connection.commit()
            return
        self.execute("COMMIT")

    def rollback(self) -> None:
        """Roll back the current transaction."""
        if self.placeholder == "?":
            self.connection.rollback()
            return
        self.execute("ROLLBACK")

    def begin(self) -> None:
        """Start a write transaction."""
        if self.placeholder == "?":
            self.execute("BEGIN IMMEDIATE")
        else:
            self.execute("BEGIN")

    def ensure_schema(self) -> None:
        """Create tables. PostgreSQL uses the versioned migration file."""
        if self.placeholder == "?":
            self._sqlite_schema()
            return
        migration = Path(__file__).resolve().parents[1] / "migrations" / "001_init.sql"
        script = migration.read_text(encoding="utf-8")
        for statement in [part.strip() for part in script.split(";") if part.strip()]:
            self.execute(statement)
        self.commit()

    def register(self, bootstrap_token: str, name: str, hostname: str | None) -> dict:
        """Create a device under the account identified by the bootstrap token."""
        if not bootstrap_token or not name:
            raise ValueError("name and bootstrap token are required")
        token = secrets.token_urlsafe(32)
        device_id = str(uuid.uuid4())
        with self.lock:
            self.begin()
            try:
                account_id = self._account_for_bootstrap(hash_token(bootstrap_token))
                self.execute(
                    """
                    INSERT INTO server_devices (
                        id, account_id, name, hostname, token_hash, created_at, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (device_id, account_id, name, hostname, hash_token(token), utc_now()),
                )
                self.commit()
            except Exception:
                self.rollback()
                raise
        return {"device_id": device_id, "token": token, "account_id": account_id}

    def device_for_token(self, token: str) -> dict | None:
        """Return the device for a bearer token, including revoked devices."""
        row = self.execute(
            """
            SELECT id, account_id, name, hostname, created_at, revoked_at
            FROM server_devices
            WHERE token_hash = ?
            """,
            (hash_token(token),),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_devices(self, account_id: str) -> list[dict]:
        """Return devices in an account without token hashes."""
        rows = self.execute(
            """
            SELECT id, name, hostname, created_at, revoked_at
            FROM server_devices
            WHERE account_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (account_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def revoke(self, account_id: str, device_id: str) -> bool:
        """Revoke a device in the same account. Missing devices return False."""
        try:
            device_id = str(uuid.UUID(device_id))
        except ValueError:
            return False
        with self.lock:
            self.begin()
            try:
                cursor = self.execute(
                    """
                    UPDATE server_devices
                    SET revoked_at = ?
                    WHERE id = ? AND account_id = ? AND revoked_at IS NULL
                    """,
                    (utc_now(), device_id, account_id),
                )
                self.commit()
            except Exception:
                self.rollback()
                raise
        return cursor.rowcount > 0

    def push(
        self, account_id: str, device_id: str, events: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        """Store new events idempotently. Invalid events are rejected and not stored."""
        if len(events) > MAX_BATCH:
            raise ValueError(f"batch is larger than {MAX_BATCH}")
        acked: list[dict] = []
        rejected: list[dict] = []
        for event in events:
            identity = {"id": str(event.get("id") or ""), "revision": event.get("revision")}
            reason = _validate_event(event, device_id)
            if reason:
                rejected.append({**identity, "error": reason})
                continue
            if self._event_exists(account_id, event["id"], int(event["revision"])):
                acked.append({"id": event["id"], "revision": int(event["revision"])})
                continue
            seq = self.reserve(account_id)
            try:
                self.commit_reserved(account_id, seq, device_id, event)
            except Exception as exc:
                self.abandon(account_id, seq)
                rejected.append({**identity, "error": str(exc)})
                continue
            acked.append({"id": event["id"], "revision": int(event["revision"])})
        return acked, rejected

    def reserve(self, account_id: str) -> int:
        """Commit a pending sequence number before the event itself is stored."""
        with self.lock:
            self.begin()
            try:
                row = self.execute(
                    "SELECT next_seq FROM account_counters WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
                if row is None:
                    seq = 1
                    self.execute(
                        "INSERT INTO account_counters (account_id, next_seq) VALUES (?, 2)",
                        (account_id,),
                    )
                else:
                    seq = int(row["next_seq"])
                    self.execute(
                        "UPDATE account_counters SET next_seq = ? WHERE account_id = ?",
                        (seq + 1, account_id),
                    )
                self.execute(
                    """
                    INSERT INTO reservations (account_id, seq, state, created_at)
                    VALUES (?, ?, 'pending', ?)
                    """,
                    (account_id, seq, utc_now()),
                )
                self.commit()
            except Exception:
                self.rollback()
                raise
        return seq

    def commit_reserved(self, account_id: str, seq: int, device_id: str, event: dict) -> None:
        """Store an event at a previously reserved sequence and mark that reservation committed."""
        payload = json.dumps(event["payload"], separators=(",", ":"), sort_keys=True)
        with self.lock:
            self.begin()
            try:
                self.execute(
                    """
                    INSERT INTO server_events (
                        account_id, event_id, revision, device_id, kind, payload, seq
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        str(uuid.UUID(str(event["id"]))),
                        int(event["revision"]),
                        device_id,
                        event["kind"],
                        payload,
                        seq,
                    ),
                )
                self.execute(
                    """
                    UPDATE reservations SET state = 'committed'
                    WHERE account_id = ? AND seq = ?
                    """,
                    (account_id, seq),
                )
                self.commit()
            except Exception:
                self.rollback()
                raise

    def abandon(self, account_id: str, seq: int) -> None:
        """Drop a reservation that never received an event."""
        with self.lock:
            self.begin()
            try:
                self.execute(
                    """
                    UPDATE reservations SET state = 'abandoned'
                    WHERE account_id = ? AND seq = ? AND state = 'pending'
                    """,
                    (account_id, seq),
                )
                self.commit()
            except Exception:
                self.rollback()
                raise

    def pull(
        self,
        account_id: str,
        cursor: int,
        limit: int,
        *,
        now: datetime | None = None,
        pending_timeout: timedelta = PENDING_TIMEOUT,
    ) -> dict:
        """Return committed events that are not past an uncommitted lower sequence."""
        if cursor < 0 or limit < 1 or limit > MAX_BATCH:
            raise ValueError("cursor and limit are out of range")
        current = now or datetime.now(UTC)
        cutoff = (current - pending_timeout).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self.lock:
            self.begin()
            try:
                self.execute(
                    """
                    UPDATE reservations SET state = 'abandoned'
                    WHERE account_id = ? AND state = 'pending' AND created_at < ?
                    """,
                    (account_id, cutoff),
                )
                horizon_row = self.execute(
                    """
                    SELECT MIN(seq) AS horizon FROM reservations
                    WHERE account_id = ? AND state = 'pending'
                    """,
                    (account_id,),
                ).fetchone()
                horizon = horizon_row["horizon"] if horizon_row else None
                sql = """
                    SELECT event_id, revision, device_id, kind, payload, seq
                    FROM server_events
                    WHERE account_id = ? AND seq > ?
                """
                params: list[object] = [account_id, cursor]
                if horizon is not None:
                    sql += " AND seq < ?"
                    params.append(int(horizon))
                sql += " ORDER BY seq ASC LIMIT ?"
                params.append(limit)
                rows = self.execute(sql, params).fetchall()
                self.commit()
            except Exception:
                self.rollback()
                raise
        events = []
        next_cursor = cursor
        for row in rows:
            events.append(
                {
                    "id": row["event_id"],
                    "revision": int(row["revision"]),
                    "device_id": row["device_id"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload"]),
                    "seq": int(row["seq"]),
                }
            )
            next_cursor = int(row["seq"])
        return {"events": events, "next_cursor": str(next_cursor)}

    def _event_exists(self, account_id: str, event_id: str, revision: int) -> bool:
        try:
            event_id = str(uuid.UUID(str(event_id)))
        except ValueError:
            return False
        row = self.execute(
            """
            SELECT 1 FROM server_events
            WHERE account_id = ? AND event_id = ? AND revision = ?
            """,
            (account_id, event_id, revision),
        ).fetchone()
        return row is not None

    def _account_for_bootstrap(self, bootstrap_hash: str) -> str:
        row = self.execute(
            "SELECT id FROM accounts WHERE bootstrap_hash = ?",
            (bootstrap_hash,),
        ).fetchone()
        if row is not None:
            return str(row["id"])
        account_id = str(uuid.uuid4())
        self.execute(
            "INSERT INTO accounts (id, bootstrap_hash, created_at) VALUES (?, ?, ?)",
            (account_id, bootstrap_hash, utc_now()),
        )
        return account_id

    def _sqlite_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                bootstrap_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS server_devices (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                name TEXT NOT NULL,
                hostname TEXT,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS account_counters (
                account_id TEXT PRIMARY KEY,
                next_seq INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS reservations (
                account_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (account_id, seq)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS server_events (
                account_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                device_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                seq INTEGER NOT NULL,
                PRIMARY KEY (account_id, event_id, revision),
                UNIQUE (account_id, seq)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_server_events_seq
            ON server_events (account_id, seq)
            """,
        )
        for statement in statements:
            self.execute(statement)
        self.commit()


def open_sqlite(path: Path) -> Store:
    """Open a server database stored in SQLite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return Store(connection)


def open_postgres(url: str) -> Store:
    """Open the PostgreSQL server database."""
    import psycopg
    from psycopg.rows import dict_row

    connection = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    return Store(connection, placeholder="%s")


def _validate_event(event: dict, device_id: str) -> str | None:
    try:
        uuid.UUID(str(event.get("id")))
    except (ValueError, TypeError):
        return "id must be a UUID"
    revision = event.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        return "revision must be a positive integer"
    if event.get("kind") not in {"command", "tombstone"}:
        return "kind must be command or tombstone"
    if event.get("device_id") != device_id:
        return "device does not match the credential"
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return "payload must be an object"
    encoded = json.dumps(payload)
    if len(encoded) > MAX_PAYLOAD:
        return "payload is too large"
    if event["kind"] == "command":
        command = payload.get("command")
        if not isinstance(command, str) or command == "" or len(command) > MAX_COMMAND:
            return "command must not be empty"
        if not payload.get("started_at"):
            return "started_at is required"
    return None
