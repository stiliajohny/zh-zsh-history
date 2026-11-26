# ZH

![ZH](src/zh/static/icon.svg)

ZH is a local-first Zsh command journal. Every interactive command is recorded in a local SQLite database without blocking your prompt or altering standard Up, Down, Ctrl+R, or `HISTFILE` behavior.

## Features

- **Local-first storage:** Records commands, exit codes, duration, working directory, Git metadata, and optional environment variables in SQLite (`0600`).
- **Non-blocking hooks:** `preexec`/`precmd` hooks wait at most 0.2s on SQLite, spool writes when locked, and never wait on the network.
- **Optional PTY output capture:** Stores gzip-compressed stdout/stderr locally (on by default, configurable and size-limited).
- **Search & loopback web UI:** Query history from the CLI (FTS5 + optional `fzf`) or inspect activity and outputs in an offline web page via `zh browse`.
- **Opt-in sync:** Disabled by default; optionally sync across machines via a self-hosted FastAPI and PostgreSQL service.

## Documentation

- [Install](docs/install.md) — Oh My Zsh plugin directory, `pipx` install, standalone Zsh setup, tab completion, and uninstall.
- [Sync](docs/sync.md) — starting `zh-server`, registering a device, and where tokens and `DATABASE_URL` live.
- [Security](docs/security.md) — stored data, PTY output capture, history import crash window, and API authentication.
- [Release](docs/release.md) — pre-release verification checklist.

## Requirements

- **Python 3.11+** and **Zsh**
- **pipx** (for installing the `zh` CLI into an isolated environment on `PATH`)
- **fzf** (optional, used only by `zh search --fzf`)

The base package has no third-party Python dependencies.

## Install (Oh My Zsh)

`pipx` installs `zh` into `~/.local/bin`, while the repository provides the Zsh plugin under `zsh/` (beside `hooks.zsh`, `history.zsh`, and `completions/`). Cloning into `$ZSH_CUSTOM` and linking `zsh/` as `$ZSH_CUSTOM/plugins/zh` lets Oh My Zsh load `zh.plugin.zsh` directly.

```bash
ZSH_CUSTOM="${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}"
git clone https://github.com/stiliajohny/zh-zsh-history.git "$ZSH_CUSTOM/zh-zsh-history"
mkdir -p "$ZSH_CUSTOM/plugins"
ln -sfn "$ZSH_CUSTOM/zh-zsh-history/zsh" "$ZSH_CUSTOM/plugins/zh"
pipx install --editable "$ZSH_CUSTOM/zh-zsh-history"
```

SSH works the same way: `git clone git@github.com:stiliajohny/zh-zsh-history.git "$ZSH_CUSTOM/zh-zsh-history"`.

Update `~/.zshrc` to include `~/.local/bin` on `PATH` (if not already present) and add `zh` to your existing `plugins=(...)` list. Do not also `source` the plugin:

```bash
export PATH="$HOME/.local/bin:$PATH"
plugins=(git zh)
```

Open a new terminal. A shell records commands only when `zh` is on `PATH`; if `zh` is missing, the hook returns quietly and the shell continues normally.

> **Not using Oh My Zsh?** Clone under `~/.local/share/zh-zsh-history`, install with `pipx install --editable`, and `source` `zsh/zh.plugin.zsh` in `~/.zshrc`. See [docs/install.md](docs/install.md) for full steps and uninstall instructions.

### Optional extras & database path

- **Sync client:** `pipx inject zh httpx` (matches the `sync` extra)
- **Sync server:** `pipx inject zh fastapi uvicorn httpx 'psycopg[binary]'` (matches the `server` extra; starts with `zh-server`)
- **Database file:** Created on `zh init` or on the first recorded command at `$XDG_DATA_HOME/zh/history.sqlite` (or `~/.local/share/zh/history.sqlite`). Override with `ZH_DB` or `--db`. Database files use mode `0600` and newly created parent directories use `0700`.

## Quickstart

After opening a new terminal:

```bash
zh search git            # Search recorded commands (alias: zhs git)
zh search --fzf          # Pick a command interactively with fzf (prints without running)
zh recent                # Show recent commands (alias: zhr)
zh stats                 # Summarize command counts (alias: zhy)
zh show 42 --output      # View captured stdout/stderr for command ID 42
zh browse                # Open the local web UI at http://127.0.0.1:8765
```

## Shell integration & tab completion

- **Single-load guard:** The plugin lasts for the current shell session. Re-sourcing it is a no-op until `ZH_PLUGIN_LOADED` is unset. `HISTFILE`, keybindings, and existing hooks remain unchanged.
- **Aliases:** `zhs`, `zht`, `zhf`, `zhr`, and `zhy` are defined for `zh search`, `zh top`, `zh failed`, `zh recent`, and `zh stats` only if those names are not already taken.
- **Tab completion:** `zh <Tab>` lists subcommands (omitting the internal `_hook` command), `zh <subcommand> -<Tab>` lists flags (`--db` and `--json` complete on every subcommand), and `zh search --status <Tab>` lists `all`, `failed`, `ok`, and `running`.
  - With Oh My Zsh (loaded before `compinit`), completions work in the next new shell.
  - If `compinit` has already run, sourcing the plugin registers the completer immediately. After editing plugin files, run `unset ZH_PLUGIN_LOADED` and re-source.

## What gets recorded

- **Captured metadata:** On `preexec`, ZH stores the command as typed along with start time, working directory, session ID, hostname, username, and Git branch/commit (when inside a repository). On `precmd`, it records the exit code (captured before other `precmd` hooks run), end time, and duration. Environment variables are recorded only if named in `env_allowlist` (empty by default).
- **Filtering & reliability:** Commands are skipped when recording is paused (`zh pause`), when empty, when starting with a space (`ignore_space` defaults to `true`), or when matching an `exclude_regex` pattern. If SQLite is locked, writes go to a local spool and flush on a later command. Hooks wait at most 0.2s on the database, never block on network I/O, and disown any background sync kick so Zsh prints no job notice.
- **Output capture:** On an interactive terminal (`capture_output` defaults to `true`), stdout and stderr are copied through a PTY to a gzip file next to the journal (up to `output_max_bytes`, default `1048576`) without re-running the command. Inspect captures via `zh show ID --output` or `zh browse`. Captured output stays local and is never synced. Non-TTY shells do not capture; full-screen TUI programs can misbehave under PTY capture (`zh config set capture_output false` disables it). Use `zh record -- COMMAND` to capture a single command explicitly. See [docs/security.md](docs/security.md) for limitations and the history-import crash window.

## Commands

Pass `--db PATH` to target another journal, or `--json` for machine-readable output on `search`, `recent`, `top`, `failed`, `stats`, `show`, `status`, and `devices`.

### Search & inspect
- `zh search [TEXT]` — filter by text, `--status` (`all`, `failed`, `ok`, `running`), `--since`, `--until`, `--device`, `--cwd`, `--exit-code`, and `--limit` (default `20`). Add `--fzf` to print a selection without executing it.
- `zh recent`, `zh top`, `zh failed`, `zh stats` — summarize the journal.
- `zh show ID [--output]` — print one command record, or its captured PTY output.

### Web UI
- `zh browse` — serve the offline loopback page (`--host` defaults to `127.0.0.1` and accepts `127.0.0.1`, `localhost`, or `::1`; `--port` defaults to `8765`; `--db` restricts to that file). Supports searching, exit-status and time-range filtering, and an activity graph. Selecting a command displays or copies it without executing it, and no endpoint writes commands.

### Recording & maintenance
- `zh init` — create the local journal database.
- `zh record -- COMMAND` — run a command in a PTY and store its gzip-compressed output.
- `zh pause` / `zh resume` — stop and restart command recording.
- `zh import` — read Zsh history (`HISTFILE` or `~/.zsh_history`), `zh import --zsh FILE`, or `zh import FILE` (JSON lines) without executing commands or modifying `HISTFILE`.
- `zh export [--output FILE]` — write the journal as JSON lines.
- `zh clean [--older-than DAYS]` — delete commands older than `retention_days` (or `--older-than`), or prune expired synced tombstones when neither is set.
- `zh doctor` / `zh status` — validate the local install (redacting the device token) and print sync counts.

### Sync
- `zh devices register|revoke`, `zh sync [install|uninstall]` — manage device credentials, manual sync, and background schedulers. See [docs/sync.md](docs/sync.md).

## Settings

Configuration is stored at `ZH_CONFIG`, `$XDG_CONFIG_HOME/zh/config.json`, or `~/.config/zh/config.json` (mode `0600`). Run `zh config` to print current settings (`device_token` is redacted as `***`).

| Key | Default | Description |
| --- | --- | --- |
| `ignore_space` | `true` | Skip commands starting with a space. |
| `exclude_regex` | `[]` | Regex patterns to exclude before persistence (`set` appends, `unset` removes). |
| `env_allowlist` | `[]` | Environment variable names to record (`set` appends, `unset` removes). |
| `retention_days` | `null` | Days to retain commands when running `zh clean`. |
| `capture_output` | `true` | Capture interactive stdout/stderr locally via PTY. |
| `output_max_bytes` | `1048576` | Max uncompressed bytes stored per command output capture. |
| `tombstone_days` | `30` | Days to retain deletion tombstones before pruning. |
| `sync_batch_size` | `500` | Events per sync batch (clamped between `1` and `500`). |

- `paused` is toggled with `zh pause` and `zh resume`.
- Sync settings (`sync_enabled`, `server_url`, `device_token`, `device_id`) are populated by `zh devices register`. The bootstrap token and `DATABASE_URL` are not stored in `config.json`.
- Setting `exclude_regex` prints a reminder that regex filtering cannot guarantee a secret is removed.

```bash
zh config get ignore_space
zh config set ignore_space true
zh config set exclude_regex 'SECRET'
zh config unset exclude_regex 'SECRET'
zh config set env_allowlist EDITOR
zh config set retention_days 90
zh config set capture_output false
zh config set output_max_bytes 1048576
```

## Sync and privacy

Sync stays off until configured. Captured output remains local, and exclusion filters run before persistence. After a pull, imported remote commands can appear in your shell's Up/Down and Ctrl+R history, whereas rows imported from a local Zsh history file are not re-added to that list. `HISTFILE` is never overwritten. See [docs/sync.md](docs/sync.md), [docs/security.md](docs/security.md), and [docs/release.md](docs/release.md) for details.

## Development

### Local checkout setup

`.venv/` is gitignored and is intended only for running tests and lint in this repository — do not put it on `PATH` in `~/.zshrc`. The `dev` extra installs `pytest`, `ruff` (0.17.5), `pre-commit`, `httpx`, and `fastapi`:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pre-commit install
```

`pre-commit install` configures a hook that runs Ruff on staged Python files and applies fixes before each commit. To record commands from a local checkout in your shell, run `pipx install --editable .` and source `zsh/zh.plugin.zsh` from that directory.

### Check a test session

From the repository root with `.venv` active:

```bash
source .venv/bin/activate
export ZH_DB=/tmp/zh-test.sqlite
unset ZH_PLUGIN_LOADED
source "$PWD/zsh/zh.plugin.zsh"
echo hello-from-zh
```

In a second terminal using the same database:

```bash
source .venv/bin/activate
zh browse --db /tmp/zh-test.sqlite
```

Open `http://127.0.0.1:8765` and reload to see `echo hello-from-zh` in the journal. The plugin stays loaded until that test shell exits.

### Run tests and lint

```bash
python -m pytest
python -m ruff check src tests server
```
