"""Local checks for the journal, config, and sync settings."""

from __future__ import annotations

import shutil
import sqlite3
import stat
from pathlib import Path
from urllib.parse import urlparse

from zh.config import config_path, load_config, public_config
from zh.database import connect, init_db, sync_counts
from zh.security import REGEX_LIMIT, invalid_patterns


def doctor(db_path: Path) -> tuple[int, list[str]]:
    """Return a process status and lines describing the local install."""
    lines: list[str] = []
    status = 0
    try:
        if db_path.expanduser().exists() and not db_path.expanduser().is_symlink():
            mode = stat.S_IMODE(db_path.expanduser().stat().st_mode)
            if mode != 0o600:
                status = 1
                lines.append(f"Journal mode is {mode:04o}; expected 0600.")
        path = init_db(db_path)
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        return 1, [f"Journal: {exc}"]
    if not any(line.startswith("Journal mode") for line in lines):
        lines.insert(0, "Journal mode is 0600.")
    connection = connect(path)
    try:
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        counts = sync_counts(connection)
    finally:
        connection.close()
    lines.append(f"Journal: {path}")
    lines.append(f"SQLite journal mode: {journal}; schema {version}.")
    lines.append(f"Pending sync events: {counts['pending']}. Cursor: {counts['cursor']}.")
    if counts["last_error"]:
        lines.append(f"Last sync error: {counts['last_error']}")
    if counts["last_success"]:
        lines.append(f"Last sync success: {counts['last_success']}")
    config_file = config_path()
    try:
        config = load_config()
    except (OSError, ValueError) as exc:
        status = 1
        lines.append(f"Config: {exc}")
        config = {}
    if config_file.is_file():
        config_mode = stat.S_IMODE(config_file.stat().st_mode)
        if config_mode != 0o600:
            status = 1
            lines.append(f"Config mode is {config_mode:04o}; expected 0600.")
        else:
            lines.append("Config mode is 0600.")
    if config:
        shown = public_config(config)
        lines.append(f"Sync enabled: {shown.get('sync_enabled')}. Paused: {shown.get('paused')}.")
        capture = "on" if shown.get("capture_output") else "off"
        lines.append(f"Command output capture: {capture}.")
        if shown.get("device_token") == "***":
            lines.append("Device token is stored and is not shown.")
        server = str(shown.get("server_url") or "")
        if server:
            parsed = urlparse(server)
            loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            if parsed.scheme == "http" and not loopback:
                lines.append("Sync URL uses HTTP on a non-loopback host. Use HTTPS.")
            elif parsed.scheme == "https":
                lines.append("Sync URL uses HTTPS.")
        patterns = list(config.get("exclude_regex") or [])
        if patterns:
            lines.append(REGEX_LIMIT)
        invalid = invalid_patterns(patterns)
        if invalid:
            lines.append(f"Ignored invalid exclude patterns: {', '.join(invalid)}")
    if shutil.which("zsh"):
        lines.append(
            "zsh is installed. Source zsh/zh.plugin.zsh "
            "or link that directory as an Oh My Zsh plugin."
        )
    else:
        lines.append("zsh was not found on PATH.")
    lines.append("ZH does not change HISTFILE or history keybindings.")
    return status, lines
