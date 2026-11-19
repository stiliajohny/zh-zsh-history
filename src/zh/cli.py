"""Command line for the local journal, browser, and sync client."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zh.analytics import failed_commands, recent_commands, stats, top_commands
from zh.browser import serve
from zh.capture import load_output, record_pty
from zh.config import load_config, public_config, refresh_sync_marker, save_config
from zh.database import (
    connect,
    default_db_path,
    get_command,
    init_db,
    iter_commands,
    list_devices,
    list_outputs,
    record_command,
    sync_counts,
)
from zh.doctor import doctor
from zh.hooks import finish as hook_finish
from zh.hooks import mark_history, pending_history, session, start
from zh.output import output_dir
from zh.schedule import install_units, uninstall_units
from zh.search import search_commands
from zh.security import REGEX_LIMIT
from zh.sync import (
    SyncError,
    list_remote_devices,
    register_device,
    revoke_device,
    sync_kick,
    sync_once,
)
from zh.zsh_history import default_zsh_history_path, import_zsh_history

SCALAR_KEYS = {
    "sync_enabled",
    "server_url",
    "ignore_space",
    "retention_days",
    "output_max_bytes",
    "tombstone_days",
    "sync_batch_size",
    "device_token",
    "device_id",
}
LIST_KEYS = {"exclude_regex", "env_allowlist"}
BOOL_KEYS = {"sync_enabled", "ignore_space", "paused"}


def main(argv: list[str] | None = None) -> int:
    """Run one zh command and return a process status."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    except (OSError, sqlite3.DatabaseError) as exc:
        print(exc, file=sys.stderr)
        return 1
    except SyncError as exc:
        print(exc, file=sys.stderr)
        return 1
    except ImportError:
        print("Install the sync extra before using the remote API.", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    database = args.db if args.db is not None else default_db_path()
    command = args.command
    if command == "init":
        print(init_db(database), flush=True)
        return 0
    if command == "browse":
        serve(database, args.host, args.port)
        return 0
    if command == "search":
        return _search(args, database)
    if command == "recent":
        return _query(database, args.json, lambda connection: recent_commands(connection)["items"])
    if command == "failed":
        return _query(database, args.json, lambda connection: failed_commands(connection)["items"])
    if command == "top":
        return _top(database, args.json)
    if command == "stats":
        return _stats(database, args.json)
    if command == "show":
        return _show(args, database)
    if command == "record":
        return _record(args, database)
    if command == "sync":
        return _sync(args, database)
    if command == "status":
        return _status(database, args.json)
    if command == "devices":
        return _devices(args, database)
    if command == "config":
        return _config(args, database)
    if command == "pause":
        return _set_paused(database, True)
    if command == "resume":
        return _set_paused(database, False)
    if command == "doctor":
        status, lines = doctor(database)
        print("\n".join(lines))
        return status
    if command == "clean":
        return _clean(args, database)
    if command == "export":
        return _export(args, database)
    if command == "import":
        return _import(args, database)
    if command == "_hook":
        return _hook(args, database)
    print("unknown command", file=sys.stderr)
    return 2


def _search(args: argparse.Namespace, database: Path) -> int:
    path = init_db(database)
    connection = connect(path)
    try:
        body = search_commands(
            connection,
            query=args.query or "",
            status=args.status,
            limit=args.limit,
            since=args.since,
            until=args.until,
            device_id=args.device,
            cwd=args.cwd,
            exit_code=args.exit_code,
        )
    finally:
        connection.close()
    return _emit_commands(body["items"], as_json=args.json, use_fzf=args.fzf and not args.json)


def _query(database: Path, as_json: bool, loader) -> int:
    path = init_db(database)
    connection = connect(path)
    try:
        items = loader(connection)
    finally:
        connection.close()
    return _emit_commands(items, as_json=as_json, use_fzf=False)


def _top(database: Path, as_json: bool) -> int:
    path = init_db(database)
    connection = connect(path)
    try:
        items = top_commands(connection)
    finally:
        connection.close()
    if as_json:
        print(json.dumps(items, ensure_ascii=False))
        return 0
    if not items:
        print("No commands.")
        return 0
    for item in items:
        print(f"{item['count']}\t{item['command']}")
    return 0


def _stats(database: Path, as_json: bool) -> int:
    path = init_db(database)
    connection = connect(path)
    try:
        body = stats(connection)
    finally:
        connection.close()
    if as_json:
        print(json.dumps(body, ensure_ascii=False))
        return 0
    print(
        f"{body['total']} commands, {body['failed']} failed, "
        f"{body['succeeded']} succeeded, {body['running']} running, "
        f"{body.get('unknown', 0)} with no exit code"
    )
    return 0


def _show(args: argparse.Namespace, database: Path) -> int:
    path = init_db(database)
    connection = connect(path)
    try:
        body = get_command(connection, args.command_id)
        outputs = list_outputs(connection, args.command_id) if body else []
    finally:
        connection.close()
    if body is None:
        print("command not found", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(body, ensure_ascii=False))
    else:
        print(body["command"])
        print(f"id: {body['id']}")
        print(f"exit: {_exit_label(body)}")
        print(f"directory: {body['cwd'] or ''}")
        print(f"host: {body.get('hostname') or ''}")
        print(f"user: {body.get('username') or ''}")
        print(f"started: {body['started_at']}")
    if args.output:
        for output in outputs:
            sys.stdout.buffer.write(load_output(path, output["relative_path"]))
    return 0


def _record(args: argparse.Namespace, database: Path) -> int:
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise ValueError("command is required")
    try:
        return record_pty(database, argv, load_config())
    except FileNotFoundError:
        print(f"command not found: {argv[0]}", file=sys.stderr)
        return 127


def _sync(args: argparse.Namespace, database: Path) -> int:
    if args.sync_action in {"install", "uninstall"}:
        home = args.home or Path.home()
        platform_name = args.platform or sys.platform
        if args.sync_action == "install":
            executable = shutil.which("zh") or f"{sys.executable} -m zh"
            written = install_units(home, platform_name, executable)
        else:
            written = uninstall_units(home, platform_name)
        for path in written:
            print(path)
        return 0
    result = sync_once(database)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif result["status"] == "ok":
        print(f"synced pushed={result['pushed']} pulled={result['pulled']}")
    else:
        message = result["status"]
        if result["status"] == "error":
            message = result.get("error", "sync failed")
        print(message)
    return 0 if result["status"] != "error" else 1


def _status(database: Path, as_json: bool) -> int:
    path = init_db(database)
    connection = connect(path)
    try:
        counts = sync_counts(connection)
    finally:
        connection.close()
    config = public_config(load_config())
    body = {"journal": str(path), **counts, "config": config}
    if as_json:
        print(json.dumps(body, ensure_ascii=False))
        return 0
    print(f"Journal: {path}")
    print(f"Pending: {counts['pending']}")
    print(f"Cursor: {counts['cursor']}")
    print(f"Sync enabled: {config['sync_enabled']}")
    print(f"Paused: {config['paused']}")
    if counts["last_error"]:
        print(f"Last error: {counts['last_error']}")
    return 0


def _devices(args: argparse.Namespace, database: Path) -> int:
    config = load_config()
    if args.devices_action == "register":
        server = args.server or config.get("server_url") or ""
        token = args.bootstrap_token or ""
        if not token:
            raise ValueError("pass --bootstrap-token")
        if not server:
            raise ValueError("pass --server")
        registered = register_device(server, token, args.name, socket.gethostname())
        config.update(
            server_url=server,
            device_token=registered["token"],
            device_id=registered["device_id"],
            sync_enabled=True,
        )
        save_config(config)
        refresh_sync_marker(database, config)
        print(registered["device_id"])
        print("Device token saved to the config file.")
        return 0
    if args.devices_action == "revoke":
        if not args.device_id:
            raise ValueError("device id is required")
        revoke_device(config["server_url"], config["device_token"], args.device_id)
        print(args.device_id)
        return 0
    if config.get("sync_enabled") and config.get("server_url") and config.get("device_token"):
        try:
            remote = list_remote_devices(config["server_url"], config["device_token"])
        except SyncError as exc:
            print(exc, file=sys.stderr)
            remote = []
        path = init_db(database)
        connection = connect(path)
        try:
            from zh.database import upsert_remote_device

            for device in remote:
                upsert_remote_device(
                    connection,
                    device_id=device["id"],
                    name=device.get("name") or device["id"],
                    hostname=device.get("hostname"),
                    revoked_at=device.get("revoked_at"),
                )
            devices = list_devices(connection)
        finally:
            connection.close()
    else:
        path = init_db(database)
        connection = connect(path)
        try:
            devices = list_devices(connection)
        finally:
            connection.close()
    if args.json:
        print(json.dumps(devices, ensure_ascii=False))
        return 0
    for device in devices:
        state = "revoked" if device["revoked_at"] else "active"
        kind = "local" if not device["remote"] else "remote"
        print(f"{device['id']}  {kind}  {state}  {device['name']}")
    return 0


def _config(args: argparse.Namespace, database: Path) -> int:
    config = load_config()
    action = args.config_action
    if action is None:
        shown = public_config(config)
        print(json.dumps(shown, indent=2, sort_keys=True))
        return 0
    if action == "get":
        if args.key not in config:
            raise ValueError("unknown setting")
        value = public_config(config)[args.key]
        print(json.dumps(value, ensure_ascii=False))
        return 0
    if action == "unset":
        if args.key not in LIST_KEYS:
            raise ValueError("only list settings can be unset")
        pattern = args.value
        if pattern is None:
            raise ValueError("value is required")
        config[args.key] = [item for item in config[args.key] if item != pattern]
    elif action == "set":
        if args.key is None or args.value is None:
            raise ValueError("key and value are required")
        if args.key in LIST_KEYS:
            if args.value not in config[args.key]:
                config[args.key].append(args.value)
            if args.key == "exclude_regex":
                print(REGEX_LIMIT, file=sys.stderr)
        elif args.key in SCALAR_KEYS:
            config[args.key] = _coerce(args.key, args.value)
        else:
            raise ValueError("unknown setting")
    else:
        raise ValueError("unknown config action")
    save_config(config)
    refresh_sync_marker(database, load_config())
    return 0


def _set_paused(database: Path, paused: bool) -> int:
    config = load_config()
    config["paused"] = paused
    save_config(config)
    refresh_sync_marker(database, config)
    print("paused" if paused else "resumed")
    return 0


def _clean(args: argparse.Namespace, database: Path) -> int:
    from zh.database import clean_journal

    config = load_config()
    days = args.older_than if args.older_than is not None else config.get("retention_days")
    older_than = None
    if days is not None:
        older_than = datetime.now(UTC) - timedelta(days=int(days))
    path = init_db(database)
    connection = connect(path)
    try:
        removed = clean_journal(
            connection,
            older_than=older_than,
            output_root=output_dir(path),
            tombstone_days=int(config.get("tombstone_days") or 30),
        )
    finally:
        connection.close()
    print(removed)
    return 0


def _export(args: argparse.Namespace, database: Path) -> int:
    path = init_db(database)
    connection = connect(path)
    try:
        rows = iter_commands(connection)
    finally:
        connection.close()
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    text = "\n".join(lines) + ("\n" if lines else "")
    if args.output:
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(args.output)
        args.output.chmod(0o600)
        return 0
    sys.stdout.write(text)
    return 0


def _import(args: argparse.Namespace, database: Path) -> int:
    """Read JSON lines, or the current Zsh history when no JSON file is given."""
    path = init_db(database)
    connection = connect(path)
    try:
        if args.zsh or args.file is None:
            history = args.file if args.file is not None else default_zsh_history_path()
            imported = import_zsh_history(connection, history, journal=path)
        else:
            imported = _import_json(connection, args.file)
    finally:
        connection.close()
    print(imported)
    return 0


def _import_json(connection: sqlite3.Connection, source: Path) -> int:
    """Store commands from a JSON-lines file. The commands are not run."""
    imported = 0
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict) or not payload.get("command"):
            continue
        record_command(
            connection,
            command=str(payload["command"]),
            started_at=str(payload.get("started_at") or datetime.now(UTC).isoformat()),
            ended_at=payload.get("ended_at"),
            exit_code=payload.get("exit_code"),
            cwd=payload.get("cwd"),
            session_id=payload.get("session_id"),
            origin="import",
            command_id=payload.get("id"),
        )
        imported += 1
    return imported


def _exit_label(body: dict) -> str:
    """Return a short exit label. A finished command with no code is unknown."""
    if body["exit_code"] is not None:
        return str(body["exit_code"])
    if body.get("ended_at"):
        return "unknown"
    return "running"


def _hook(args: argparse.Namespace, database: Path) -> int:
    action = args.hook_action
    if action == "session":
        print(session(database))
        return 0
    if action == "start":
        command = sys.stdin.read()
        if command.endswith("\n"):
            command = command[:-1]
        command_id = start(database, args.session, args.cwd or str(Path.cwd()), command)
        if command_id:
            print(command_id)
        return 0
    if action == "finish":
        if args.exit_code is None or not args.id:
            raise ValueError("finish requires --id and --exit-code")
        hook_finish(database, args.id, args.exit_code)
        return 0
    if action == "pending-history":
        staged = pending_history(database, args.session)
        if staged:
            print(staged)
        return 0
    if action == "mark-history":
        mark_history(database, args.session)
        return 0
    if action == "sync-kick":
        try:
            sync_kick(database)
        except Exception:
            return 0
        return 0
    raise ValueError("unknown hook")


def _emit_commands(items: list[dict], *, as_json: bool, use_fzf: bool) -> int:
    if as_json:
        print(json.dumps(items, ensure_ascii=False))
        return 0
    if use_fzf:
        chosen = _fzf_choice(items)
        if chosen is not None:
            print(chosen)
        return 0
    if not items:
        print("No commands.")
        return 0
    for item in items:
        exit_label = _exit_label(item)
        print(f"{item['id']}  {exit_label}  {item['command']}")
    return 0


def _fzf_choice(items: list[dict]) -> str | None:
    """Let fzf print a command. The command is not executed."""
    fzf = shutil.which("fzf")
    if fzf is None:
        print("fzf is not installed", file=sys.stderr)
        return None
    if not items:
        return None
    lines = []
    for index, item in enumerate(items):
        preview = item["command"].replace("\n", "\\n")
        lines.append(f"{index}\t{preview}")
    completed = subprocess.run(
        [fzf, "--with-nth=2.."],
        input="\n".join(lines),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    index_text = completed.stdout.split("\t", 1)[0].strip()
    if not index_text.isdigit():
        return None
    index = int(index_text)
    if index < 0 or index >= len(items):
        return None
    return items[index]["command"]


def _coerce(key: str, value: str):
    if key in BOOL_KEYS:
        lowered = value.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{key} must be true or false")
    if key == "retention_days" and value.lower() in {"none", "null", ""}:
        return None
    if key in {"retention_days", "output_max_bytes", "tombstone_days", "sync_batch_size"}:
        return int(value)
    return value


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=None, help="Journal file path")
    common.add_argument("--json", action="store_true", help="Print JSON")
    parser = argparse.ArgumentParser(prog="zh", description="Local command journal")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", parents=[common], help="Create the local journal database")
    browse = subcommands.add_parser(
        "browse", parents=[common], help="Serve the loopback history browser"
    )
    browse.add_argument("--host", default="127.0.0.1")
    browse.add_argument("--port", type=int, default=8765)
    search = subcommands.add_parser("search", parents=[common], help="Search stored commands")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--since")
    search.add_argument("--until")
    search.add_argument("--device")
    search.add_argument("--cwd")
    search.add_argument("--exit-code", type=int)
    search.add_argument("--status", default="all")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--fzf", action="store_true")
    for name, help_text in (
        ("recent", "Show recent commands"),
        ("top", "Show repeated commands"),
        ("failed", "Show failed commands"),
        ("stats", "Count commands by state"),
        ("status", "Show journal and sync status"),
        ("pause", "Stop recording commands"),
        ("resume", "Resume recording commands"),
        ("doctor", "Check the local install"),
    ):
        subcommands.add_parser(name, parents=[common], help=help_text)
    show = subcommands.add_parser("show", parents=[common], help="Show one command")
    show.add_argument("command_id")
    show.add_argument("--output", action="store_true")
    record = subcommands.add_parser(
        "record", parents=[common], help="Record a command through a PTY"
    )
    record.add_argument("argv", nargs=argparse.REMAINDER)
    sync = subcommands.add_parser(
        "sync", parents=[common], help="Synchronise or install the scheduler"
    )
    sync.add_argument("sync_action", nargs="?", choices=["install", "uninstall"])
    sync.add_argument("--home", type=Path)
    sync.add_argument("--platform")
    devices = subcommands.add_parser(
        "devices", parents=[common], help="List, register, or revoke devices"
    )
    devices.add_argument("devices_action", nargs="?", choices=["register", "revoke"])
    devices.add_argument("device_id", nargs="?")
    devices.add_argument("--server")
    devices.add_argument("--name", default=socket.gethostname())
    devices.add_argument("--bootstrap-token")
    config = subcommands.add_parser(
        "config", parents=[common], help="Read or change local settings"
    )
    config.add_argument("config_action", nargs="?", choices=["set", "get", "unset"])
    config.add_argument("key", nargs="?")
    config.add_argument("value", nargs="?")
    clean = subcommands.add_parser("clean", parents=[common], help="Delete old commands")
    clean.add_argument("--older-than", type=int)
    export = subcommands.add_parser("export", parents=[common], help="Write commands as JSON lines")
    export.add_argument("--output", type=Path)
    import_command = subcommands.add_parser(
        "import",
        parents=[common],
        help="Read JSON lines, or the current Zsh history",
    )
    import_command.add_argument(
        "--zsh",
        action="store_true",
        help="Parse a Zsh history file. With no file, read HISTFILE or ~/.zsh_history",
    )
    import_command.add_argument(
        "file",
        nargs="?",
        type=Path,
        help="JSON lines file, or a Zsh history file when --zsh is set",
    )
    hook = subcommands.add_parser("_hook", parents=[common], help=argparse.SUPPRESS)
    hook.add_argument(
        "hook_action",
        choices=["session", "start", "finish", "pending-history", "mark-history", "sync-kick"],
    )
    hook.add_argument("--session", default="")
    hook.add_argument("--cwd", default="")
    hook.add_argument("--id", default="")
    hook.add_argument("--exit-code", type=int)
    hook.add_argument("--file")
    return parser
