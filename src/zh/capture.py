"""PTY recording and stored output files."""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shlex
import signal
import struct
import subprocess
import termios
import threading
from pathlib import Path

from zh.database import add_output, connect, init_db
from zh.hooks import attach_output, finish, start
from zh.output import output_dir, read_output, write_output
from zh.security import rejection_reason

_COMMAND_ID = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


def _ignore_stop(signum: int, frame: object) -> None:
    """Keep SIGUSR1 from killing the recorder. The wakeup pipe ends the read."""
    del signum, frame


def load_output(db_path: Path, relative_path: str) -> bytes:
    """Return one stored capture. The path must stay inside the output directory."""
    root = output_dir(db_path).resolve()
    path = (root / relative_path).resolve()
    if path != root and root not in path.parents:
        raise ValueError("output path is outside the output directory")
    return read_output(path)


def begin_session_capture(db_path: Path, command_id: str) -> tuple[str, int] | None:
    """Start a PTY copy of one command. Return the slave path and the recorder pid.

    The recorder keeps one slave descriptor open so the PTY does not close before the shell
    redirects onto it. The shell sends SIGUSR1 after the command. None means do not redirect.
    """
    if not _COMMAND_ID.fullmatch(command_id):
        return None
    from zh.config import load_config

    config = load_config()
    if not config.get("capture_output"):
        return None
    try:
        tty = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return None
    try:
        master, slave = pty.openpty()
    except OSError:
        os.close(tty)
        return None
    try:
        slave_name = os.ttyname(slave)
        _copy_winsize(tty, master)
        _copy_termios(tty, slave)
        ready_read, ready_write = os.pipe()
    except OSError:
        os.close(master)
        os.close(slave)
        os.close(tty)
        return None
    try:
        pid = os.fork()
    except OSError:
        os.close(master)
        os.close(slave)
        os.close(tty)
        os.close(ready_read)
        os.close(ready_write)
        return None
    if pid == 0:
        os.close(ready_read)
        wake_read, wake_write = os.pipe()
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGQUIT, signal.SIG_IGN)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            signal.signal(signal.SIGUSR1, _ignore_stop)
            os.set_blocking(wake_write, False)
            signal.set_wakeup_fd(wake_write)
            _redirect_child_stdio(tty)
            os.write(ready_write, b"1")
            os.close(ready_write)
            payload, truncated = _copy_until_signal(
                master,
                1,
                wake_read,
                int(config.get("output_max_bytes") or 0),
            )
            os.close(wake_read)
            os.close(wake_write)
            os.close(slave)
            attach_output(db_path, command_id, payload, truncated)
        except Exception:
            pass
        os._exit(0)
    os.close(master)
    os.close(slave)
    os.close(tty)
    os.close(ready_write)
    ready = _wait_ready(ready_read)
    os.close(ready_read)
    if not ready:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        return None
    return slave_name, pid


def _copy_winsize(src: int, dst: int) -> None:
    """Copy the terminal size onto a PTY so column-aware commands match the window."""
    try:
        packed = fcntl.ioctl(src, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        fcntl.ioctl(dst, termios.TIOCSWINSZ, packed)
    except OSError:
        return


def _copy_termios(src: int, dst: int) -> None:
    """Copy terminal settings onto the slave before the shell redirects onto it."""
    try:
        termios.tcsetattr(dst, termios.TCSANOW, termios.tcgetattr(src))
    except termios.error:
        return


def _redirect_child_stdio(tty: int) -> None:
    """Point the recorder at the real terminal and drop the shell's pipes."""
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    if null > 2:
        os.close(null)
    os.dup2(tty, 1)
    os.dup2(tty, 2)
    if tty > 2:
        os.close(tty)


def _wait_ready(fd: int) -> bool:
    """Wait until the recorder has left the shell's process group."""
    readable, _, _ = select.select([fd], [], [], 1)
    if not readable:
        return False
    try:
        return os.read(fd, 1) == b"1"
    except OSError:
        return False


def _copy_until_signal(master: int, mirror: int, wake: int, limit: int) -> tuple[bytes, bool]:
    """Mirror PTY output until the shell signals, then keep bytes still buffered."""
    chunks: list[bytes] = []
    total = 0
    truncated = False

    def take(data: bytes) -> None:
        nonlocal total, truncated
        _write_all(mirror, data)
        if limit and total >= limit:
            truncated = True
            return
        if limit and total + len(data) > limit:
            chunks.append(data[: limit - total])
            total = limit
            truncated = True
            return
        chunks.append(data)
        total += len(data)

    try:
        while True:
            readable, _, _ = select.select([master, wake], [], [])
            if wake in readable:
                break
            if master not in readable:
                continue
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            take(data)
        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            take(data)
    finally:
        os.close(master)
    return b"".join(chunks), truncated


def _copy_master(master: int, mirror: int, limit: int) -> tuple[bytes, bool]:
    """Read a PTY until it closes. Mirror every byte and keep at most `limit`."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    try:
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            _write_all(mirror, data)
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
    finally:
        os.close(master)
    return b"".join(chunks), truncated


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte. A closed terminal stops the mirror and keeps the stored copy."""
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except OSError:
            return
        if written <= 0:
            return
        view = view[written:]


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
