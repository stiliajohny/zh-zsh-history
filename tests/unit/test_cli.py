"""Tests for the zh command line."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run_zh(*args: str, db: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the module CLI against an optional journal path."""
    command = [sys.executable, "-m", "zh", *args]
    if db is not None:
        command.extend(["--db", str(db)])
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env.pop("ZH_DB", None)
    return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)


def test_init_prints_the_journal_path(tmp_path: Path) -> None:
    """zh init creates the requested database and prints its path."""
    path = tmp_path / "history.sqlite"
    result = run_zh("init", db=path)
    assert result.returncode == 0
    assert result.stdout.strip() == str(path)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_browse_rejects_a_public_host(tmp_path: Path) -> None:
    """zh browse does not bind a non-loopback address."""
    result = run_zh("browse", "--host", "0.0.0.0", "--port", "9", db=tmp_path / "history.sqlite")
    assert result.returncode == 2
    assert "loopback" in result.stderr


def test_missing_command_fails() -> None:
    """The CLI asks for a subcommand."""
    result = run_zh()
    assert result.returncode != 0
