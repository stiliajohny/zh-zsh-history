# Security

Recording and sync are local decisions. Sync stays disabled until config says otherwise. Filters run before a command is written.

## What is stored

A command is skipped when recording is paused, when it is empty, when it starts with whitespace and `ignore_space` is true (the default), or when it matches an `exclude_regex` pattern. An invalid pattern is skipped and `zh doctor` reports it. `zh config set exclude_regex PATTERN` adds one pattern and prints that a regex cannot guarantee a secret is removed. `zh config unset exclude_regex PATTERN` removes that pattern.

Environment capture keeps only names in `env_allowlist`. The default list is empty, so the environment is not stored. The full process environment is not copied into the journal.

The journal file mode is `0600`. A parent directory created for a new journal or config is `0700`. A symlink path and a file that is not SQLite are refused. Config mode is `0600`. `zh config`, `zh doctor`, and `zh status` print the device token as `***`.

The device token is stored in plaintext in that config file. The bootstrap token used at `zh devices register` is not written there. On the server, both are stored only as SHA-256: `accounts.bootstrap_hash` and `server_devices.token_hash`. `DATABASE_URL` is an environment variable of `zh-server` and is not written by this tool. Setup steps are in [sync.md](sync.md).

## Output

Interactive commands store stdout and stderr when `capture_output` is true, which is the default. The shell redirects the command's output to a PTY and a recorder copies every byte back to the terminal. The stored copy keeps at most `output_max_bytes` (default 1048576) and is marked truncated past that. The bytes are gzip files under an `outputs` directory next to the journal, mode `0600`. They are not encrypted and they are not synced. A shell whose stdout is not a terminal does not redirect. `zh config set capture_output false` leaves ordinary commands on the real terminal and stores no output file.

The PTY mixes stdout and stderr. Stdin stays on the real terminal, so a full-screen program that needs one device for the keyboard and the screen can be wrong. The command text is not passed to `eval`. A privacy filter can refuse to store the command, and then its output is not stored either. Output that is stored can still contain secrets printed by the command. `zh show ID --output` reads one local capture. The history page shows that text as text. Paths that leave the output directory are rejected.

`zh record -- COMMAND` runs a command in its own PTY and stores the same kind of file. That PTY is not an interactive user terminal. A privacy filter can refuse to store the command while the command itself still runs.

`zh clean` deletes commands older than the retention setting and removes local output files for those commands. Deletion writes a tombstone. Tombstones stay pending until sync acknowledges them, then expire after `tombstone_days` (default 30). `zh devices revoke` drops a remote device token.

## History

The plugin does not open or rewrite `HISTFILE`. Remote commands are added to the current shell history with `print -s`. If the shell stops after that add and before `zh _hook mark-history`, the next prompt can insert the same lines again in that session. The SQLite import record is written only after the shell reports the file was applied, so a crash retries the same staged file. That is not exactly-once history in the shell.

`zh import` and `zh import --zsh` read `HISTFILE` or `~/.zsh_history` and do not write it. The reader accepts extended-history timestamps and durations, a backslash before a newline, the extra space Zsh writes after a trailing backslash, a leading `\:`, and metafied bytes. Importing the same bytes again does not insert those commands a second time. If the import stops halfway, running it again stores the missing commands. Zsh can later rewrite `HISTFILE`, and that rewrite can change which rows match. Commands read from the history file are not copied back into the shell and are not pushed as new local executions. The file has no exit code, so those rows are finished commands with no exit code. A line that begins with `:` and is not extended-history metadata has no command, which is how Zsh reads that line. Leading-space commands and `exclude_regex` matches are skipped. Pause does not block this import.

## API

`GET /v1/health` does not require a token. Device registration, listing, revocation, push, and pull do. Registration authenticates with the bootstrap token and returns a new device token once. Later calls authenticate with that device token. A revoked token is rejected. Push batches larger than 500 events are rejected. The server bind refuses plain HTTP except on loopback, unless `ZH_TLS_PROXY=1`.
