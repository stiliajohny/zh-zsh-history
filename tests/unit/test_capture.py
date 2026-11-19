"""Tests for PTY capture and stored output files."""

from __future__ import annotations

import os
import pty
import select
import signal
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from zh.capture import _copy_master, begin_session_capture, load_output, record_pty
from zh.config import DEFAULTS, save_config
from zh.database import connect, get_command, init_db
from zh.hooks import start


def test_record_pty_keeps_exit_code_and_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A recorded command keeps its exit code and a readable output file."""
    monkeypatch.setenv("ZH_CONFIG", str(tmp_path / "config.json"))
    db = tmp_path / "history.sqlite"
    code = record_pty(db, [sys.executable, "-c", "print('captured')"], dict(DEFAULTS))
    assert code == 0
    connection = connect(db)
    try:
        row = connection.execute("SELECT id, exit_code FROM commands").fetchone()
        assert row is not None
        assert row["exit_code"] == 0
        stored = get_command(connection, row["id"])
        assert stored is not None
        assert "print" in stored["command"]
        output = connection.execute(
            "SELECT relative_path FROM outputs WHERE command_id = ?",
            (row["id"],),
        ).fetchone()
    finally:
        connection.close()
    assert output is not None
    assert b"captured" in load_output(db, output["relative_path"])


def test_record_pty_missing_command(tmp_path: Path) -> None:
    """A missing executable is reported and nothing is stored."""
    db = tmp_path / "history.sqlite"
    init_db(db)
    with pytest.raises(FileNotFoundError):
        record_pty(db, ["zh-missing-command-name"], dict(DEFAULTS))
    connection = connect(db)
    try:
        count = connection.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
    finally:
        connection.close()
    assert count == 0


def test_record_limits_output_and_a_filter_discards_it(tmp_path: Path) -> None:
    """A capture is size-limited, and a rejected command still runs without being stored."""
    db = tmp_path / "history.sqlite"
    config = dict(DEFAULTS)
    config["output_max_bytes"] = 4
    code = record_pty(
        db,
        [sys.executable, "-c", "import sys; sys.stdout.write('abcdefghijklmnopqrstuvwxyz')"],
        config,
    )
    assert code == 0
    connection = connect(db)
    try:
        row = connection.execute(
            "SELECT id, exit_code FROM commands"
        ).fetchone()
        assert row is not None
        stored = connection.execute(
            "SELECT relative_path, size_bytes, truncated FROM outputs WHERE command_id = ?",
            (row["id"],),
        ).fetchone()
    finally:
        connection.close()
    assert stored is not None
    assert int(stored["truncated"]) == 1
    assert int(stored["size_bytes"]) <= 4
    output_file = tmp_path / "outputs" / stored["relative_path"]
    assert stat.S_IMODE(output_file.stat().st_mode) == 0o600
    filtered = dict(DEFAULTS)
    filtered["exclude_regex"] = ["skip-me"]
    code = record_pty(db, [sys.executable, "-c", "print('skip-me')"], filtered)
    assert code == 0
    connection = connect(db)
    try:
        count = connection.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_copy_master_keeps_a_prefix_and_mirrors_the_rest() -> None:
    """The stored copy is capped. The mirror still receives every byte."""
    master, slave = pty.openpty()
    mirror_read, mirror_write = os.pipe()
    box: dict[str, tuple[bytes, bool]] = {}

    def reader() -> None:
        box["value"] = _copy_master(master, mirror_write, 4)

    thread = threading.Thread(target=reader)
    thread.start()
    os.write(slave, b"abcdefghijklmnopqrstuvwxyz")
    mirrored = bytearray()
    deadline = time.monotonic() + 2
    while bytes(mirrored) != b"abcdefghijklmnopqrstuvwxyz" and time.monotonic() < deadline:
        ready, _, _ = select.select([mirror_read], [], [], 0.1)
        if ready:
            mirrored.extend(os.read(mirror_read, 64))
    os.close(slave)
    thread.join(timeout=2)
    os.close(mirror_write)
    os.close(mirror_read)
    payload, truncated = box["value"]
    assert payload == b"abcd"
    assert truncated is True
    assert bytes(mirrored) == b"abcdefghijklmnopqrstuvwxyz"


def test_session_capture_can_be_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A disabled capture leaves stdout alone and stores no slave path."""
    monkeypatch.setenv("ZH_CONFIG", str(tmp_path / "config.json"))
    save_config({**DEFAULTS, "capture_output": False})
    db = tmp_path / "history.sqlite"
    command_id = start(db, "", str(tmp_path), "echo generated")
    assert command_id
    assert begin_session_capture(db, command_id) is None
    assert begin_session_capture(db, "not-a-command") is None


def test_session_capture_stores_bytes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bytes written to the slave are stored for that command."""
    monkeypatch.setenv("ZH_CONFIG", str(tmp_path / "config.json"))
    try:
        tty = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        pytest.skip("no controlling terminal")
    os.close(tty)
    db = tmp_path / "history.sqlite"
    command_id = start(db, "", str(tmp_path), "echo generated")
    assert command_id
    started = begin_session_capture(db, command_id)
    if started is None:
        pytest.skip("capture did not start")
    slave, capture_pid = started
    fd = os.open(slave, os.O_RDWR | os.O_NOCTTY)
    try:
        os.write(fd, b"generated-text\n")
    finally:
        os.close(fd)
    os.kill(capture_pid, signal.SIGUSR1)
    deadline = time.monotonic() + 3
    stored = None
    while time.monotonic() < deadline:
        connection = connect(db)
        try:
            row = connection.execute(
                "SELECT relative_path FROM outputs WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is not None:
            stored = load_output(db, row["relative_path"])
            break
        time.sleep(0.05)
    assert stored is not None
    assert b"generated-text" in stored


def test_load_output_rejects_parent_paths(tmp_path: Path) -> None:
    """Output reads cannot leave the output directory."""
    db = tmp_path / "history.sqlite"
    init_db(db)
    with pytest.raises(ValueError, match="outside"):
        load_output(db, "../history.sqlite")
