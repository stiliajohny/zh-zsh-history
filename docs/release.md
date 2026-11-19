# Release checklist

Run these from a virtual environment that has the `dev` extra installed, and `zsh` on `PATH`.

- `python -m ruff check src tests server`
- `pre-commit run --all-files` after `pre-commit install`
- `python -m pytest -q`
- `zsh -n zsh/zh.plugin.zsh`
- `zsh -n zsh/hooks.zsh`
- `zsh -n zsh/history.zsh`
- `zsh -n zsh/completions/_zh`
- `ZH_BENCH=100000 python -m pytest tests/integration/test_benchmark.py -q -s`
- `ZH_BENCH=1000000 python -m pytest tests/integration/test_benchmark.py -q -s`

Syntax is checked with `zsh -n`. ShellCheck has no Zsh dialect, so `shellcheck -s sh` reports the plugin's Zsh syntax and is not the syntax gate.

Confirm before a release:

- Sync is still off by default.
- `zh doctor` redacts `device_token`.
- `zh browse` rejects a non-loopback host.
- `zh sync install` and `zh sync uninstall` only add or remove the user scheduler.
- The server refuses plain HTTP on a public host unless `ZH_TLS_PROXY=1`.
- PostgreSQL is selected with `DATABASE_URL`. The SQL for that database is `server/migrations/001_init.sql`. The automated tests exercise the same statements on SQLite. On this machine `psql` is installed, and it could not connect to a server on the local socket, so that SQL file was not applied to a live database.
