"""PTY recording and stored output files."""

from __future__ import annotations

import os
import pty
import shlex
import subprocess
import threading
from pathlib import Path

from zh.database import add_output, connect, init_db
from zh.hooks import finish, start
from zh.output import output_dir, read_output, write_output
from zh.security import rejection_reason


def load_output(db_path: Path, relative_path: str) -> bytes:
    """Return one stored capture. The path must stay inside the output directory."""
    root = output_dir(db_path).resolve()
    path = (root / relative_path).resolve()
    if path != root and root not in path.parents:
        raise ValueError("output path is outside the output directory")
    return read_output(path)


def record_pty(db_path: Path, argv: list[str], config: dict | None = None) -> int:
    """Run argv in a PTY, keep its exit code, and store a bounded output capture."""
    if config is None:
        from zh.config import load_config

        config = load_config()
    if not argv:
        raise ValueError("command is required")
    command = shlex.join(argv)
    store = rejection_reason(command, config) is None
    limit = int(config.get("output_max_bytes") or 0)
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
    except FileNotFoundError:
        os.close(slave)
        os.close(master)
        raise
    os.close(slave)
    payload, truncated = _read_pty(master, process, store=store, limit=limit)
    code = process.wait()
    if code < 0:
        code = 128 - code
    if store:
        _store(db_path, command, payload, truncated, code)
    return code


def _read_pty(
    master: int,
    process: subprocess.Popen[bytes],
    *,
    store: bool,
    limit: int,
) -> tuple[bytes, bool]:
    """Drain the PTY until the process exits. Returns the kept bytes and a truncation flag."""
    chunks: list[bytes] = []
    total = 0
    truncated = False

    def reader() -> None:
        nonlocal total, truncated
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            if not store:
                continue
            if limit and total >= limit:
                truncated = True
                continue
            if limit and total + len(data) > limit:
                chunks.append(data[: limit - total])
                total = limit
                truncated = True
                continue
            chunks.append(data)
            total += len(data)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    process.wait()
    os.close(master)
    thread.join(timeout=2)
    return b"".join(chunks), truncated


def _store(db_path: Path, command: str, payload: bytes, truncated: bool, exit_code: int) -> None:
    """Write the command and its captured output into the journal."""
    command_id = start(db_path, "", os.getcwd(), command)
    if not command_id:
        return
    directory = output_dir(db_path)
    relative = f"{command_id}.gz"
    write_output(directory, relative, payload)
    path = init_db(db_path)
    connection = connect(path)
    try:
        add_output(
            connection,
            command_id=command_id,
            relative_path=relative,
            size_bytes=len(payload),
            truncated=truncated,
        )
    finally:
        connection.close()
    finish(db_path, command_id, exit_code)
