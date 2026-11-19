"""Tests for command-line behaviour beyond init and browse."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from zh.schedule import (
    install_units,
    launchd_plist,
    systemd_service,
    systemd_timer,
    uninstall_units,
)

ROOT = Path(__file__).resolve().parents[2]


def run_zh(*args: str, db: Path, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    """Run the module CLI against one journal."""
    command = [sys.executable, "-m", "zh", *args, "--db", str(db)]
    merged = os.environ.copy()
    merged["PYTHONPATH"] = str(ROOT / "src")
    merged.pop("ZH_DB", None)
    if env:
        merged.update(env)
    return subprocess.run(
        command, cwd=ROOT, env=merged, capture_output=True, text=True, check=False
    )


def test_search_json_does_not_run_the_command(tmp_path: Path) -> None:
    """JSON search prints the stored text and does not execute it."""
    database = tmp_path / "history.sqlite"
    marker = tmp_path / "ran"
    command = f"touch {marker}"
    assert run_zh("init", db=database).returncode == 0
    imported = tmp_path / "in.jsonl"
    imported.write_text(
        json.dumps({"command": command, "started_at": "2026-11-18T12:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    result = run_zh("import", str(imported), db=database)
    assert result.returncode == 0
    assert not marker.exists()
    found = run_zh("search", "touch", "--json", db=database)
    assert found.returncode == 0
    assert command in found.stdout
    assert not marker.exists()


def test_fzf_prints_the_selection_without_running_it(tmp_path: Path, monkeypatch) -> None:
    """A fake fzf selects a command line and the process does not run it."""
    database = tmp_path / "history.sqlite"
    marker = tmp_path / "ran"
    imported = tmp_path / "in.jsonl"
    imported.write_text(
        json.dumps(
            {"command": f"touch {marker}", "started_at": "2026-11-18T12:00:00+00:00"}
        )
        + "\n",
        encoding="utf-8",
    )
    run_zh("import", str(imported), db=database)
    fzf = tmp_path / "fzf"
    fzf.write_text("#!/bin/sh\nhead -n 1\n", encoding="utf-8")
    fzf.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    result = run_zh("search", "--fzf", db=database, env={"PATH": os.environ["PATH"]})
    assert result.returncode == 0
    assert f"touch {marker}" in result.stdout
    assert not marker.exists()


def test_doctor_redacts_tokens_and_schedule_round_trip(tmp_path: Path) -> None:
    """Doctor hides the device token, and scheduler files can be installed and removed."""
    database = tmp_path / "history.sqlite"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {"device_token": "secret-token", "sync_enabled": False, "exclude_regex": ["token="]}
        ),
        encoding="utf-8",
    )
    os.chmod(config, 0o600)
    result = run_zh("doctor", db=database, env={"ZH_CONFIG": str(config)})
    assert result.returncode == 0
    assert "secret-token" not in result.stdout
    assert "cannot guarantee" in result.stdout
    home = tmp_path / "home"
    installed = install_units(home, "linux", "zh")
    assert any(path.name == "zh-sync.service" for path in installed)
    service = home / ".config" / "systemd" / "user" / "zh-sync.service"
    assert "ExecStart=zh sync" in service.read_text()
    assert uninstall_units(home, "linux")
    assert install_units(home, "darwin", "zh")
    plist = (home / "Library" / "LaunchAgents" / "com.zh.sync.plist").read_text()
    assert "<string>zh</string>" in plist
    assert launchd_plist("zh") == plist
    assert "ExecStart=zh sync" in systemd_service("zh")
    assert "OnUnitActiveSec=5min" in systemd_timer()
