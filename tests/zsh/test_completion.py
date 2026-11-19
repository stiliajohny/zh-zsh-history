"""Tests for zh tab completion."""

from __future__ import annotations

import os
import pty
import select
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "zsh" / "zh.plugin.zsh"
COMPLETION = ROOT / "zsh" / "completions" / "_zh"


def run_zsh(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run Zsh without the user startup files."""
    env = os.environ.copy()
    env.pop("ZH_PLUGIN_LOADED", None)
    env["ZDOTDIR"] = str(tmp_path)
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
        pytest.fail("zsh is required for zh completion")


def test_completion_file_parses(require_zsh: None, tmp_path: Path) -> None:
    """The completion file is valid Zsh."""
    result = run_zsh(f"zsh -n {shlex.quote(str(COMPLETION))}", tmp_path)
    assert result.returncode == 0, result.stderr


def test_compinit_after_plugin_finds_the_completer(require_zsh: None, tmp_path: Path) -> None:
    """Loading the plugin before compinit still registers zh, and a second load is a no-op."""
    plugin = shlex.quote(str(PLUGIN))
    dump = shlex.quote(str(tmp_path / ".zcompdump"))
    script = f"""
    source {plugin}
    first=${{#fpath}}
    source {plugin}
    second=${{#fpath}}
    autoload -Uz compinit
    compinit -u -d {dump}
    print -r -- "registered=${{_comps[zh]}}"
    print -r -- "hook=${{_comps[_hook]-}}"
    print -r -- "fpath=$first/$second"
    """
    result = run_zsh(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "registered=_zh" in result.stdout
    assert "hook=" in result.stdout
    assert "hook=_zh" not in result.stdout
    assert "fpath=" in result.stdout
    first, second = result.stdout.split("fpath=", 1)[1].splitlines()[0].split("/")
    assert first == second


def test_plugin_after_compinit_completes_commands_and_flags(
    require_zsh: None, tmp_path: Path
) -> None:
    """Tab after zh lists subcommands, and tab after a dash lists that command's flags."""
    text = _complete(
        tmp_path,
        [
            "zh \t",
            "zh search -\t",
            "zh search --status \t",
        ],
    )
    assert "search" in text
    assert "_hook" not in text
    for flag in ("--cwd", "--db", "--fzf", "--status"):
        assert flag in text
    for status in ("all", "failed", "ok", "running"):
        assert status in text


def _complete(tmp_path: Path, commands: list[str]) -> str:
    """Return the terminal text from one Zsh session after each completion command."""
    dump = tmp_path / ".zcompdump"
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("zsh", ["zsh", "-f"])
    try:
        _send(fd, "PS1=ZHPROMPT\n")
        _send(fd, "unset zle_bracketed_paste\n")
        _send(fd, "autoload -Uz compinit\n")
        _send(fd, f"compinit -u -d {shlex.quote(str(dump))}\n")
        _send(fd, f"source {shlex.quote(str(PLUGIN))}\n")
        _send(fd, "print ZHCOMP-DONE\n")
        _read_until(fd, b"\nZHCOMP-DONE\n", 8)
        chunks = []
        for command in commands:
            _send(fd, command)
            chunks.append(_read_idle(fd, 2.5))
            _send(fd, "\x15")
            _read_idle(fd, 0.4)
        return b"".join(chunks).decode("utf-8", "replace")
    finally:
        os.close(fd)


def _send(fd: int, text: str) -> None:
    """Write one line to the Zsh pty."""
    os.write(fd, text.encode())


def _read_until(fd: int, token: bytes, timeout: float) -> bytes:
    """Read pty output until a token appears or the deadline passes."""
    end = time.time() + timeout
    data = b""
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        data += os.read(fd, 16384)
        if token in data:
            return data
    return data


def _read_idle(fd: int, timeout: float) -> bytes:
    """Read pty output until it stays quiet or the deadline passes."""
    end = time.time() + timeout
    data = b""
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.25)
        if not ready:
            if data:
                return data
            continue
        data += os.read(fd, 16384)
    return data
