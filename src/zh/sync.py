"""Incremental sync client. The shell never waits on this module."""

from __future__ import annotations

import fcntl
import random
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from zh.config import load_config, refresh_sync_marker
from zh.database import (
    apply_remote_batch,
    connect,
    ensure_local_device,
    get_sync_value,
    init_db,
    mark_events_synced,
    pending_events,
    set_sync_value,
    upsert_remote_device,
)
from zh.output import output_dir

DEFAULT_TIMEOUT = 10.0


class SyncError(Exception):
    """A sync request failed."""

    def __init__(self, message: str, *, retry: bool) -> None:
        super().__init__(message)
        self.retry = retry


def backoff_seconds(attempt: int, random_source: random.Random | None = None) -> float:
    """Return a capped exponential delay with jitter."""
    rng = random_source or random.Random()
    base = min(300.0, 2.0 ** max(attempt, 0))
    delay = base + rng.uniform(0, base * 0.1)
    return min(300.0, delay)


def sync_once(
    db_path: Path,
    *,
    config: dict | None = None,
    transport=None,
    attempts: int = 3,
    sleep=None,
    random_source: random.Random | None = None,
) -> dict:
    """Push pending local events and pull remote events."""
    settings = config if config is not None else load_config()
    refresh_sync_marker(db_path, settings)
    if not settings.get("sync_enabled"):
        return {"status": "disabled"}
    if not settings.get("server_url") or not settings.get("device_token"):
        return {"status": "unconfigured"}
    lock = SyncLock(db_path.expanduser().parent / "sync.lock")
    if not lock.acquire():
        return {"status": "busy"}
    try:
        return _sync_with_retries(
            db_path,
            settings,
            transport=transport,
            attempts=attempts,
            sleep=sleep or (lambda _seconds: None),
            random_source=random_source,
        )
    finally:
        lock.release()


def sync_kick(db_path: Path, *, popen=subprocess.Popen) -> str:
    """Start a detached sync when one is due. Returns immediately."""
    settings = load_config()
    refresh_sync_marker(db_path, settings)
    ready = (
        settings.get("sync_enabled")
        and settings.get("server_url")
        and settings.get("device_token")
    )
    if not ready:
        return "disabled"
    path = init_db(db_path)
    connection = connect(path, timeout=0.2)
    try:
        nxt = get_sync_value(connection, "next_attempt_at")
    finally:
        connection.close()
    if nxt and _parse_time(nxt) > datetime.now(UTC):
        return "waiting"
    popen(
        [sys.executable, "-m", "zh", "sync"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return "started"


def register_device(
    server_url: str,
    bootstrap_token: str,
    name: str,
    hostname: str,
    transport=None,
) -> dict:
    """Register this install and return the one-time device credential."""
    with _client(server_url, bootstrap_token, transport) as client:
        response = client.post(
            "/v1/devices/register",
            json={"name": name, "hostname": hostname},
        )
        _raise_for(response)
        return response.json()


def revoke_device(server_url: str, device_token: str, device_id: str, transport=None) -> None:
    """Revoke a device in this account."""
    with _client(server_url, device_token, transport) as client:
        response = client.delete(f"/v1/devices/{device_id}")
        _raise_for(response)


def list_remote_devices(server_url: str, device_token: str, transport=None) -> list[dict]:
    """Return devices visible to this credential."""
    with _client(server_url, device_token, transport) as client:
        response = client.get("/v1/devices")
        _raise_for(response)
        body = response.json()
        if not isinstance(body, list):
            raise SyncError("device list was not a list", retry=False)
        return body


class SyncLock:
    """A non-blocking exclusive lock so two sync workers do not overlap."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        """Return True when this process holds the lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            return False
        return True

    def release(self) -> None:
        """Release the lock."""
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def _sync_with_retries(db_path, settings, *, transport, attempts, sleep, random_source) -> dict:
    last_error = "sync failed"
    for attempt in range(attempts):
        try:
            summary = _push_and_pull(db_path, settings, transport)
            _remember(db_path, "last_error", "")
            _remember(db_path, "last_success", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
            _remember(db_path, "next_attempt_at", "")
            summary["status"] = "ok"
            return summary
        except SyncError as exc:
            last_error = str(exc)
            _remember(db_path, "last_error", last_error)
            if not exc.retry or attempt + 1 == attempts:
                break
            delay = backoff_seconds(attempt, random_source)
            nxt = datetime.now(UTC).timestamp() + delay
            _remember(db_path, "next_attempt_at", str(nxt))
            sleep(delay)
    return {"status": "error", "error": last_error}


def _push_and_pull(db_path: Path, settings: dict, transport) -> dict:
    path = init_db(db_path)
    pushed = 0
    pulled = 0
    with _client(settings["server_url"], settings["device_token"], transport) as client:
        connection = connect(path)
        try:
            device_id = settings.get("device_id") or ensure_local_device(connection)
            batch_size = int(settings["sync_batch_size"])
            while True:
                batch = pending_events(connection, batch_size)
                if not batch:
                    break
                for event in batch:
                    event["device_id"] = device_id
                body = {"events": _wire_events(batch)}
                response = _request(lambda: client.post("/v1/events/push", json=body))
                _raise_for(response)
                body = response.json()
                acked = body.get("acked") or []
                acked_keys = {(item["id"], int(item["revision"])) for item in acked}
                mark_events_synced(
                    connection,
                    [
                        event
                        for event in batch
                        if (event["id"], int(event["revision"])) in acked_keys
                    ],
                )
                pushed += len(acked_keys)
                if not acked_keys:
                    raise SyncError("server rejected the batch", retry=False)
                if len(batch) < batch_size:
                    break
            cursor = get_sync_value(connection, "pull_cursor") or "0"
            while True:
                response = _request(
                    lambda: client.get(
                        "/v1/events/pull", params={"cursor": cursor, "limit": batch_size}
                    )
                )
                _raise_for(response)
                body = response.json()
                events = body.get("events") or []
                next_cursor = str(body.get("next_cursor") or cursor)
                apply_remote_batch(
                    connection,
                    events,
                    next_cursor,
                    output_root=output_dir(path),
                )
                pulled += len(events)
                cursor = next_cursor
                if len(events) < batch_size:
                    break
        finally:
            connection.close()
        _cache_devices(path, client)
    return {"pushed": pushed, "pulled": pulled}


def _cache_devices(db_path: Path, client) -> None:
    try:
        response = client.get("/v1/devices")
        _raise_for(response)
        devices = response.json()
    except SyncError:
        return
    if not isinstance(devices, list):
        return
    connection = connect(db_path)
    try:
        for device in devices:
            upsert_remote_device(
                connection,
                device_id=device["id"],
                name=device.get("name") or device["id"],
                hostname=device.get("hostname"),
                revoked_at=device.get("revoked_at"),
            )
    finally:
        connection.close()


def _wire_events(events: list[dict]) -> list[dict]:
    wired = []
    for event in events:
        wired.append(
            {
                "id": event["id"],
                "revision": int(event["revision"]),
                "kind": event["kind"],
                "device_id": event["device_id"],
                "payload": event["payload"],
            }
        )
    return wired


def _remember(db_path: Path, key: str, value: str) -> None:
    connection = connect(init_db(db_path))
    try:
        set_sync_value(connection, key, value)
    finally:
        connection.close()


def _client(server_url: str, token: str, transport):
    import httpx

    if not server_url.startswith(("http://", "https://")):
        raise SyncError("server URL must start with http:// or https://", retry=False)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(
        base_url=server_url.rstrip("/"),
        headers=headers,
        timeout=httpx.Timeout(DEFAULT_TIMEOUT),
        transport=transport,
    )


def _request(call):
    import httpx

    try:
        return call()
    except httpx.TimeoutException as exc:
        raise SyncError("request timed out", retry=True) from exc
    except httpx.TransportError as exc:
        raise SyncError("server is unreachable", retry=True) from exc


def _raise_for(response) -> None:
    if response.status_code < 400:
        return
    retry = response.status_code >= 500
    detail = response.text.strip() or f"HTTP {response.status_code}"
    raise SyncError(detail[:300], retry=retry)


def _parse_time(value: str) -> datetime:
    if value.startswith("20") and "T" in value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    return datetime.fromtimestamp(float(value), UTC)
