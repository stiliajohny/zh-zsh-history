# ZH

![ZH](src/zh/static/icon.svg)

ZH is a local Zsh command journal. Each interactive command is written to SQLite on this machine. A loopback page can search that journal. Sync to another machine stays off until you configure it. Up, Down, and Ctrl+R keep working as Zsh provides them.

## Documentation

- [Install](docs/install.md) covers the Git clone, the pipx install, the `~/.zshrc` line, tab completion, and uninstall.
- [Sync](docs/sync.md) covers starting the API, registering a device, and where the tokens and `DATABASE_URL` are stored.
- [Security](docs/security.md) covers what is stored, output capture, history import, and API authentication.
- [Release](docs/release.md) lists the checks to run before a release.

## Requirements

Python 3.11 or newer, and Zsh. `fzf` is optional and is used only by `zh search --fzf`.

## Install from Git

pipx installs the `zh` command into its own environment and links it on `PATH`. That environment is not in the clone, and it is not committed. The Zsh plugin stays in the clone, so `~/.zshrc` sources that file. The base package has no third-party dependencies.

```bash
git clone https://github.com/stiliajohny/zh-zsh-history.git "$HOME/zh-zsh-history"
pipx install --editable "$HOME/zh-zsh-history"
```

SSH works the same way: `git clone git@github.com:stiliajohny/zh-zsh-history.git "$HOME/zh-zsh-history"`.

pipx puts `zh` in `~/.local/bin`. Add this to `~/.zshrc` when that directory is not already on `PATH`, before the plugin:

```bash
export PATH="$HOME/.local/bin:$PATH"
source "$HOME/zh-zsh-history/zsh/zh.plugin.zsh"
```

Open a new terminal. A new shell records commands only when `zh` is on `PATH`. When `zh` is missing, the hook returns and the shell continues.

`pipx inject zh httpx` adds the sync client. `pipx inject zh fastapi uvicorn httpx 'psycopg[binary]'` adds the API packages. Those match the `sync` and `server` extras. The API is started with `zh-server`.

`zh init` creates the journal. The first recorded command creates it as well. The default path is `$XDG_DATA_HOME/zh/history.sqlite`, or `~/.local/share/zh/history.sqlite` when `XDG_DATA_HOME` is unset. `ZH_DB` or `--db` selects another file. The database file mode is `0600`. A parent directory created for the journal is `0700`.

## This checkout

`.venv/` is gitignored. It is only for tests and lint in this repository. Do not put it on `PATH` in `~/.zshrc`. The `dev` extra installs pytest, Ruff, httpx, and FastAPI:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

To record commands from this checkout, use the pipx install above with this directory in place of `$HOME/zh-zsh-history`, and source `zsh/zh.plugin.zsh` from here.

## Load the plugin

The plugin lasts for the shell that sources it. Sourcing it again in that shell does nothing until `ZH_PLUGIN_LOADED` is unset. `HISTFILE`, keybindings, and existing hooks stay as they are.

Oh My Zsh loads the same file when `zh` is listed in `plugins`. Keep `~/.local/bin` on `PATH`. Do not also `source` the plugin. Link the real plugin file, add `zh` to `plugins=(...)`, and open a new shell.

```bash
mkdir -p "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zh"
ln -sfn "$HOME/zh-zsh-history/zsh/zh.plugin.zsh" "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zh/zh.plugin.zsh"
```

The plugin resolves that link and loads `hooks.zsh` and `history.zsh` from the directory that contains the real `zh.plugin.zsh`. Keep those files beside it.

Aliases `zhs`, `zht`, `zhf`, `zhr`, and `zhy` are defined only when those names are free. They run `zh search`, `zh top`, `zh failed`, `zh recent`, and `zh stats`.

## Tab completion

After the plugin loads, Tab completes `zh`. `zh` then Tab lists the subcommands. `zh search -` then Tab lists that command's flags, including `--status` and `--fzf`. `zh search --status` then Tab lists `all`, `failed`, `ok`, and `running`. `--db` and `--json` complete on every subcommand. The hidden `_hook` command is left out.

If the plugin loads before `compinit`, as Oh My Zsh does, the next new shell completes `zh`. If `compinit` has already run, sourcing the plugin registers the completer in that shell. A second source does nothing, so after the plugin changes run `unset ZH_PLUGIN_LOADED` and source it again. Keybindings and `HISTFILE` stay as they are.

Uninstall steps are in [docs/install.md](docs/install.md).

## What gets recorded

On `preexec` the plugin asks `zh` to store the command as it was typed. On `precmd` it stores the exit code captured before other precmd hooks run. A stored row has the command text, start and end time, exit code, duration, working directory, session, hostname, username, and the Git branch and commit when the working directory is inside a repository. Environment values are stored only for names in `env_allowlist`. That list is empty until you add names, so the environment is omitted.

A command is skipped while recording is paused, when it is empty, when it starts with a space (`ignore_space` defaults to true), or when it matches an `exclude_regex` pattern. `zh pause` and `zh resume` stop and start new recording. A locked journal is written to a spool and stored on a later command. The hook waits at most 0.2 seconds on the database and does not wait on the network. A background sync starts only after sync is configured.

Ordinary recording stores the command text and its metadata. `zh record -- COMMAND` is the output capture: it runs the command in a PTY and keeps a size-limited gzip file. Limits and the history-import crash window are in [docs/security.md](docs/security.md).

## Settings

Config is `ZH_CONFIG`, or `$XDG_CONFIG_HOME/zh/config.json`, or `~/.config/zh/config.json`. The file mode is `0600`. `zh config` prints the settings. A device token is shown as `***`.

```bash
zh config get ignore_space
zh config set ignore_space true
zh config set exclude_regex 'SECRET'
zh config unset exclude_regex 'SECRET'
zh config set env_allowlist EDITOR
zh config set retention_days 90
zh config set output_max_bytes 1048576
```

`zh config set` on `exclude_regex` or `env_allowlist` appends one entry. `zh config unset` removes that entry. Setting `exclude_regex` prints that a regex cannot guarantee a secret is removed. `zh pause` and `zh resume` are the controls for `paused`. Sync stays off until `sync_enabled`, `server_url`, and a device token are set. `zh devices register` writes those three and stores the device token in this file. The bootstrap token and `DATABASE_URL` are not stored here. Those steps are in [docs/sync.md](docs/sync.md).

## Look up commands

`--db` selects the journal. `--json` prints JSON for `search`, `recent`, `top`, `failed`, `stats`, `show`, `status`, and `devices`.

- `zh search [TEXT]` filters by text, `--status` (`all`, `failed`, `ok`, or `running`), `--since`, `--until`, `--device`, `--cwd`, `--exit-code`, and `--limit` (default 20). `zh search --fzf` prints the selected text and leaves it unexecuted. `fzf` must be installed.
- `zh recent`, `zh top`, `zh failed`, and `zh stats` summarise the journal.
- `zh show ID` prints one command. `zh show ID --output` prints a PTY capture stored for that id.
- `zh browse` serves the loopback page. `--host` defaults to `127.0.0.1` and `--port` defaults to `8765`. Accepted hosts are `127.0.0.1`, `localhost`, and `::1`. The page searches stored commands, filters by exit status and time, and draws a graph of how many commands started in that range. Choosing a command copies or displays it. The page leaves the shell unchanged and has no endpoint that adds a command. `zh browse --db` shows only that file.
- `zh record -- COMMAND` runs a command in a PTY and stores a gzip capture up to `output_max_bytes` (default 1048576).
- `zh pause` and `zh resume` stop and start recording.
- `zh import` reads the current Zsh history (`HISTFILE`, or `~/.zsh_history`). `zh import --zsh FILE` reads that history file. `zh import FILE` reads JSON lines. None of these run the commands or write `HISTFILE`. `zh export` writes JSON lines. `zh export --output FILE` writes that file.
- `zh clean` deletes commands older than `retention_days` when that setting is a number of days. `zh clean --older-than DAYS` uses that count for this run. With neither value set, it drops tombstones that have already synced and expired.
- `zh doctor` checks the local journal and redacts the device token. `zh status` prints sync counts.
- `zh devices register`, `zh devices revoke`, `zh sync`, `zh sync install`, and `zh sync uninstall` are the sync client. See [docs/sync.md](docs/sync.md).

## Sync and privacy

Sync stays disabled until config says otherwise. Captured output stays on this machine. Filters run before a command is written. After a pull, imported commands can appear in this shell's Up/Down and Ctrl+R list. Commands read from the Zsh history file are not added to that list again. `HISTFILE` stays unchanged. Details are in [docs/sync.md](docs/sync.md) and [docs/security.md](docs/security.md). Release checks are in [docs/release.md](docs/release.md).

## Check a session

From this repository, with the virtualenv activated:

```bash
source .venv/bin/activate
export ZH_DB=/tmp/zh-test.sqlite
unset ZH_PLUGIN_LOADED
source "$PWD/zsh/zh.plugin.zsh"
echo hello-from-zh
```

In another terminal, using the same file:

```bash
source .venv/bin/activate
zh browse --db /tmp/zh-test.sqlite
```

Open `http://127.0.0.1:8765` and reload. `echo hello-from-zh` is in the journal. The load lasts until that shell exits.

From the same virtualenv, run `python -m pytest` and `python -m ruff check src tests`.
