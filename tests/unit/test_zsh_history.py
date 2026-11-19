"""Tests for reading a Zsh history file into the journal."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zh.cli import main
from zh.config import DEFAULTS
from zh.database import connect, init_db, iter_commands, record_command
from zh.history import prepare_history_import
from zh.zsh_history import import_zsh_history, parse_zsh_history


def test_parser_matches_zsh_history_rules() -> None:
    """Extended lines, continuations, escapes, and meta bytes become commands."""
    cafe = bytes([0x83, 0xC3 ^ 0x20, 0x83, 0xA9 ^ 0x20])
    data = b"".join(
        [
            b": 1700000000:2;echo simple\n",
            b": 1700000001:0;echo one\\\n",
            b"two\n",
            b": 1700000002:0;echo end\\ \n",
            b"\\: colon command\n",
            b": 1700000003:0;echo caf" + cafe + b"\n",
            b": 1700000004:1;echo hello\\\n",
            b": 9:0;inside\n",
            b": 1700000000:1700000005;echo absolute\n",
            b": not a timestamp\n",
            b"echo plain\n",
            b"echo twice\n",
            b"echo twice\n",
            b"echo last",
        ]
    )
    entries = parse_zsh_history(data)
    assert [entry.command for entry in entries] == [
        "echo simple",
        "echo one\ntwo",
        "echo end\\",
        ": colon command",
        "echo café",
        "echo hello\n: 9:0;inside",
        "echo absolute",
        "echo plain",
        "echo twice",
        "echo twice",
        "echo last",
    ]
    assert entries[0].started == 1700000000
    assert entries[0].elapsed == 2
    assert entries[1].elapsed == 0
    assert entries[6].elapsed == 5
    assert entries[7].started is None


def test_import_is_idempotent_and_leaves_the_history_file(tmp_path: Path) -> None:
    """A second import adds nothing, and the history file bytes stay put."""
    history = tmp_path / "zsh_history"
    history.write_bytes(
        b": 1700000000:2;echo simple\n: 1700000000:2;echo simple\n echo hidden\n"
    )
    os.chmod(history, 0o600)
    before = history.read_bytes()
    path = init_db(tmp_path / "history.sqlite")
    connection = connect(path)
    try:
        added = import_zsh_history(connection, history, journal=path, config=dict(DEFAULTS))
        assert added == 2
        again = import_zsh_history(connection, history, journal=path, config=dict(DEFAULTS))
        assert again == 0
        rows = connection.execute(
            "SELECT command, duration_ms, exit_code, import_kind, sync_state, origin "
            "FROM commands ORDER BY rowid"
        ).fetchall()
        assert [row["command"] for row in rows] == ["echo simple", "echo simple"]
        assert rows[0]["duration_ms"] == 2000
        assert rows[0]["exit_code"] is None
        assert rows[0]["import_kind"] == "zsh"
        assert rows[0]["sync_state"] == "imported"
        assert rows[0]["origin"] == "import"
        ids = connection.execute("SELECT id FROM commands").fetchall()
        assert ids[0]["id"] != ids[1]["id"]
    finally:
        connection.close()
    assert history.read_bytes() == before
    assert os.stat(history).st_mode & 0o777 == 0o600


def test_retry_after_a_partial_import_fills_only_the_missing_row(tmp_path: Path) -> None:
    """Deleting one imported row and importing again stores that row once."""
    history = tmp_path / "zsh_history"
    history.write_bytes(b"echo one\necho two\n")
    os.utime(history, (1_700_000_000, 1_700_000_000))
    path = init_db(tmp_path / "history.sqlite")
    connection = connect(path)
    try:
        assert import_zsh_history(connection, history, journal=path, config=dict(DEFAULTS)) == 2
        os.utime(history, (1_700_000_100, 1_700_000_100))
        assert import_zsh_history(connection, history, journal=path, config=dict(DEFAULTS)) == 0
        os.utime(history, (1_700_000_000, 1_700_000_000))
        command_id = _id(connection, "echo two")
        connection.execute("DELETE FROM command_revisions WHERE command_id = ?", (command_id,))
        connection.execute("DELETE FROM commands WHERE id = ?", (command_id,))
        connection.commit()
        assert import_zsh_history(connection, history, journal=path, config=dict(DEFAULTS)) == 1
        stored = [row["command"] for row in iter_commands(connection)]
        assert stored == ["echo one", "echo two"]
        starts = [
            datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
            for row in iter_commands(connection)
        ]
        assert starts[0] == datetime.fromtimestamp(1_699_999_999, UTC)
        assert starts[1] == datetime.fromtimestamp(1_700_000_000, UTC)
        finished = connection.execute(
            "SELECT ended_at, duration_ms FROM commands WHERE command = 'echo one'"
        ).fetchone()
        assert finished["ended_at"] is not None
        assert finished["duration_ms"] is None
    finally:
        connection.close()


def test_filters_skip_secrets_and_pause_does_not_block_import(tmp_path: Path) -> None:
    """Leading space and exclude patterns are dropped. Pause still imports."""
    history = tmp_path / "zsh_history"
    history.write_bytes(b" echo hidden\necho secret\necho kept\n")
    path = init_db(tmp_path / "history.sqlite")
    connection = connect(path)
    try:
        paused = dict(DEFAULTS)
        paused["paused"] = True
        paused["exclude_regex"] = ["secret"]
        added = import_zsh_history(connection, history, journal=path, config=paused)
        assert added == 1
        assert [row["command"] for row in iter_commands(connection)] == ["echo kept"]
    finally:
        connection.close()


def test_zsh_import_is_not_staged_back_into_the_shell(tmp_path: Path) -> None:
    """A history-file import stays out of the shell refresh. A remote import does not."""
    history = tmp_path / "zsh_history"
    history.write_bytes(b"echo from-file\n")
    path = init_db(tmp_path / "history.sqlite")
    connection = connect(path)
    try:
        import_zsh_history(connection, history, journal=path, config=dict(DEFAULTS))
        record_command(
            connection,
            command="echo remote",
            started_at="2020-01-01T00:00:00Z",
            exit_code=0,
            origin="import",
        )
        staged = prepare_history_import(connection, path, "session-a")
        assert staged is not None
        payload = staged.read_bytes()
        assert payload.split(b"\0")[0] == b"echo remote"
        assert b"echo from-file" not in payload
    finally:
        connection.close()


def test_import_rejects_a_database_and_a_missing_file(tmp_path: Path) -> None:
    """The journal and a missing path are not treated as history."""
    path = init_db(tmp_path / "history.sqlite")
    connection = connect(path)
    try:
        other = tmp_path / "other.sqlite"
        other.write_bytes(b"SQLite format 3\x00")
        with pytest.raises(ValueError, match="journal"):
            import_zsh_history(connection, path, journal=path, config=dict(DEFAULTS))
        with pytest.raises(ValueError, match="SQLite"):
            import_zsh_history(connection, other, journal=path, config=dict(DEFAULTS))
        with pytest.raises(ValueError, match="not found"):
            import_zsh_history(
                connection, tmp_path / "missing", journal=path, config=dict(DEFAULTS)
            )
        with pytest.raises(ValueError, match="directory"):
            import_zsh_history(connection, tmp_path, journal=path, config=dict(DEFAULTS))
    finally:
        connection.close()


def test_cli_reads_histfile_and_json_lines(tmp_path: Path, monkeypatch, capsys) -> None:
    """zh import reads HISTFILE, and zh import FILE still reads JSON lines."""
    history = tmp_path / "zsh_history"
    history.write_bytes(b"echo from-hist\n")
    monkeypatch.setenv("HISTFILE", str(history))
    monkeypatch.setenv("ZH_CONFIG", str(tmp_path / "missing.json"))
    database = tmp_path / "history.sqlite"
    assert main(["import", "--db", str(database)]) == 0
    assert capsys.readouterr().out.strip() == "1"
    assert history.read_bytes() == b"echo from-hist\n"
    source = tmp_path / "commands.jsonl"
    source.write_text(json.dumps({"command": "echo json"}) + "\n", encoding="utf-8")
    assert main(["import", str(source), "--db", str(database)]) == 0
    assert capsys.readouterr().out.strip() == "1"
    connection = connect(init_db(database))
    try:
        assert [row["command"] for row in iter_commands(connection)] == [
            "echo from-hist",
            "echo json",
        ]
    finally:
        connection.close()


def test_real_zsh_history_round_trip(tmp_path: Path) -> None:
    """A file written by Zsh parses back to the commands that were saved."""
    if shutil.which("zsh") is None:
        pytest.skip("zsh is not installed")
    history = tmp_path / "zsh_history"
    zshrc = "\n".join(
        [
            "setopt EXTENDED_HISTORY",
            "unsetopt HIST_IGNORE_SPACE",
            f"HISTFILE={shlex.quote(str(history))}",
            "HISTSIZE=50",
            "SAVEHIST=50",
            "print -s -- 'echo simple'",
            "print -s -- $'echo multi\\nline'",
            "print -s -- $'echo trail\\\\'",
            "print -s -- ': colon command'",
            "print -s -- 'echo café'",
            "print -s -- $'echo hello\\n: 9:0;inside'",
            "print -s -- 'echo twice'",
            "print -s -- 'echo twice'",
            'fc -W "$HISTFILE"',
            "exit",
            "",
        ]
    )
    (tmp_path / ".zshrc").write_text(zshrc, encoding="utf-8")
    env = os.environ.copy()
    env["ZDOTDIR"] = str(tmp_path)
    env["HISTFILE"] = str(history)
    completed = subprocess.run(
        ["zsh", "-i"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    entries = parse_zsh_history(history.read_bytes())
    assert [entry.command for entry in entries] == [
        "echo simple",
        "echo multi\nline",
        "echo trail\\",
        ": colon command",
        "echo café",
        "echo hello\n: 9:0;inside",
        "echo twice",
        "echo twice",
    ]
    assert all(entry.started is not None for entry in entries)


def _id(connection: sqlite3.Connection, command: str) -> str:
    row = connection.execute("SELECT id FROM commands WHERE command = ?", (command,)).fetchone()
    assert row is not None
    return str(row["id"])
