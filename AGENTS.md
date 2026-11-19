# ZH — Agent Instructions

These instructions apply to every AI agent working in this repository, including Cursor.

## Required reading
Before starting any task, read `AGENTS.md`, `PLAN.md`, and relevant existing source files. `PLAN.md` is the single source of truth for the roadmap.

## Mandatory workflow
1. Identify the relevant task ID in `PLAN.md`.
2. If a feature, bug, refactor, or newly discovered requirement is missing, add it to `PLAN.md` **before implementing** it. Include a unique ID, priority, dependencies, scope, and testable acceptance criteria.
3. Mark the task `In Progress` before code changes. Avoid editing tasks concurrently without coordinating ownership.
4. Implement the smallest maintainable change; add or update tests and documentation.
5. Run relevant tests, lint and shell compatibility checks. If they cannot run, record why.
6. After completing a task, update `PLAN.md` with status, changed files, test evidence, risks and next steps. Only mark `Done` after acceptance criteria pass.
7. In the final response, include task IDs, changes, test results, and remaining issues.

**Interpretation:** Every new requirement discovered while executing shell commands must be added to the roadmap. Do not create roadmap items for routine commands (`ls`, `git status`, test runs) unless they uncover actionable work. Do not silently add scope.

## Engineering constraints
- Local-first: commands are recorded even without internet access.
- Never block shell prompt rendering or command execution on network I/O.
- Preserve standard ZSH Up/Down/Ctrl+R history, `fc`, user keybindings and existing hooks.
- Support Oh My Zsh via `zh.plugin.zsh` and standalone sourcing.
- Never overwrite `.zsh_history`; preserve user history settings by default.
- Keep shell hooks minimal; implement storage, search and sync in Python.
- SQLite locally (WAL); PostgreSQL remotely via a versioned API.
- Use globally unique event IDs, idempotent push and commit-safe pull cursors.
- Treat command completion as a revision or distinct event; do not lose exit codes when a command is synced before completion.
- Avoid timestamp-only sync, duplicate uploads, and re-export of imported commands.
- Make remote sync and output capture opt-in; filter secrets before local persistence.
- Do not capture the entire environment. Use an explicit allowlist.
- Output capture must be explicit PTY-based; do not wrap arbitrary ZSH syntax in `eval`.
- Never claim exactly-once native history import without crash-recovery tests.
- Avoid unnecessary services, dependencies or infrastructure.
- Never commit credentials, databases, terminal recordings or secrets.

## Quality gates
- Tests for new logic and failure scenarios.
- ZSH integration tests for hooks, exit codes, multiline commands, concurrent terminals and native history.
- Offline/retry/concurrency tests for sync.
- Documentation updated for changed behaviour and configuration.
- Security review for data capture, retention, authentication and deletion.

## Task report format
- Task: `ZH-###`
- Status: Planned / In Progress / Blocked / Done
- Changes: files and behaviour
- Validation: exact commands and outcomes
- Risks: unresolved issues
- Next: next roadmap item
