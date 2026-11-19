"""Shell hook operations. Network sync is never waited on here."""

from __future__ import annotations

import getpass
import json
import os
import socket
import sqlite3
import time
import uuid
from pathlib import Path

from zh.config import load_config
from zh.database import (
    complete_command,
    connect,
    ensure_local_device,
    init_db,
    record_command,
    start_session,
    utc_now,
)
from zh.gitinfo import read_git_metadata
from zh.history import mark_history_import, prepare_history_import
from zh.security import filter_environment, rejection_reason

HOOK_TIMEOUT = 0.2


def session(db_path: Path, session_id: str | None = None, pid: int | None = None) -> str:
    """Create the shell session and return its id."""
    path = init_db(db_path)
    connection = connect(path, timeout=HOOK_TIMEOUT)
    try:
        return start_session(connection, session_id, pid=pid if pid is not None else os.getpid())
    finally:
        connection.close()


def start(db_path: Path, session_id: str, cwd: str, command: str) -> str | None:
    """Record a command start, or skip it when a privacy filter rejects it."""
    config = load_config()
    if rejection_reason(command, config):
        return None
    command_id = str(uuid.uuid4())
    branch, commit = read_git_metadata(Path(cwd))
    environment = filter_environment(os.environ, list(config.get("env_allowlist") or []))
    payload = {
        "op": "start",
        "id": command_id,
        "command": command,
        "started_at": utc_now(),
        "cwd": cwd,
        "session_id": session_id or None,
        "hostname": _machine_name(),
        "username": _login_name(),
        "git_branch": branch,
        "git_commit": commit,
        "environment": environment or None,
    }
    try:
        path = init_db(db_path)
        connection = connect(path, timeout=HOOK_TIMEOUT)
        try:
            _flush_spool(connection, _spool_dir(path))
            device_id = ensure_local_device(connection)
            record_command(connection, device_id=device_id, **_start_fields(payload))
        finally:
            connection.close()
    except sqlite3.OperationalError:
        _write_spool(db_path, payload)
    return command_id


def finish(db_path: Path, command_id: str, exit_code: int) -> None:
    """Record the original exit code. A locked database is spooled and retried later."""
    payload = {"op": "finish", "id": command_id, "exit_code": exit_code, "ended_at": utc_now()}
    try:
        path = init_db(db_path)
        connection = connect(path, timeout=HOOK_TIMEOUT)
        try:
            _flush_spool(connection, _spool_dir(path))
            complete_command(
                connection, command_id, exit_code=exit_code, ended_at=payload["ended_at"]
            )
        finally:
            connection.close()
    except sqlite3.OperationalError:
        _write_spool(db_path, payload)
    except ValueError as exc:
        if "unknown command" not in str(exc):
            raise
        _write_spool(db_path, payload)


def pending_history(db_path: Path, session_id: str) -> str | None:
    """Return a staged history file for this session, when one is needed."""
    path = init_db(db_path)
    connection = connect(path, timeout=HOOK_TIMEOUT)
    try:
        staged = prepare_history_import(connection, path, session_id)
    finally:
        connection.close()
    if staged is None:
        return None
    return str(staged)


def mark_history(db_path: Path, session_id: str) -> None:
    """Remember which staged commands this session has imported."""
    path = init_db(db_path)
    connection = connect(path, timeout=HOOK_TIMEOUT)
    try:
        mark_history_import(connection, path, session_id)
    finally:
        connection.close()


def _machine_name() -> str | None:
    """Return this computer's hostname."""
    name = socket.gethostname()
    return name or None


def _login_name() -> str | None:
    """Return the user running this command."""
    try:
        name = getpass.getuser()
    except (OSError, KeyError):
        return None
    return name or None


def _start_fields(payload: dict) -> dict:
    return {
        "command_id": payload["id"],
        "command": payload["command"],
        "started_at": payload["started_at"],
        "cwd": payload["cwd"],
        "session_id": payload["session_id"],
        "hostname": payload["hostname"],
        "username": payload.get("username"),
        "git_branch": payload["git_branch"],
        "git_commit": payload["git_commit"],
        "environment": payload["environment"],
    }


def _spool_dir(db_path: Path) -> Path:
    return db_path.expanduser().parent / "spool"


def _write_spool(db_path: Path, payload: dict) -> None:
    directory = _spool_dir(db_path)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"{time.time_ns()}-{uuid.uuid4()}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _flush_spool(connection: sqlite3.Connection, directory: Path) -> None:
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["op"] == "start":
            device_id = ensure_local_device(connection)
            try:
                record_command(connection, device_id=device_id, **_start_fields(payload))
            except sqlite3.IntegrityError:
                connection.rollback()
            path.unlink()
            continue
        if payload["op"] == "finish":
            try:
                complete_command(
                    connection,
                    payload["id"],
                    exit_code=int(payload["exit_code"]),
                    ended_at=payload["ended_at"],
                )
            except ValueError as exc:
                if "unknown command" in str(exc):
                    return
                raise
            path.unlink()
