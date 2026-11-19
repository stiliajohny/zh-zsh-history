# ZH — Implementation Plan and Roadmap

**Status:** Implemented · **Platform:** macOS and Linux · **Shell:** ZSH / Oh My Zsh

## Goal

Create a local-first ZSH history and execution journal that preserves native Up/Down arrow and Ctrl+R behaviour, stores commands and execution metadata in SQLite, optionally captures output, and incrementally synchronises history across devices through a remote PostgreSQL API.

## Non-negotiable outcomes

- Normal ZSH history continues to work, including native navigation.
- Commands and metadata survive offline operation and shell restarts.
- Bidirectional sync transfers only missing or changed events and is idempotent.
- Multiple terminals/devices can run concurrently without data loss.
- Remote sync never blocks the shell.
- Optional output capture and environment capture protect secrets by default.
- Installation supports Oh My Zsh and manual sourcing.

## Architecture

```text
ZSH preexec/precmd hooks
       |
       v
Local SQLite (commands, revisions, sessions, sync state, FTS5)
       |                         ^
       v                         |
Background sync worker <--> HTTPS FastAPI <--> PostgreSQL
       |
       v
Remote events in SQLite -> staged native history import -> Up/Down/Ctrl+R

Explicit `zh record -- ...` -> PTY -> compressed local output files
```

### Stack

- ZSH with `add-zsh-hook` and optional ZLE widgets
- Python 3.11+ for CLI, local persistence and worker
- SQLite WAL + FTS5 locally; PostgreSQL remotely
- FastAPI + httpx for sync API/client
- `systemd --user` timer (Linux), `launchd` (macOS)
- pytest, Ruff, ShellCheck, GitHub Actions (or equivalent CI)
- Optional fzf for interactive search

### Repository layout

```text
zh/
├── AGENTS.md
├── PLAN.md
├── .cursor/rules/project.mdc
├── README.md
├── pyproject.toml
├── zsh/zh.plugin.zsh
├── zsh/hooks.zsh
├── zsh/history.zsh
├── zsh/completions/_zh
├── src/zh/{cli,database,browser,capture,search,analytics,sync,security,output}.py
├── server/app/
├── server/migrations/
├── deploy/systemd/
├── deploy/launchd/
├── tests/{unit,integration,zsh,sync}/
└── docs/
```

## Data model

Local tables: `commands`, `command_revisions` (or immutable completion events), `sessions`, `devices`, `outputs`, `sync_state`, `history_imports`, `tombstones`. Index command timestamps, device, exit code, pending sync and FTS5 text. Each execution has a UUID; identical command strings are distinct events. Record start/end timestamps (UTC), exit status, duration, cwd, device/session ID and optional Git metadata. Outputs are compressed files referenced by SQLite, not blobs in the command table. Use migrations and secure local permissions.

## Sync protocol

1. Locally record an event immediately, independent of connectivity.
2. Push only locally originated pending events/revisions in bounded batches (default 500).
3. Server authenticates device, validates ownership, commits idempotently and acknowledges specific IDs/revisions.
4. Mark only acknowledged revisions as synced.
5. Pull changes after a server-provided, **commit-safe** cursor; PostgreSQL sequence allocation alone does not guarantee commit ordering.
6. Apply changes, tombstones and cursor advance in a single local transaction.
7. Stage newly imported remote commands for native ZSH history; never re-export them as new local executions.
8. Use locks, HTTP timeouts, exponential backoff with jitter and crash-safe retry.
9. Support revocation, retention, offline devices and tombstone expiry policies.

Proposed API: `POST /v1/devices/register`, `POST /v1/events/push`, `GET /v1/events/pull?cursor=...`, `GET /v1/devices`, `DELETE /v1/devices/{id}`, `GET /v1/health`.

## Native history strategy

Use existing ZSH history, not a replacement navigation interface. Explore staged `fc -R` imports, test multiline/extended history and multiple active shells. Track imported event IDs per shell session. Native history and SQLite cannot be updated atomically; document crash windows and test recovery. Do not overwrite HISTFILE. Preserve user options by default; evaluate `INC_APPEND_HISTORY`, `SHARE_HISTORY`, `HIST_FCNTL_LOCK` for compatibility. Optional pre-navigation ZLE refresh may be added after the prompt-refresh implementation works.

## CLI

`zh init`, `zh search`, `zh recent`, `zh top`, `zh failed`, `zh stats`, `zh show ID`, `zh record -- CMD`, `zh sync`, `zh status`, `zh devices`, `zh config`, `zh doctor`, `zh clean`, `zh export`, `zh import`.

Support date/device/cwd/exit-code filters, JSON output, completions and optional fzf. Ship optional aliases (`zhs`, `zht`, `zhf`, `zhr`, `zhy`) without overriding existing aliases/keybindings.

## Security and privacy

Remote sync disabled by default. Exclude leading-space commands when configured, regex patterns, explicit pause/resume, and allowlisted environment variables. Apply filters **before persistence**. Warn that regex filtering cannot guarantee secret removal. Use per-device credentials, HTTPS, local file permissions and device revocation. Consider client-side encryption for remote data. PTY output recording must be opt-in, size-limited, compressed and optionally encrypted; PTY streams do not reliably separate stdout/stderr. Provide deletion/tombstone propagation and retention controls.

## Roadmap — update this on every agent task

Status values: `Planned`, `In Progress`, `Blocked`, `Done`. Each new feature/bug/refactor must be added with a new `ZH-###` ID **before code changes**. Update status, validation and task log **after implementation**. Routine terminal commands do not need individual entries.

### M0 — Project foundation

**ZH-001: Package, tooling and Oh My Zsh loader**

- Priority: P0 | Status: Done | Dependencies: None
- Scope: Add `zsh/zh.plugin.zsh`. A session loads it with `source zsh/zh.plugin.zsh` and drops it when that shell exits. Oh My Zsh loads the same file when `zh` is in `plugins` and the file is linked at `$ZSH_CUSTOM/plugins/zh/zh.plugin.zsh`. Loading it again in that shell is a no-op. Keybindings stay unchanged. ZH-003 sources `zsh/hooks.zsh` from this file so a loaded session can record commands.
- Acceptance: project installs locally; `zh.plugin.zsh` loads from Oh My Zsh `plugins=(... zh)` or standalone sourcing; repeated loading is safe; hooks/keybindings untouched; lint/tests run.
- Validation: `zsh -n zsh/zh.plugin.zsh` — syntax ok. `python -m pytest -q` — 26 passed. `python -m ruff check tests/zsh/test_plugin.py` — clean. `python -m ruff check src tests` still reports four existing E501 lines in `src/zh/database.py` (376, 541, 842, 1076); this task did not change that file.
- Notes: ZH-003 records commands from the sourced plugin. A second source in the same shell does not reload the file.

**ZH-002: SQLite schema and migrations**

- Priority: P0 | Status: Done | Dependencies: ZH-001
- Acceptance: WAL, busy timeout, migrations, UUIDs, revisions, sessions, devices, outputs, sync cursors, history imports, tombstones and indexes; tests cover concurrent writes and migrations.
- Validation: `python -m pytest tests/unit/test_database.py -q` is included in `python -m pytest -q` — 49 passed, 1 skipped. Schema version is 3. A version 1 journal migrates forward and keeps the original command. Concurrent writes are covered by `test_concurrent_records`.
- Notes: Version 1 created the commands table and FTS5 triggers. Version 2 added revisions, sessions, devices, outputs, sync state, history imports, and tombstones. Version 3 adds `username`.

### M1 — Local command journal

**ZH-003: ZSH lifecycle hooks**

- Priority: P0 | Status: Done | Dependencies: ZH-002
- Scope: `zsh/zh.plugin.zsh` sources `zsh/hooks.zsh`. `preexec` calls `zh _hook start` with the original command on stdin. `precmd` calls `zh _hook finish` with the exit code saved before other hooks run. The journal is `ZH_DB` or the default path. Other hooks and keybindings stay in place. No network call. `HISTFILE` is not modified.
- Acceptance: `preexec` captures original command and start metadata; `precmd` captures original exit code, end time and duration; works with multiline commands, pipelines, nested shells and other hooks; no network blocking.
- Validation: `python -m pytest -q` — 48 passed, 1 skipped. Includes `tests/integration/test_zsh_hooks.py` and `tests/zsh/test_shell_record.py`. `python -m ruff check src/zh/capture.py tests/zsh/test_shell_record.py tests/unit/test_capture.py tests/zsh/test_plugin.py` — clean. `python -m ruff check src/zh/cli.py` still reports existing long lines in that file.
- Notes: Uses the current journal. Does not finish the remaining ZH-002 acceptance items. A command that starts with a space is not stored. The browser shows only the database file it was started with.

**ZH-004: CLI, FTS5 and analytics**

- Priority: P1 | Status: Done | Dependencies: ZH-003
- Acceptance: init/search/recent/top/failed/stats/show/status/config/doctor; search filters, JSON output, optional fzf and shell completions; selection never auto-executes.
- Validation: `python -m pytest tests/unit/test_cli.py tests/unit/test_commands.py tests/unit/test_search.py -q` is included in the 49 passing tests. `zsh -n zsh/completions/_zh` passed. `zh search --fzf` prints the selection and does not run it.

### M2 — Remote sync

**ZH-005: FastAPI/PostgreSQL service and device auth**

- Priority: P0 | Status: Done | Dependencies: ZH-002
- Acceptance: versioned API, migrations, scoped credentials, registration/revocation, TLS documentation, input validation and authentication tests.
- Validation: `python -m pytest tests/sync/test_protocol.py -q` is included in the 49 passing tests. TLS behaviour is documented in `docs/sync.md`. `psql` is installed, but no server was listening on the local socket, so `server/migrations/001_init.sql` was not applied to a live PostgreSQL database. The same statements are covered through the SQLite server store.

**ZH-006: Incremental bidirectional sync**

- Priority: P0 | Status: Done | Dependencies: ZH-003, ZH-005
- Acceptance: idempotent push, revisions, commit-safe pull cursor, transactional local cursor, tombstones, partial acknowledgements, concurrent devices, offline recovery and retries; no duplicate events.
- Validation: `tests/sync/test_protocol.py` covers auth, idempotent push, the commit-safe cursor, two devices, tombstones, retries, and a busy overlapping sync. Included in the 49 passing tests.

**ZH-007: Background scheduling**

- Priority: P1 | Status: Done | Dependencies: ZH-006
- Acceptance: macOS launchd and Linux systemd user timers, manual `zh sync`, asynchronous shell trigger, locking, timeouts, backoff, sync status and no overlapping workers.
- Validation: `tests/unit/test_commands.py` installs and removes the systemd and launchd units. `tests/sync/test_protocol.py` covers backoff and a held sync lock. Unit templates are in `deploy/systemd` and `deploy/launchd`. Included in the 49 passing tests.

### M3 — Native cross-device history

**ZH-008: Native history import and refresh**

- Priority: P0 | Status: Done | Dependencies: ZH-003, ZH-006
- Acceptance: remote commands appear with Up/Down/Ctrl+R after refresh; no HISTFILE overwrite; multiline handling, per-session import tracking, crash/retry behaviour and multi-terminal tests.
- Validation: `tests/integration/test_zsh_hooks.py` shows an imported command in `fc -l` and leaves a sentinel HISTFILE unchanged. `tests/unit/test_hooks.py` reuses a pending import file until it is marked. The crash window is documented in `docs/security.md`: a shell that stops after `print -s` and before mark-history can insert those lines twice in that session. Included in the 49 passing tests.

### M4 — Privacy and optional output

**ZH-009: Privacy, retention and deletion**

- Priority: P0 | Status: Done | Dependencies: ZH-003, ZH-006
- Acceptance: exclusion-before-persist, pause/resume, env allowlist, secure permissions, retention, tombstones, device revocation and output deletion tests.
- Validation: `tests/unit/test_hooks.py` rejects leading space, pause, and patterns before insert, and stores only allowlisted environment names. `tests/sync/test_protocol.py` deletes a command and syncs the tombstone. `zh doctor` redacts the device token. Included in the 49 passing tests. Limits are documented in `docs/security.md`.

**ZH-010: Explicit PTY output recording**

- Priority: P2 | Status: Done | Dependencies: ZH-004, ZH-009
- Acceptance: `zh record -- ...` captures terminal stream, preserves exit code, limits/compresses output, documents interactive and stdout/stderr limitations, and does not change ordinary command execution.
- Validation: `tests/unit/test_capture.py` keeps the exit code, limits the capture, stores gzip mode `0600`, and still runs a command that a filter refuses to store. Ordinary hook recording does not open a PTY. Limitations are in `docs/security.md`. Included in the 49 passing tests.

### M4b — Local history browser

**ZH-012: Loopback history browser**

- Priority: P1 | Status: Done | Dependencies: None (lands the commands table and migration v1 that ZH-002 will extend)
- Scope: Local-only web UI served by `zh browse` on 127.0.0.1. Reads the SQLite command journal. Shows recent commands, substring search, failed/succeeded/running filters, stats, and command detail. Selection never executes a command. No record or import endpoint. Minimum schema is `commands`, FTS5 sync, and `schema_migrations`. Database file mode `0600`. Non-loopback binds are rejected.
- Acceptance: `zh init` creates a WAL database; `zh browse` serves only on a loopback address; the UI loads offline with no third-party assets; search, status filters, stats, detail, empty journal, unknown id, and bad query parameters behave as tested; command text is rendered as text; HTML/JS does not execute journal contents; file mode is `0600`; repeated migration is safe.
- Validation: `python -m pytest -q` — 21 passed. `python -m ruff check src tests` — clean.
- Notes: Shell hooks, native history import, sync, and PTY capture stay on their own tasks. This browser shows whatever is already in the journal.

**ZH-013: History browser visual design**

- Priority: P2 | Status: Done | Dependencies: ZH-012
- Scope: Restyle `src/zh/static` so the journal is easier to scan. Keep the loopback page offline, with no third-party assets. Do not add endpoints or change search, filter, or selection behaviour. Choosing a command still does not run it.
- Acceptance: Existing browser tests pass. The page still says that choosing a command does not run it. Search, status, theme, results, detail, copy, and empty states stay labelled and keyboard-reachable in light and dark themes. Command text is still rendered as text.
- Validation: `python -m pytest tests/unit/test_browser.py -q` — 6 passed. Browser check: search, selection, detail, copy control, empty search, light theme, and a 390px-wide header.

**ZH-014: History range, graph, header and footer**

- Priority: P1 | Status: Done | Dependencies: ZH-012
- Scope: Filter the journal by a start, an end, or both, including presets for the last hour, last 24 hours, last day, and last 7 days. Add a graph of command counts for that range. Put page options in the header and add a footer. Keep the page usable on a narrow screen. No third-party assets. Choosing a command still does not run it.
- Acceptance: Search, stats, and the graph honour the same range. A start after the end or a bad timestamp is rejected. Presets and custom from/to work, including from-only and to-only. The graph renders as text-backed markup, not executable journal content. Header options switch between commands and the graph. The layout remains usable at 390px width. Existing browser tests still pass.
- Validation: `python -m pytest tests/unit/test_search.py tests/unit/test_browser.py -q` — 10 passed. Browser check: last hour, from-only, to-only, invalid range, graph, command detail, and a 390px header.

**ZH-016: Command origin hostname and user**

- Priority: P1 | Status: Done | Dependencies: ZH-003, ZH-012
- Scope: Store the machine hostname and the user who ran each command. Migration v3 adds `username` to `commands` and `command_revisions`. The shell hook records both when a command starts. The browser list and detail show them as text. Older rows keep a null user. Choosing a command still does not run it.
- Acceptance: A recorded command keeps the hostname and username. A version 2 journal migrates without losing commands. Search and command detail return both fields. The page shows them as text. A missing user or hostname is shown as unknown. Existing browser tests still pass.
- Validation: `python -m pytest -q` — 49 passed, 1 skipped. Hook test stores `socket.gethostname()` and `getpass.getuser()`. Browser API returns `origin-host` and `origin-user`, and the page script includes the Host and User labels. `python -m ruff check` on the edited Python files — clean.
- Notes: Hostname was already written by the hook. This task makes it visible and adds the user. Restart `zh browse` to load the new page. Commands recorded before this change have no user.

**ZH-015: Grafana-style browser chrome**

- Priority: P2 | Status: Done | Dependencies: ZH-014
- Scope: Restyle the history page toolbar, header, theme control, and graph. Keep the existing colors. Search, status, and the time range sit in one toolbar. The time range opens as a menu with presets and a from/to calendar. The theme control is an icon. The graph uses a gridded time series. No third-party assets. Choosing a command still does not run it.
- Acceptance: Existing browser tests pass. The page still says that choosing a command does not run it and still offers the time presets. Header navigation and the theme icon work in light and dark themes. The toolbar and graph remain usable at 390px width. A start after the end is still rejected.
- Validation: `python -m pytest tests/unit/test_search.py tests/unit/test_browser.py -q` — 10 passed. `node --check src/zh/static/browse.js` ok. Browser check: toolbar, time menu, last hour, graph axis, light theme icon, and a 390px header.

**ZH-017: Search field focus**

- Priority: P2 | Status: Done | Dependencies: ZH-015
- Scope: Draw the search focus ring around the whole search field, including the icon and the `/` hint. Keep the existing colors. Do not change search behaviour.
- Acceptance: Focusing the search field highlights the full control. The inner text box has no separate ring. Keyboard focus stays visible. Existing browser tests still pass.
- Validation: `python -m pytest tests/unit/test_browser.py -q` — 6 passed. Browser check: focused search field border and ring wrap the icon, text, and `/` hint. The input outline is none.

**ZH-023: Import the current Zsh history**

- Priority: P0 | Status: Done | Dependencies: ZH-002, ZH-004, ZH-008
- Scope: `zh import` with no file, and `zh import --zsh [PATH]`, read the current Zsh history (`HISTFILE`, otherwise `~/.zsh_history`) into the journal. Parse the file the way Zsh reads it: extended-history timestamps and durations, backslash-newline continuations, the extra space Zsh writes after a trailing backslash, a leading `\:`, and metafied bytes. Do not write `HISTFILE`. Importing the same file again does not insert those commands a second time. A crash part-way through can be retried. Imported rows are not copied back into the shell history and are not pushed as new local executions. A missing exit code on a finished history row is not shown as still running.
- Acceptance: Extended, plain, multiline, trailing-backslash, colon, and non-ASCII history lines become the original command text, with timestamp and duration when the file has them. The history file bytes stay unchanged. A second import adds nothing. Deleting one imported row and importing again restores that row. A remote import is still staged for the shell; a Zsh-file import is not. Leading-space and excluded commands are not stored. `zh import FILE` still reads JSON lines.
- Validation: `python -m ruff check src tests server` clean. `python -m pytest -q` — 60 passed, 1 skipped. `node --check src/zh/static/browse.js` ok. `zsh -n zsh/completions/_zh` ok. An interactive Zsh wrote an extended history file and the parser returned those commands, including a real newline, a trailing backslash, a leading colon, `café`, and two identical lines.
- Notes: Schema version is 4. `import_kind=zsh` keeps a history-file row out of the shell refresh. Zsh history has no exit code. A line that starts with `:` and is not extended-history metadata has no command, which matches Zsh's reader. Re-import identity is the timestamp, duration, command, and how many times that triple has already appeared. A later rewrite of `HISTFILE` can change that identity. This is not exactly-once import across a history rewrite. Lines with no timestamp are ordered from the file's modification time. Pause does not block this import.

**ZH-019: Zsh completion for zh**

- Priority: P1 | Status: Done | Dependencies: ZH-001, ZH-004
- Scope: Loading `zsh/zh.plugin.zsh` adds `zsh/completions` to `fpath` and registers `_zh` when the completion system is already active. `zh <Tab>` lists subcommands. `zh <subcommand> <Tab>` lists that command's flags. Hidden `_hook` is not offered. Keybindings stay unchanged.
- Acceptance: After the plugin loads in a Zsh session with completion enabled, `zh` completes subcommands and the flags defined by the CLI. A session that loads the plugin before `compinit` still exposes the completion directory. Repeated loading does not register the completer twice. `zsh -n` passes.
- Validation: `zsh -n zsh/completions/_zh` and `zsh -n zsh/zh.plugin.zsh` passed. `python -m pytest -q` — 52 passed, 1 skipped. `python -m ruff check tests/zsh/test_completion.py` clean. Tab lists subcommands and omits `_hook`. `zh search -` lists `--status` and `--fzf`. `zh search --status` lists all, failed, ok, and running. Loading the plugin before `compinit` still registers `_zh`. A second source does not grow `fpath`.
- Notes: `zsh/completions/_zh` previously named only the subcommands and was not attached by the plugin.

**ZH-018: Theme icon follows the active theme**

- Priority: P2 | Status: Done | Dependencies: ZH-015
- Scope: Show the moon while the page is dark and the sun while it is light. Keep the existing colors. Do not change theme storage.
- Acceptance: The visible icon matches `data-theme`. The button name matches that theme. Switching themes swaps the icon. Existing browser tests still pass.
- Validation: `python -m pytest tests/unit/test_browser.py -q` — 6 passed. `node --check src/zh/static/browse.js` ok. Dark theme shows the moon. Light theme shows the sun.

**ZH-022: Plugin icon**

- Priority: P2 | Status: Done | Dependencies: ZH-013
- Scope: Add one ZH icon and show it in the history page header, as the page icon, and at the top of the README. Serve it from the loopback page with the other static files. Keep the page offline. Choosing a command still does not run it.
- Acceptance: The header shows the icon with accessible name ZH. The browser tab uses the same file. The README shows that file. The icon request returns an SVG from this host. Existing browser tests still pass.
- Validation: `python -m pytest tests/unit/test_browser.py -q` — 6 passed. `python -m ruff check src/zh/browser.py tests/unit/test_browser.py` — clean. Headless Chrome screenshots of `http://127.0.0.1:8765/` show the icon beside History in light theme, dark theme, and a 390px-wide header. The page still says that choosing a command does not run it.
- Notes: Replaces the text badge in the header. The page title stays History. The README image is `src/zh/static/icon.svg`.

**ZH-020: Graph stays in its panel for one bucket**

- Priority: P1 | Status: Done | Dependencies: ZH-014
- Scope: Keep the activity chart a fixed height when the range has one bucket. A single command draws a narrow bar on a normal grid. Do not change the activity counts.
- Acceptance: One matching command does not stretch the graph to the viewport. The chart stays about 208px tall. Several buckets still scroll sideways when they do not fit. Existing browser tests still pass.
- Validation: `python -m pytest tests/unit/test_browser.py -q` — 6 passed. One command: chart 773×208, bar 32px wide, panel 384px tall in a 728px viewport. Four commands stay 208px tall.

**ZH-021: User documentation for install and the plugin**

- Priority: P1 | Status: Done | Dependencies: ZH-001, ZH-003, ZH-004
- Scope: Rewrite `README.md` so a reader can install `zh`, keep it on `PATH`, load the plugin for every shell, and see what is recorded, which commands exist, and where sync and privacy are documented. Align `docs/install.md` with that install path.
- Acceptance: `README.md` states the `PATH` requirement, standalone and Oh My Zsh loads, recorded fields, skip rules, config keys that exist, the `zh browse` command that serves the page, and links to `docs/install.md`, `docs/sync.md`, `docs/security.md`, and `docs/release.md`. `docs/install.md` states that a new shell records only when `zh` is on `PATH`.
- Validation: Checked against `zsh/zh.plugin.zsh`, `zsh/hooks.zsh`, `src/zh/cli.py`, `src/zh/config.py`, `src/zh/hooks.py`, and `src/zh/database.py`. Pytest was not run; the change is documentation.
- Notes: The README also shows the existing `src/zh/static/icon.svg`. `paused` is changed with `zh pause` and `zh resume`. `zh config set` rejects that key.

**ZH-024: Document zh tab completion**

- Priority: P1 | Status: Done | Dependencies: ZH-019, ZH-021
- Scope: Say in `README.md` and `docs/install.md` how Tab completion is registered, which words it offers, and that a shell which already loaded the plugin must unset `ZH_PLUGIN_LOADED` and source it again.
- Acceptance: Both pages say Tab lists `zh` subcommands and the flags for the current subcommand, that `_hook` is omitted, and that Oh My Zsh picks the completer up on the next shell. Neither page invents commands or files.
- Validation: Checked `README.md` and `docs/install.md` against `zsh/zh.plugin.zsh` and `zsh/completions/_zh`. Pytest was not run; the change is documentation.

**ZH-025: Document database sync setup and secret storage**

- Priority: P1 | Status: Done | Dependencies: ZH-005, ZH-006, ZH-021
- Scope: Extend `docs/sync.md` and `docs/security.md` so a reader can start the API against PostgreSQL or server SQLite, register a device, and see where the bootstrap token, device token, and `DATABASE_URL` are stored. Point at that from `README.md`.
- Acceptance: The pages name `zh-server`, `DATABASE_URL`, `ZH_SERVER_DB`, `zh devices register`, the config path, the `sync-enabled` marker, and that the server stores SHA-256 hashes. They do not invent files or settings.
- Validation: Checked `docs/sync.md`, `docs/security.md`, `README.md`, and `docs/install.md` against `server/app/main.py`, `server/app/store.py`, `src/zh/config.py`, `src/zh/cli.py`, and `pyproject.toml`. Pytest was not run; the change is documentation.
- Notes: `DATABASE_URL` is process environment only. The device token is plaintext in the client config. The server stores SHA-256 hashes.

**ZH-026: Link the docs from the README**

- Priority: P2 | Status: Done | Dependencies: ZH-021, ZH-025
- Scope: Add a documentation section to `README.md` that links `docs/install.md`, `docs/sync.md`, `docs/security.md`, and `docs/release.md`.
- Acceptance: Each page under `docs/` is linked from `README.md`. The blurbs match those pages. No new pages are created.
- Validation: `docs/` contains `install.md`, `sync.md`, `security.md`, and `release.md`. Each is linked from the Documentation section in `README.md`. Pytest was not run; the change is documentation.
- Notes:

**ZH-027: Document the Git install and ~/.zshrc lines**

- Priority: P1 | Status: Done | Dependencies: ZH-001, ZH-021
- Scope: Rewrite the install sections in `README.md` and `docs/install.md` so a reader clones `https://github.com/stiliajohny/zh-zsh-history`, installs the `zh` command in a virtualenv, and adds the `PATH` and `source` lines to `~/.zshrc`. Keep the checkout install for work in this repository, and the Oh My Zsh link. `pip` installs the command only; the plugin stays in the clone.
- Acceptance: Both pages show the clone, `python -m pip install -e .`, and a `~/.zshrc` block with `PATH` before `source` of `zsh/zh.plugin.zsh`. The `dev` extra stays on the checkout path. No commands or files are invented.
- Validation: Checked `README.md` and `docs/install.md` against `pyproject.toml` (`zh` script, `sync`/`server`/`dev` extras) and `zsh/zh.plugin.zsh` (sources `hooks.zsh` and `history.zsh`, second load is a no-op). Pytest was not run; the change is documentation.
- Notes: The GitHub remote `stiliajohny/zh-zsh-history` was created in this session. A clone is empty until the first push.

**ZH-028: Install the command with pipx, not a repo virtualenv**

- Priority: P1 | Status: Done | Dependencies: ZH-027
- Scope: Change the Git install in `README.md` and `docs/install.md`. The clone supplies `zsh/zh.plugin.zsh`. `pipx install --editable` installs the `zh` command outside the clone. `~/.zshrc` sources the plugin and does not point at `.venv`. The gitignored `.venv` stays the way to run tests in this checkout.
- Acceptance: Neither install page tells a shell to put `$HOME/zh-zsh-history/.venv/bin` on `PATH`. Both pages show the pipx install and a `source` of `zsh/zh.plugin.zsh`. The hook still requires `zh` on `PATH`.
- Validation: Checked `README.md` and `docs/install.md` against `.gitignore` (`.venv/` ignored), `pyproject.toml` (script `zh`, extras `sync` and `server`), and `zsh/hooks.zsh` (returns when `zh` is not on `PATH`). Pytest was not run; the change is documentation.
- Notes: pipx keeps its own environment and puts `zh` in `~/.local/bin` by default.

**ZH-029: Fix import order and add pre-commit**

- Priority: P1 | Status: Done | Dependencies: ZH-011
- Scope: Rewrite the multi-line imports Ruff rejects, and add a pre-commit config that runs the same `ruff check` as CI on staged Python files. Install `pre-commit` with the `dev` extra. Document how to turn the hook on in this checkout.
- Acceptance: `python -m ruff check src tests server` is clean. `.pre-commit-config.yaml` runs Ruff 0.17.5, the same pin as the `dev` extra. `README.md` and `docs/install.md` say to run `pre-commit install` after the `dev` extra. The hook does not format unrelated files.
- Validation: `python -m ruff check src tests server` clean. `pre-commit install` wrote `.git/hooks/pre-commit`. `pre-commit run --all-files` — ruff check passed. `python -m pytest -q` — 60 passed, 1 skipped.
- Notes: The hook is `ruff-check` with `--fix`. Ruff is pinned to 0.17.5 in `pyproject.toml` and in the hook `rev`. Bump both together. The formatter hook is not enabled.

**ZH-030: Install the plugin under Oh My Zsh custom**

- Priority: P1 | Status: Done | Dependencies: ZH-001, ZH-028
- Scope: Change the Git install in `README.md` and `docs/install.md`. Clone into `$ZSH_CUSTOM/zh-zsh-history` (`~/.oh-my-zsh/custom` when `ZSH_CUSTOM` is unset). Link the clone's `zsh/` directory as `$ZSH_CUSTOM/plugins/zh`, because Oh My Zsh loads `plugins/zh/zh.plugin.zsh` and that file lives in `zsh/` beside `hooks.zsh`, `history.zsh`, and `completions/`. `pipx install --editable` still installs the `zh` command. A shell without Oh My Zsh clones under `~/.local/share` and sources `zsh/zh.plugin.zsh`.
- Acceptance: Neither install page tells the reader to clone into `$HOME/zh-zsh-history`. Both pages show the custom-directory clone, the directory link, `pipx install --editable`, and `zh` in `plugins=(...)` with no extra `source`. Both pages still show a source line for a shell that does not use Oh My Zsh. The hook still requires `zh` on `PATH`.
- Validation: Checked `README.md` and `docs/install.md` against `zsh/zh.plugin.zsh` (`${0:A:h}` resolves the link and sources `hooks.zsh` and `history.zsh` beside the real file; completions load from that directory). Pytest was not run; the change is documentation.
- Notes: A clone placed directly in `plugins/zh` would leave `zh.plugin.zsh` at `plugins/zh/zsh/zh.plugin.zsh`, which Oh My Zsh does not load. The `plugins=(git zh)` line is the stock Oh My Zsh list with `zh` added.

### M5 — Release

**ZH-011: Hardening and release**

- Priority: P1 | Status: Done | Dependencies: ZH-001–ZH-010
- Acceptance: Linux/macOS and Oh My Zsh tests, concurrency/crash tests, 100k/1m-row benchmarks, documentation, install/uninstall, security review and release checklist.
- Validation: `python -m ruff check src tests server` — clean. `python -m pytest -q` — 49 passed, 1 skipped. `zsh -n` passed for the plugin, hooks, history, and completion files. `ZH_BENCH=100000` insert 6.434s, search 0.042s. `ZH_BENCH=1000000` insert 76.880s, search 0.482s. Install, sync, security, and release notes are in `docs/`. CI is `.github/workflows/ci.yml`. Live PostgreSQL was not available.

## In progress

None.

## Completed

- ZH-001: Package, tooling and Oh My Zsh loader
- ZH-002: SQLite schema and migrations
- ZH-003: ZSH lifecycle hooks
- ZH-004: CLI, FTS5 and analytics
- ZH-005: FastAPI/PostgreSQL service and device auth
- ZH-006: Incremental bidirectional sync
- ZH-007: Background scheduling
- ZH-008: Native history import and refresh
- ZH-009: Privacy, retention and deletion
- ZH-010: Explicit PTY output recording
- ZH-011: Hardening and release
- ZH-012: Loopback history browser
- ZH-013: History browser visual design
- ZH-014: History range, graph, header and footer
- ZH-015: Grafana-style browser chrome
- ZH-016: Command origin hostname and user
- ZH-017: Search field focus
- ZH-018: Theme icon follows the active theme
- ZH-019: Zsh completion for zh
- ZH-020: Graph stays in its panel for one bucket
- ZH-021: User documentation for install and the plugin
- ZH-022: Plugin icon
- ZH-023: Import the current Zsh history
- ZH-024: Document zh tab completion
- ZH-025: Document database sync setup and secret storage
- ZH-026: Link the docs from the README
- ZH-027: Document the Git install and ~/.zshrc lines
- ZH-028: Install the command with pipx, not a repo virtualenv
- ZH-029: Fix import order and add pre-commit
- ZH-030: Install the plugin under Oh My Zsh custom

## Task log

| Date       | ID            | Work                                        | Files                                                                                                                                                     | Tests and evidence                                                                                                                                                                                                       | Remaining    |
| ---------- | ------------- | ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------ |
| 2026-11-18 | —             | Initial plan                                | AGENTS.md, PLAN.md                                                                                                                                        | Planning only                                                                                                                                                                                                            | Start ZH-001 |
| 2026-11-18 | ZH-012        | Loopback history browser                    | src/zh/browser.py, src/zh/static, src/zh/database.py, src/zh/search.py, src/zh/cli.py, tests/unit                                                         | `python -m pytest -q` 21 passed; `python -m ruff check src tests` clean                                                                                                                                                  | ZH-001       |
| 2026-11-18 | ZH-013        | Restyle the loopback history page           | src/zh/static/browse.html, browse.css, browse.js                                                                                                          | `python -m pytest tests/unit/test_browser.py -q` — 6 passed. Checked search, detail, empty results, light theme, and a 390px header in the browser.                                                                      | ZH-001       |
| 2026-11-18 | ZH-001        | Session-only Zsh plugin loader              | zsh/zh.plugin.zsh, tests/zsh/test_plugin.py, README.md                                                                                                    | `zsh -n zsh/zh.plugin.zsh` ok. `python -m pytest -q` — 26 passed. `python -m ruff check tests/zsh/test_plugin.py` clean. Full `ruff check src tests` still has four pre-existing E501 lines in `src/zh/database.py`.     | ZH-002       |
| 2026-11-18 | ZH-014        | Time range, graph, header, footer           | src/zh/search.py, src/zh/browser.py, src/zh/static/browse.html, browse.css, browse.js, tests/unit, README.md                                              | `python -m pytest tests/unit/test_search.py tests/unit/test_browser.py -q` — 10 passed. Browser check of presets, from-only, to-only, graph, detail, and a 390px header.                                                 | ZH-003       |
| 2026-11-18 | ZH-003        | Record shell commands from Zsh hooks        | zsh/hooks.zsh, src/zh/capture.py, tests/zsh/test_shell_record.py, tests/unit/test_capture.py, README.md                                                   | `python -m pytest -q` — 48 passed, 1 skipped. Interactive zsh test stored `false` with exit 1 and a multiline command, left HISTFILE unchanged, and kept another precmd hook.                                            | ZH-004       |
| 2026-11-18 | ZH-015        | Grafana-style history chrome                | src/zh/static/browse.html, browse.css, browse.js                                                                                                          | `python -m pytest tests/unit/test_search.py tests/unit/test_browser.py -q` — 10 passed. Browser check of the toolbar, time menu, graph, theme icons, and a 390px header.                                                 | ZH-016       |
| 2026-11-18 | ZH-016        | Store hostname and user on each command     | src/zh/database.py, src/zh/hooks.py, src/zh/search.py, src/zh/cli.py, src/zh/static/browse.js, browse.css, tests/unit, README.md                          | `python -m pytest -q` — 49 passed, 1 skipped. A hook record keeps the machine hostname and login name. The command API returns both.                                                                                     | ZH-017       |
| 2026-11-18 | ZH-017        | Search field focus ring                     | src/zh/static/browse.css                                                                                                                                  | `python -m pytest tests/unit/test_browser.py -q` — 6 passed. Focus ring wraps the search icon, text, and `/` hint.                                                                                                       | ZH-011       |
| 2026-11-18 | ZH-002–ZH-011 | Journal, sync, privacy, recording, release  | src/zh, server, zsh, deploy, docs, tests, .github/workflows/ci.yml                                                                                        | `python -m ruff check src tests server` clean. `python -m pytest -q` — 49 passed, 1 skipped. Benchmarks: 100k search 0.042s, 1m search 0.482s. Live PostgreSQL was not running.                                          | None         |
| 2026-11-18 | ZH-018        | Theme icon follows the active theme         | src/zh/static/browse.html, browse.css, browse.js                                                                                                          | `python -m pytest tests/unit/test_browser.py -q` — 6 passed. Dark shows the moon. Light shows the sun.                                                                                                                   | None         |
| 2026-11-18 | ZH-020        | Keep a one-bucket graph in its panel        | src/zh/static/browse.js, browse.css                                                                                                                       | `python -m pytest tests/unit/test_browser.py -q` — 6 passed. One command draws a 32px bar in a 208px chart.                                                                                                              | ZH-019       |
| 2026-11-18 | ZH-021        | Document install and the plugin             | README.md, docs/install.md                                                                                                                                | Checked against the plugin, hooks, CLI, config, and journal path. Pytest was not run; the change is documentation.                                                                                                       | ZH-020       |
| 2026-11-18 | ZH-019        | Zsh completion for zh                       | zsh/zh.plugin.zsh, zsh/completions/\_zh, tests/zsh/test_completion.py, README.md, docs/install.md                                                         | `zsh -n` passed. `python -m pytest -q` — 52 passed, 1 skipped. Tab lists subcommands and search flags, and omits `_hook`.                                                                                                | ZH-020       |
| 2026-11-18 | ZH-022        | Plugin icon in the header and README        | src/zh/static/icon.svg, browse.html, browse.css, src/zh/browser.py, README.md, tests/unit/test_browser.py                                                 | `python -m pytest tests/unit/test_browser.py -q` — 6 passed. Ruff clean. Header icon checked in light, dark, and a 390px width.                                                                                          | None         |
| 2026-11-18 | ZH-023        | Import the current Zsh history              | src/zh/zsh_history.py, database.py, history.py, cli.py, search.py, static/browse.js, browse.css, zsh/completions/\_zh, tests, README.md, docs/security.md | `python -m ruff check src tests server` clean. `python -m pytest -q` — 60 passed, 1 skipped. Interactive Zsh history round-trip matched multiline, backslash, colon, and non-ASCII commands. `HISTFILE` bytes unchanged. | None         |
| 2026-11-18 | ZH-024        | Document zh tab completion                  | README.md, docs/install.md                                                                                                                                | Checked both pages against `zsh/zh.plugin.zsh` and `zsh/completions/_zh`. Pytest was not run; the change is documentation.                                                                                               | None         |
| 2026-11-19 | ZH-025        | Document database sync and secret storage   | docs/sync.md, docs/security.md, docs/install.md, README.md                                                                                                | Checked against `server/app/main.py`, `server/app/store.py`, `src/zh/config.py`, `src/zh/cli.py`, and `pyproject.toml`. Pytest was not run; the change is documentation.                                                 | None         |
| 2026-11-19 | ZH-026        | Link the docs from the README               | README.md, PLAN.md                                                                                                                                        | `docs/` contains install, sync, security, and release. Each is linked from the Documentation section. Pytest was not run; the change is documentation.                                                                   | None         |
| 2026-11-19 | ZH-027        | Document the Git install and ~/.zshrc lines | README.md, docs/install.md, PLAN.md                                                                                                                       | Checked both pages against `pyproject.toml` and `zsh/zh.plugin.zsh`. Pytest was not run; the change is documentation. The GitHub remote is empty until the first push.                                                   | None         |
| 2026-11-19 | ZH-028        | Install zh with pipx, not a repo virtualenv | README.md, docs/install.md, PLAN.md                                                                                                                       | Checked both pages against `.gitignore`, `pyproject.toml`, and `zsh/hooks.zsh`. Pytest was not run; the change is documentation.                                                                                         | None         |
| 2026-11-19 | ZH-029        | Fix import order and add pre-commit         | src/zh/cli.py, tests/unit/test_database.py, .pre-commit-config.yaml, pyproject.toml, README.md, docs/install.md, docs/release.md                          | `python -m ruff check src tests server` clean. `pre-commit run --all-files` passed. `python -m pytest -q` — 60 passed, 1 skipped.                                                                                        | None         |
| 2026-11-19 | ZH-030        | Install the plugin under Oh My Zsh custom   | README.md, docs/install.md, PLAN.md                                                                                                                       | Checked both pages against `zsh/zh.plugin.zsh`. Pytest was not run; the change is documentation.                                                                                                                         | None         |

## Template for newly discovered work

```markdown
### ZH-### — Title

- Priority: P0/P1/P2 | Status: Planned | Dependencies: IDs or None
- Scope:
- Acceptance:
- Validation: Pending
- Notes:
```

## Implementation references

- ZSH hooks: https://zsh.sourceforge.io/Doc/Release/Functions.html
- ZSH options: https://zsh.sourceforge.io/Doc/Release/Options.html
- ZSH builtins (`fc`): https://zsh.sourceforge.io/Doc/Release/Shell-Builtin-Commands.html
- SQLite WAL: https://www.sqlite.org/wal.html
- SQLite FTS5: https://www.sqlite.org/fts5.html
- PostgreSQL transactions: https://www.postgresql.org/docs/current/transaction-iso.html
- FastAPI: https://fastapi.tiangolo.com/
- Atuin reference: https://github.com/atuinsh/atuin
