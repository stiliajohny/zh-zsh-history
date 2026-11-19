"""Local configuration for privacy and sync."""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    "sync_enabled": False,
    "server_url": "",
    "device_token": "",
    "device_id": "",
    "ignore_space": True,
    "exclude_regex": [],
    "paused": False,
    "env_allowlist": [],
    "retention_days": None,
    "capture_output": True,
    "output_max_bytes": 1_048_576,
    "tombstone_days": 30,
    "sync_batch_size": 500,
}


def config_path() -> Path:
    """Return the config path from ZH_CONFIG or the XDG config directory."""
    override = os.environ.get("ZH_CONFIG")
    if override:
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "zh" / "config.json"


def load_config(path: Path | None = None) -> dict:
    """Return the saved config merged over the defaults."""
    target = path or config_path()
    data = dict(DEFAULTS)
    if not target.is_file():
        return _normalize(data)
    loaded = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("config must be a JSON object")
    data.update(loaded)
    return _normalize(data)


def save_config(config: dict, path: Path | None = None) -> Path:
    """Write the config with mode 0600. A new parent directory is mode 0700."""
    target = path or config_path()
    parent = target.parent
    created = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if created:
        os.chmod(parent, 0o700)
    payload = _normalize(config)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    os.chmod(target, 0o600)
    return target


def public_config(config: dict) -> dict:
    """Return config safe to print. The device token is redacted."""
    shown = dict(config)
    if shown.get("device_token"):
        shown["device_token"] = "***"
    return shown


def sync_marker_path(db_path: Path) -> Path:
    """Return the file the shell checks before starting a background sync."""
    return db_path.expanduser().parent / "sync-enabled"


def refresh_sync_marker(db_path: Path, config: dict) -> None:
    """Create or remove the shell sync marker to match the config."""
    marker = sync_marker_path(db_path)
    enabled = bool(
        config.get("sync_enabled") and config.get("server_url") and config.get("device_token")
    )
    if enabled:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
        os.chmod(marker, 0o600)
        return
    if marker.exists() and not marker.is_symlink():
        marker.unlink()


def _normalize(config: dict) -> dict:
    merged = dict(DEFAULTS)
    merged.update(config)
    merged["sync_enabled"] = bool(merged["sync_enabled"])
    merged["ignore_space"] = bool(merged["ignore_space"])
    merged["paused"] = bool(merged["paused"])
    merged["capture_output"] = bool(merged["capture_output"])
    merged["exclude_regex"] = [str(item) for item in merged["exclude_regex"]]
    merged["env_allowlist"] = [str(item) for item in merged["env_allowlist"]]
    merged["server_url"] = str(merged["server_url"] or "")
    merged["device_token"] = str(merged["device_token"] or "")
    merged["device_id"] = str(merged["device_id"] or "")
    retention = merged["retention_days"]
    merged["retention_days"] = None if retention in {None, ""} else int(retention)
    merged["output_max_bytes"] = int(merged["output_max_bytes"])
    merged["tombstone_days"] = int(merged["tombstone_days"])
    merged["sync_batch_size"] = max(1, min(500, int(merged["sync_batch_size"])))
    return merged
