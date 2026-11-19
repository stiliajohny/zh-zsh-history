"""Tests for recording a Zsh command into the journal."""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from zh.database import connect

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "zsh" / "zh.plugin.zsh"


def run_zsh(script: str, tmp_path: Path, db: Path) -> subprocess.CompletedProcess[str]:
    """Source the plugin with an isolated journal and a zh command on PATH."""
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
    env.pop("ZH_PLUGIN_LOADED", None)
    env.pop("ZH_SESSION_ID", None)
    env.pop("ZH_ACTIVE_ID", None)
    return subprocess.run(
        ["zsh", "-f", "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


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
