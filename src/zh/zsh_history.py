"""Read a Zsh history file into the local journal.

The reader follows Zsh ``readhistline`` and ``readhistfile``: a backslash at the
end of a physical line is a newline inside the command, a backslash followed
by spaces is a trailing backslash plus one saved space, and byte ``0x83``
starts a metafied character. ``HISTFILE`` is only read.
"""

from __future__ import annotations

import getpass
import os
import socket
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zh.config import load_config
from zh.database import ensure_local_device, get_command, record_command
from zh.security import rejection_reason

_META = 0x83
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "zh/zsh-history/v1")
_MAX_COMMAND = 100_000


@dataclass(frozen=True)
class HistoryEntry:
    """One command recovered from a Zsh history file."""

    command: str
    started: int | None
    elapsed: int | None


def default_zsh_history_path() -> Path:
    """Return ``HISTFILE`` when it is set, otherwise ``~/.zsh_history``."""
    override = os.environ.get("HISTFILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".zsh_history"


def parse_zsh_history(data: bytes) -> list[HistoryEntry]:
    """Return commands in file order, with timestamps when the file stored them."""
    entries: list[HistoryEntry] = []
    for raw in _logical_lines(data):
        text = _unmetafy(raw).decode("utf-8", errors="replace")
        started, elapsed, command = _split_entry(text)
        if command == "":
            continue
        entries.append(HistoryEntry(command, started, elapsed))
    return entries


def import_zsh_history(
    connection: sqlite3.Connection,
    history_path: Path,
    *,
    journal: Path | None = None,
    config: dict | None = None,
) -> int:
    """Store new commands from a Zsh history file. Returns how many were added.

    The same file can be imported again without duplicating those commands.
    A command that the privacy filters reject is left out. Pause does not
    block this import. The history file is not opened for writing.
    """
    path = history_path.expanduser()
    if path.is_dir():
        raise ValueError("Zsh history path is a directory")
    if journal is not None and _same_file(path, journal):
        raise ValueError("Zsh history path is the journal file")
    if not path.is_file():
        raise ValueError(f"Zsh history file was not found: {path}")
    data = path.read_bytes()
    if data.startswith(b"SQLite format 3"):
        raise ValueError("Zsh history path is a SQLite database")
    entries = parse_zsh_history(data)
    if not entries:
        return 0
    settings = dict(config if config is not None else load_config())
    settings["paused"] = False
    started_at = _assign_started(entries, _file_mtime(path))
    device_id = ensure_local_device(connection)
    hostname, username = _who()
    seen: dict[tuple[int | None, int | None, str], int] = {}
    added = 0
    for entry, started in zip(entries, started_at, strict=True):
        key = (entry.started, entry.elapsed, entry.command)
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        if len(entry.command) > _MAX_COMMAND:
            continue
        if rejection_reason(entry.command, settings):
            continue
        command_id = _entry_id(entry, occurrence)
        if get_command(connection, command_id) is not None:
            continue
        ended, duration_ms, infer = _finish(started, entry.elapsed)
        try:
            record_command(
                connection,
                command=entry.command,
                started_at=started.isoformat(),
                ended_at=ended.isoformat(),
                duration_ms=duration_ms,
                origin="import",
                command_id=command_id,
                device_id=device_id,
                hostname=hostname,
                username=username,
                import_kind="zsh",
                infer_duration=infer,
            )
        except sqlite3.IntegrityError:
            continue
        added += 1
    return added


def _entry_id(entry: HistoryEntry, occurrence: int) -> str:
    """Return a stable id for one history occurrence."""
    stamp = "" if entry.started is None else str(entry.started)
    elapsed = "" if entry.elapsed is None else str(entry.elapsed)
    raw = "\0".join((stamp, elapsed, str(occurrence), entry.command))
    return str(uuid.uuid5(_NAMESPACE, raw))


def _finish(started: datetime, elapsed: int | None) -> tuple[datetime, int | None, bool]:
    """Return the end time, duration in milliseconds, and whether to infer it."""
    if elapsed is None:
        return started, None, False
    return started + timedelta(seconds=elapsed), elapsed * 1000, True


def _assign_started(entries: list[HistoryEntry], anchor: datetime) -> list[datetime]:
    """Give every entry a UTC start time, keeping real timestamps when present."""
    known: list[datetime | None] = [
        None if entry.started is None else datetime.fromtimestamp(entry.started, UTC)
        for entry in entries
    ]
    if all(item is None for item in known):
        base = anchor - timedelta(seconds=len(entries) - 1)
        return [base + timedelta(seconds=index) for index in range(len(entries))]
    result: list[datetime | None] = list(known)
    index = 0
    while index < len(result):
        if result[index] is not None:
            index += 1
            continue
        end = index
        while end < len(result) and result[end] is None:
            end += 1
        previous = result[index - 1] if index else None
        following = result[end] if end < len(result) else None
        slots = end - index
        if previous is None and following is not None:
            for offset in range(slots):
                result[index + offset] = following - timedelta(seconds=slots - offset)
        elif following is None and previous is not None:
            for offset in range(slots):
                result[index + offset] = previous + timedelta(seconds=offset + 1)
        elif previous is not None and following is not None:
            gap = (following - previous).total_seconds()
            for offset in range(slots):
                if gap > slots:
                    result[index + offset] = previous + timedelta(seconds=offset + 1)
                else:
                    result[index + offset] = previous + timedelta(microseconds=offset + 1)
        else:
            result[index] = anchor
        index = end
    return [item if item is not None else anchor for item in result]


def _logical_lines(data: bytes) -> list[bytes]:
    """Join physical lines the way Zsh ``readhistline`` does."""
    lines: list[bytes] = []
    buf = bytearray()
    index = 0
    size = len(data)
    while index < size:
        newline = data.find(b"\n", index)
        if newline < 0:
            buf.extend(data[index:])
            if buf:
                lines.append(bytes(buf))
            return lines
        buf.extend(data[index:newline])
        index = newline + 1
        if buf.endswith(b"\\"):
            buf[-1] = ord("\n")
            if index >= size:
                lines.append(_strip_saved_space(buf))
                return lines
            continue
        lines.append(_strip_saved_space(buf))
        buf = bytearray()
    return lines


def _strip_saved_space(buf: bytearray) -> bytes:
    """Drop the space Zsh adds when a command ends with a backslash."""
    if not buf:
        return b""
    last = len(buf) - 1
    space = last
    while space >= 0 and buf[space] == 0x20:
        space -= 1
    if space != last and space >= 0 and buf[space] == 0x5C:
        del buf[-1]
    return bytes(buf)


def _unmetafy(data: bytes) -> bytes:
    """Decode Zsh meta bytes. ``0x83`` followed by ``byte ^ 0x20`` is one byte."""
    out = bytearray()
    index = 0
    size = len(data)
    while index < size:
        byte = data[index]
        if byte == _META and index + 1 < size:
            out.append(data[index + 1] ^ 0x20)
            index += 2
            continue
        out.append(byte)
        index += 1
    return bytes(out)


def _split_entry(text: str) -> tuple[int | None, int | None, str]:
    """Split one logical line into a start time, elapsed seconds, and command."""
    if text.startswith("\\:"):
        return None, None, text[1:]
    if not text.startswith(":"):
        return None, None, text
    body = text[1:]
    stim = _zstrtol(body)
    colon = body.find(":")
    if colon < 0:
        return None, None, ""
    elapsed_raw = _zstrtol(body[colon + 1 :])
    semi = body.find(";", colon + 1)
    command = "" if semi < 0 else body[semi + 1 :]
    if stim <= 0:
        return None, None, command
    if elapsed_raw < stim:
        elapsed = elapsed_raw if elapsed_raw > 0 else 0
    else:
        elapsed = elapsed_raw - stim
    return stim, elapsed, command


def _zstrtol(text: str) -> int:
    """Parse one integer the way Zsh ``zstrtol`` does with base 0."""
    index = 0
    while index < len(text) and text[index] in " \t":
        index += 1
    if index >= len(text):
        return 0
    sign = 1
    if text[index] == "-":
        sign = -1
        index += 1
    elif text[index] == "+":
        index += 1
    if index >= len(text):
        return 0
    base = 10
    if text[index] == "0" and index + 1 < len(text) and text[index + 1] in "xX":
        base = 16
        index += 2
    elif text[index] == "0":
        base = 8
        index += 1
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    value = 0
    while index < len(text):
        digit = digits.find(text[index].lower())
        if digit < 0 or digit >= base:
            break
        value = value * base + digit
        index += 1
    return sign * value


def _file_mtime(path: Path) -> datetime:
    """Return the history file's modification time, truncated to a second."""
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).replace(microsecond=0)


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _who() -> tuple[str | None, str | None]:
    """Return this machine's hostname and the current user, when available."""
    try:
        hostname = socket.gethostname() or None
    except OSError:
        hostname = None
    try:
        username = getpass.getuser() or None
    except OSError:
        username = None
    return hostname, username
