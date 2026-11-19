# Sync

Sync is off until you turn it on. The shell never waits for the network. A background `zh sync` starts only when config has `sync_enabled`, `server_url`, and `device_token`, and a `sync-enabled` marker sits next to the journal.

The local journal stays SQLite. The API is a separate process. It uses PostgreSQL when `DATABASE_URL` starts with `postgres`, and a server SQLite file when `ZH_SERVER_DB` is set instead.

## Start the API

Install the `server` extra, then run `zh-server`. The client that registers and syncs needs the `sync` extra (`httpx`). The `dev` extra already includes `httpx`.

```bash
python -m pip install -e ".[server]"
DATABASE_URL="postgres://USER:PASSWORD@HOST:5432/DATABASE" zh-server
```

`DATABASE_URL` is read from the environment of that process. This project does not write it to a file. A URL that starts with `postgres` selects PostgreSQL and applies `server/migrations/001_init.sql` on startup. To use server SQLite instead, leave `DATABASE_URL` unset and set `ZH_SERVER_DB` to the server database file. If neither is set, `zh-server` exits.

The default bind is `127.0.0.1:8080`. `ZH_SERVER_HOST` and `ZH_SERVER_PORT` change that. Plain HTTP on any other host is refused unless `ZH_TLS_PROXY=1`, which means a TLS proxy is already in front of the process. `GET /v1/health` does not require a token.

## Register a device

You choose the bootstrap token. It is the account key: the first registration with that token creates the account, and the same token on another machine adds another device to it. A different token creates a different account. The client does not save the bootstrap token.

```bash
zh devices register \
  --server "http://127.0.0.1:8080" \
  --bootstrap-token "YOUR_BOOTSTRAP_TOKEN" \
  --name laptop
```

`--server` is required when `server_url` is not already in config. `--name` defaults to the hostname. The command sends the bootstrap token once as a bearer token. The server returns a device token and a device id. Registration writes `server_url`, `device_token`, `device_id`, and `sync_enabled` into the local config, then creates the `sync-enabled` marker. `zh devices revoke DEVICE_ID` rejects that device token afterwards.

The client sends the `device_id` from config. That is the server device, not the local journal device id. A device can push only its own id.

## Where credentials are stored

The device token is the only client secret. It is plaintext in the config file: `ZH_CONFIG`, or `$XDG_CONFIG_HOME/zh/config.json`, or `~/.config/zh/config.json`. The file mode is `0600`. `zh config`, `zh doctor`, and `zh status` print it as `***`.

The server does not keep the raw tokens. It stores SHA-256 of the bootstrap token in `accounts.bootstrap_hash` and SHA-256 of the device token in `server_devices.token_hash`. The device token is returned once, at registration.

`DATABASE_URL` stays in the server process environment. It is not part of the client config.

The marker is an empty `sync-enabled` file, mode `0600`, in the journal directory. That directory is `$XDG_DATA_HOME/zh`, or `~/.local/share/zh`, unless `ZH_DB` or `--db` points somewhere else. The marker is removed when sync is no longer fully configured.

## What moves

`zh sync` pushes local pending revisions and pending tombstones, at most `sync_batch_size` events (default 500). The server stores an event once for an account, event id, and revision. Only acknowledged ids are marked synced. If a batch is not acknowledged, the client stops that attempt instead of retrying the same failure forever.

Pull uses a cursor. The server reserves a sequence before an event is visible, and pull hides events at or beyond the oldest reservation that is still pending. A reservation older than 30 seconds is abandoned so a crash cannot stall the cursor. The client applies one pull batch and advances the local cursor in the same transaction. Imported commands stay `origin=import` and are not pushed again.

Timeouts are 10 seconds. Retryable failures wait with exponential backoff and jitter, capped at 300 seconds. HTTP 401 and 400 are not retried. A second sync exits while `sync.lock` is held.

## Schedule

`zh sync install` writes a user timer that runs `zh sync` every five minutes. On macOS that is a launchd agent labelled `com.zh.sync` with `StartInterval` 300. On Linux it is a systemd user oneshot and a timer with `OnBootSec=2min` and `OnUnitActiveSec=5min`. `zh sync` without `install` runs one sync in the foreground.

Copies of those units, with the executable name `zh`, are in `deploy/systemd` and `deploy/launchd`. `zh sync install` writes the same text with the `zh` path it was given.

## What is not synced

Captured output stays in gzip files next to the journal. Those files are not uploaded. Environment values are not copied from a remote event onto the local row. There is no client-side encryption; the server stores command text as the client sent it. Use HTTPS, or a TLS proxy with `ZH_TLS_PROXY=1`, for any host that is not loopback.
