"""macOS and Linux user schedulers for `zh sync`."""

from __future__ import annotations

from pathlib import Path


def install_units(home: Path, platform_name: str, executable: str) -> list[Path]:
    """Write a user scheduler that runs `zh sync` and does not start at shell prompt time."""
    if platform_name == "darwin":
        directory = home / "Library" / "LaunchAgents"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "com.zh.sync.plist"
        path.write_text(launchd_plist(executable), encoding="utf-8")
        return [path]
    if platform_name == "linux":
        directory = home / ".config" / "systemd" / "user"
        directory.mkdir(parents=True, exist_ok=True)
        service = directory / "zh-sync.service"
        timer = directory / "zh-sync.timer"
        service.write_text(systemd_service(executable), encoding="utf-8")
        timer.write_text(systemd_timer(), encoding="utf-8")
        return [service, timer]
    raise ValueError("scheduler support is limited to macOS and Linux")


def uninstall_units(home: Path, platform_name: str) -> list[Path]:
    """Remove scheduler files written by install_units."""
    if platform_name == "darwin":
        paths = [home / "Library" / "LaunchAgents" / "com.zh.sync.plist"]
    elif platform_name == "linux":
        directory = home / ".config" / "systemd" / "user"
        paths = [directory / "zh-sync.service", directory / "zh-sync.timer"]
    else:
        raise ValueError("scheduler support is limited to macOS and Linux")
    removed = []
    for path in paths:
        if path.is_file() and not path.is_symlink():
            path.unlink()
            removed.append(path)
    return removed


def systemd_service(executable: str) -> str:
    """Return a systemd user service that runs one sync."""
    return (
        "[Unit]\n"
        "Description=Sync the ZH command journal\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={executable} sync\n"
    )


def systemd_timer() -> str:
    """Return a systemd user timer with a five minute cadence."""
    return (
        "[Unit]\n"
        "Description=Sync the ZH command journal on a timer\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=2min\n"
        "OnUnitActiveSec=5min\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def launchd_plist(executable: str) -> str:
    """Return a launchd agent that runs one sync every five minutes."""
    escaped = (
        executable.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        "  <string>com.zh.sync</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"    <string>{escaped}</string>\n"
        "    <string>sync</string>\n"
        "  </array>\n"
        "  <key>StartInterval</key>\n"
        "  <integer>300</integer>\n"
        "  <key>RunAtLoad</key>\n"
        "  <false/>\n"
        "</dict>\n"
        "</plist>\n"
    )
