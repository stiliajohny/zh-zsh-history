"""Tests for recording a Zsh command into the journal."""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from zh.capture import load_output
from zh.config import DEFAULTS, save_config
from zh.database import connect

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "zsh" / "zh.plugin.zsh"


def _zh_shell_env(tmp_path: Path, db: Path) -> dict[str, str]:
    """Return an environment whose zh command writes to this journal."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    launcher = bindir / "zh"
    pythonpath = shlex.quote(str(ROOT / "src"))
    python = shlex.quote(sys.executable)
    launcher.write_text(
        f"#!/bin/sh\nexport PYTHONPATH={pythonpath}\nexec {python} -m zh \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(ROOT / "src")
    env["ZH_DB"] = str(db)
    env["ZH_CONFIG"] = str(tmp_path / "config.json")
    env["PS1"] = ""
    env.pop("ZH_PLUGIN_LOADED", None)
    env.pop("ZH_SESSION_ID", None)
    env.pop("ZH_ACTIVE_ID", None)
    return env


def run_zsh(script: str, tmp_path: Path, db: Path) -> subprocess.CompletedProcess[str]:
    """Source the plugin with an isolated journal and a zh command on PATH."""
    return subprocess.run(
        ["zsh", "-f", "-c", script],
        cwd=ROOT,
        env=_zh_shell_env(tmp_path, db),
        capture_output=True,
        text=True,
        check=False,
    )


def _tty_zsh(script: str, env: dict[str, str]) -> str:
    """Run interactive zsh on a terminal so job control can print."""
    if sys.platform == "darwin":
        command = ["script", "-q", "/dev/null", "zsh", "-f", "-i", "-c", script]
    else:
        command = [
            "script",
            "-q",
            "-e",
            "-c",
            "zsh -f -i -c " + shlex.quote(script),
            "/dev/null",
        ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    return result.stdout + result.stderr


@pytest.fixture(scope="module")
def require_zsh() -> None:
    """Stop these tests when the zsh binary is absent."""
    if shutil.which("zsh") is None:
        pytest.fail("zsh is required to record shell commands")


def commands(db: Path) -> list[tuple[str, int | None]]:
    """Return stored command text and exit codes."""
    connection = connect(db)
    try:
        rows = connection.execute(
            "SELECT command, exit_code FROM commands ORDER BY started_at"
        ).fetchall()
    finally:
        connection.close()
    return [(row["command"], row["exit_code"]) for row in rows]


def test_hooks_store_exit_code_and_multiline_text(require_zsh: None, tmp_path: Path) -> None:
    """preexec stores the original command and precmd stores the exit code."""
    db = tmp_path / "history.sqlite"
    plugin = shlex.quote(str(PLUGIN))
    script = f"""
    source {plugin}
    zh_preexec "echo hello" "" "echo hello"
    (exit 3)
    zh_precmd
    zh_preexec $'printf one\\nprintf two' "" $'printf one\\nprintf two'
    (exit 0)
    zh_precmd
    zh_preexec "echo saved" "" "echo saved"
    (exit 4)
    _zh_save_exit
    true
    zh_precmd
    true
    """
    result = run_zsh(script, tmp_path, db)
    assert result.returncode == 0, result.stderr
    assert commands(db) == [
        ("echo hello", 3),
        ("printf one\nprintf two", 0),
        ("echo saved", 4),
    ]
    connection = connect(db)
    try:
        stored = connection.execute("SELECT COUNT(*) FROM outputs").fetchone()[0]
    finally:
        connection.close()
    assert stored == 0


def test_leading_space_is_not_stored(require_zsh: None, tmp_path: Path) -> None:
    """A command that starts with a space is skipped by the default filter."""
    db = tmp_path / "history.sqlite"
    plugin = shlex.quote(str(PLUGIN))
    script = f"""
    source {plugin}
    zh_preexec " secret" "" " secret"
    (exit 0)
    zh_precmd
    """
    result = run_zsh(script, tmp_path, db)
    assert result.returncode == 0, result.stderr
    assert not db.exists() or commands(db) == []


def test_sync_kick_is_not_reported(require_zsh: None, tmp_path: Path) -> None:
    """An interactive shell does not print a job line for the disowned sync kick."""
    db = tmp_path / "history.sqlite"
    plugin = shlex.quote(str(PLUGIN))
    env = _zh_shell_env(tmp_path, db)
    control = _tty_zsh(
        "sleep 0.3 >/dev/null 2>&1 &\nsleep 0.5\nprint -r -- MARKER\nexit\n",
        env,
    )
    assert "done" in control
    output = _tty_zsh(
        f"source {plugin}\n"
        'zh_preexec "true" "" "true"\n'
        "zh_precmd\n"
        "sleep 0.5\n"
        "print -r -- MARKER\n"
        "exit\n",
        env,
    )
    assert "MARKER" in output
    assert "sync-kick" not in output
    assert commands(db) == [("true", 0)]


def _wait_output(db: Path, command: str, timeout: float = 5) -> bytes | None:
    """Poll until a command's capture is stored, or the wait ends."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = connect(db)
        try:
            row = connection.execute(
                """
                SELECT outputs.relative_path
                FROM outputs
                JOIN commands ON commands.id = outputs.command_id
                WHERE commands.command = ?
                """,
                (command,),
            ).fetchone()
        finally:
            connection.close()
        if row is not None:
            return load_output(db, row["relative_path"])
        time.sleep(0.05)
    return None


def test_plugin_stores_stdout_and_stderr(require_zsh: None, tmp_path: Path) -> None:
    """A terminal command keeps stdout and stderr, and the terminal still shows both."""
    db = tmp_path / "history.sqlite"
    plugin = shlex.quote(str(PLUGIN))
    output = _tty_zsh(
        f"source {plugin}\n"
        "_zh_sample() {\n"
        "  cmd='print -r -- ZH_STDOUT_SENTINEL | cat'\n"
        '  zh_preexec "$cmd" "" "$cmd"\n'
        "  print -r -- ZH_STDOUT_SENTINEL | cat\n"
        "  zh_precmd\n"
        "  err='print -u2 ZH_STDERR_SENTINEL'\n"
        '  zh_preexec "$err" "" "$err"\n'
        "  print -u2 ZH_STDERR_SENTINEL\n"
        "  zh_precmd\n"
        "}\n"
        "_zh_sample\n"
        "exit\n",
        _zh_shell_env(tmp_path, db),
    )
    assert "ZH_STDOUT_SENTINEL" in output
    assert "ZH_STDERR_SENTINEL" in output
    stdout = _wait_output(db, "print -r -- ZH_STDOUT_SENTINEL | cat")
    stderr = _wait_output(db, "print -u2 ZH_STDERR_SENTINEL")
    assert stdout is not None and b"ZH_STDOUT_SENTINEL" in stdout
    assert stderr is not None and b"ZH_STDERR_SENTINEL" in stderr


def test_capture_output_false_stores_no_stream(require_zsh: None, tmp_path: Path) -> None:
    """Turning capture off keeps the command and the terminal text, without an output file."""
    db = tmp_path / "history.sqlite"
    save_config({**DEFAULTS, "capture_output": False}, tmp_path / "config.json")
    plugin = shlex.quote(str(PLUGIN))
    output = _tty_zsh(
        f"source {plugin}\n"
        "_zh_sample() {\n"
        '  zh_preexec "echo ZH_VISIBLE_SENTINEL" "" "echo ZH_VISIBLE_SENTINEL"\n'
        "  echo ZH_VISIBLE_SENTINEL\n"
        "  zh_precmd\n"
        "}\n"
        "_zh_sample\n"
        "exit\n",
        _zh_shell_env(tmp_path, db),
    )
    assert "ZH_VISIBLE_SENTINEL" in output
    assert ("echo ZH_VISIBLE_SENTINEL", 0) in commands(db)
    assert _wait_output(db, "echo ZH_VISIBLE_SENTINEL", timeout=1) is None


def test_missing_zh_does_not_stop_the_hook(require_zsh: None, tmp_path: Path) -> None:
    """The shell continues when the zh command is not on PATH."""
    plugin = shlex.quote(str(PLUGIN))
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin"
    env["ZH_DB"] = str(tmp_path / "history.sqlite")
    env.pop("ZH_PLUGIN_LOADED", None)
    result = subprocess.run(
        ["zsh", "-f", "-c", f"source {plugin}; zh_preexec echo hi '' echo; print -r -- continued"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "continued" in result.stdout
