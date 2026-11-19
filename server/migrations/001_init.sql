-- Version 1 sync schema for PostgreSQL.
-- Sequence numbers are reserved in a committed row before the event commits.
-- A pull does not advance past a lower sequence that is still pending.

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    bootstrap_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS server_devices (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts (id),
    name TEXT NOT NULL,
    hostname TEXT,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS account_counters (
    account_id TEXT PRIMARY KEY REFERENCES accounts (id),
    next_seq BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    account_id TEXT NOT NULL REFERENCES accounts (id),
    seq BIGINT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, seq)
);

CREATE TABLE IF NOT EXISTS server_events (
    account_id TEXT NOT NULL REFERENCES accounts (id),
    event_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    seq BIGINT NOT NULL,
    PRIMARY KEY (account_id, event_id, revision),
    UNIQUE (account_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_server_events_seq ON server_events (account_id, seq);
