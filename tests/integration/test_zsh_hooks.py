"""ZSH integration for hooks, history, and aliases."""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_plugin_preserves_history_and_records_exit_codes(tmp_path: Path) -> None:
    """The plugin records commands, keeps keybindings, and does not rewrite HISTFILE."""
    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh is not installed")
    env, plugin = _zsh_env(tmp_path)
    database = Path(env["ZH_DB"])
    histfile = tmp_path / "zsh_history"
    histfile.write_text("sentinel\n", encoding="utf-8")
    _check_zsh_syntax(zsh, plugin)
    script = "\n".join(
        [
            "PS1=",
            "PS2=",
            f"HISTFILE={_quote(histfile)}",
            "before=$(bindkey -L)",
            "alias zhs='echo kept'",
            "typeset -g OTHER=0",
            "other_precmd() { typeset -g OTHER=1 }",
            "autoload -Uz add-zsh-hook",
            "add-zsh-hook precmd other_precmd",
            f"source {_quote(plugin)}",
            "after=$(bindkey -L)",
            "[[ $before == $after ]] || { print -u2 bindings-changed; exit 3 }",
            "[[ $(alias zhs) == *kept* ]] || { print -u2 alias-replaced; exit 4 }",
            f"source {_quote(plugin)}",
            "n=0",
            "for item in $precmd_functions; do",
            "  [[ $item == zh_precmd ]] && n=$((n + 1))",
            "done",
            "[[ $n -eq 1 ]] || { print -u2 hook-loaded-twice; exit 5 }",
            "false",
            'print -r -- "EXIT:$?"',
            'print -r -- "OTHER:$OTHER"',
            "echo 'one",
            "two'",
            " echo hidden",
            'print -r -- "FILE:$(<$HISTFILE)"',
            "exit",
        ]
    )
    completed = subprocess.run(
        [zsh, "-f", "-i"],
        input=script,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "EXIT:1" in completed.stdout
    assert "OTHER:1" in completed.stdout
    assert "FILE:sentinel" in completed.stdout
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT command, exit_code FROM commands ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()
    commands = [row[0] for row in rows]
    assert "false" in commands
    assert any("one" in command and "two" in command for command in commands)
    assert all(not str(command).startswith(" ") for command in commands)
    false_row = next(row for row in rows if row[0] == "false")
    assert false_row[1] == 1
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_remote_command_enters_session_history(tmp_path: Path) -> None:
    """An imported command is visible to fc after the next prompt."""
    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh is not installed")
    from zh.database import connect, init_db, record_command

    env, plugin = _zsh_env(tmp_path)
    database = init_db(Path(env["ZH_DB"]))
    journal = connect(database)
    record_command(
        journal,
        command="echo from-remote",
        started_at="2026-11-18T12:00:00+00:00",
        exit_code=0,
        origin="import",
    )
    journal.close()
    script = "\n".join(
        [
            "PS1=",
            "PS2=",
            f"source {_quote(plugin)}",
            "true",
            "print -r -- HISTORY-START",
            "fc -l || true",
            "print -r -- HISTORY-END",
            "exit",
        ]
    )
    completed = subprocess.run(
        [zsh, "-f", "-i"],
        input=script,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    history = completed.stdout.split("HISTORY-START", 1)[-1].split("HISTORY-END", 1)[0]
    assert "echo from-remote" in history


def _zsh_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Point an interactive shell at the local zh package."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "zh"
    wrapper.write_text(
        f"#!/bin/sh\nexport PYTHONPATH='{ROOT / 'src'}'\nexec '{sys.executable}' -m zh \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["ZH_DB"] = str(tmp_path / "history.sqlite")
    env["ZH_CONFIG"] = str(tmp_path / "config.json")
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PS1"] = ""
    return env, ROOT / "zsh" / "zh.plugin.zsh"


def _check_zsh_syntax(zsh: str, plugin: Path) -> None:
    """Reject a plugin file that zsh cannot parse."""
    for path in (
        plugin,
        ROOT / "zsh" / "hooks.zsh",
        ROOT / "zsh" / "history.zsh",
        ROOT / "zsh" / "completions" / "_zh",
    ):
        checked = subprocess.run(
            [zsh, "-n", str(path)], capture_output=True, text=True, check=False
        )
        assert checked.returncode == 0, checked.stderr


def _quote(path: Path) -> str:
    """Quote a path for a zsh single-quoted string."""
    return "'" + str(path).replace("'", "'\\''") + "'"
