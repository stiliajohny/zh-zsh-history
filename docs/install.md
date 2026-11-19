# Install

ZH needs Python 3.11 or newer, Zsh, and pipx. pipx installs the `zh` command outside the clone. The clone supplies the Zsh plugin. `.venv/` is gitignored and is not part of this install.

## Oh My Zsh

Oh My Zsh loads `$ZSH_CUSTOM/plugins/zh/zh.plugin.zsh` when `zh` is in `plugins=(...)`. `$ZSH_CUSTOM` is `~/.oh-my-zsh/custom` when it is unset. This repository keeps `zh.plugin.zsh` in `zsh/`, beside `hooks.zsh`, `history.zsh`, and `completions/`. Clone into the custom directory and link that `zsh` directory as the `zh` plugin. A clone directly in `plugins/zh` leaves `zh.plugin.zsh` one directory deeper, so Oh My Zsh does not load it.

```bash
ZSH_CUSTOM="${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}"
git clone https://github.com/stiliajohny/zh-zsh-history.git "$ZSH_CUSTOM/zh-zsh-history"
mkdir -p "$ZSH_CUSTOM/plugins"
ln -sfn "$ZSH_CUSTOM/zh-zsh-history/zsh" "$ZSH_CUSTOM/plugins/zh"
pipx install --editable "$ZSH_CUSTOM/zh-zsh-history"
```

SSH works the same way: `git clone git@github.com:stiliajohny/zh-zsh-history.git "$ZSH_CUSTOM/zh-zsh-history"`.

pipx links `zh` into `~/.local/bin`. A new shell records commands only when `zh` is on `PATH`. When `zh` is missing, the hook returns and the shell continues.

Add the `PATH` line to `~/.zshrc` when `~/.local/bin` is not already on `PATH`. Add `zh` to the existing `plugins=(...)` line. The line below is a stock Oh My Zsh list with `zh` added. Keep any other names already on that line. Leave out a `source` of the plugin.

```bash
export PATH="$HOME/.local/bin:$PATH"
plugins=(git zh)
```

Open a new terminal. Oh My Zsh loads the linked directory for that shell. The plugin resolves the link and loads `hooks.zsh` and `history.zsh` from the real `zsh` directory.

`git -C "$ZSH_CUSTOM/zh-zsh-history" pull` updates the plugin. The pipx install is editable, so the `zh` command follows that clone.

The base package has no third-party dependencies. `pipx inject zh httpx` adds the sync client. `pipx inject zh fastapi uvicorn httpx 'psycopg[binary]'` adds the API packages. Those match the `sync` and `server` extras. The API is started with `zh-server`. Database and token setup is in [sync.md](sync.md). `fzf` is used only by `zh search --fzf`.

The plugin defines `zhs`, `zht`, `zhf`, `zhr`, and `zhy` only when those names are free. They run `zh search`, `zh top`, `zh failed`, `zh recent`, and `zh stats`. Keybindings and `HISTFILE` stay as they are.

## Without Oh My Zsh

Clone under `~/.local/share` and source the plugin. pipx is the same command, pointed at this clone.

```bash
git clone https://github.com/stiliajohny/zh-zsh-history.git "$HOME/.local/share/zh-zsh-history"
pipx install --editable "$HOME/.local/share/zh-zsh-history"
```

Add this to `~/.zshrc`. Include the `PATH` line when `~/.local/bin` is not already on `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
source "$HOME/.local/share/zh-zsh-history/zsh/zh.plugin.zsh"
```

Open a new terminal. That load lasts until the shell exits. Sourcing the file again in the same shell does nothing. After the plugin file changes, `unset ZH_PLUGIN_LOADED` before sourcing it again.

## This checkout

`.venv/` is listed in `.gitignore`. Create it only to run tests and lint. Do not add it to `~/.zshrc`. The `dev` extra installs pytest, Ruff 0.17.5, pre-commit, httpx, and FastAPI:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pre-commit install
```

`pre-commit install` runs Ruff on staged Python files before each commit and applies Ruff's fixes. The same lint command is `python -m ruff check src tests server`.

To record commands from this checkout, run `pipx install --editable` on this directory and source `zsh/zh.plugin.zsh` from here.

## Tab completion

`zh` then Tab lists subcommands. `zh search -` then Tab lists that command's flags. `zh search --status` then Tab lists `all`, `failed`, `ok`, and `running`. `--db` and `--json` complete on every subcommand. The hidden `_hook` command is left out.

Oh My Zsh loads the plugin before `compinit`, so a new shell completes `zh`. In a shell that already ran `compinit`, sourcing the plugin registers the completer immediately. After the plugin file changes, `unset ZH_PLUGIN_LOADED` and source it again. The completions directory has to stay beside the real `zh.plugin.zsh`.

## One shell

From the clone or this checkout, with `zh` already on `PATH`, to try a separate journal:

```bash
export ZH_DB=/tmp/zh-test.sqlite
unset ZH_PLUGIN_LOADED
source "$PWD/zsh/zh.plugin.zsh"
echo hello-from-zh
```

In another terminal:

```bash
zh browse --db /tmp/zh-test.sqlite
```

Open `http://127.0.0.1:8765` and reload. `echo hello-from-zh` is in the journal.

## Uninstall

`pipx uninstall zh` removes the command. Remove `zh` from `plugins` and delete the `$ZSH_CUSTOM/plugins/zh` link, or remove the `source` line from `~/.zshrc`. That stops new recording. The journal file stays until you delete it. The clone can be removed after that.

`zh sync uninstall` removes the user scheduler this tool wrote: `~/Library/LaunchAgents/com.zh.sync.plist` on macOS, or `~/.config/systemd/user/zh-sync.service` and `zh-sync.timer` on Linux. It does not delete the journal or the config.
