"""Tests for the sync protocol and client."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from server.app.main import assert_bind, create_app
from server.app.store import open_sqlite

from zh.config import save_config
from zh.database import (
    apply_remote_batch,
    complete_command,
    connect,
    delete_command,
    get_command,
    get_sync_value,
    init_db,
    pending_events,
    record_command,
)
from zh.sync import SyncLock, backoff_seconds, register_device, sync_once


def test_backoff_is_capped_and_jittered() -> None:
    """Retry delays grow and stay within five minutes."""
    rng = random.Random(1)
    first = backoff_seconds(1, rng)
    assert 2 <= first <= 2.2
    assert backoff_seconds(20, random.Random(1)) == 300


def test_server_auth_idempotence_and_commit_safe_cursor(tmp_path: Path) -> None:
    """Devices authenticate, repeats do not duplicate, and a pending sequence blocks newer rows."""
    store = open_sqlite(tmp_path / "server.sqlite")
    app = create_app(store)
    transport = AppTransport(app)
    with httpx.Client(transport=transport, base_url="http://sync.test") as client:
        health = client.get("/v1/health")
        assert health.status_code == 200
        assert client.post("/v1/events/push", json={"events": []}).status_code == 401
    registered = register_device(
        "http://sync.test", "bootstrap-secret", "laptop", "host", transport
    )
    other = register_device("http://sync.test", "bootstrap-secret", "phone", "host", transport)
    assert registered["account_id"] == other["account_id"]
    assert registered["token"] != other["token"]
    event = _event("11111111-1111-1111-1111-111111111111", registered["device_id"], "echo one")
    again = _event("22222222-2222-2222-2222-222222222222", registered["device_id"], "echo two")
    bad = _event("33333333-3333-3333-3333-333333333333", registered["device_id"], "")
    with httpx.Client(
        transport=transport,
        base_url="http://sync.test",
        headers={"Authorization": f"Bearer {registered['token']}"},
    ) as client:
        first = client.post("/v1/events/push", json={"events": [event, bad]}).json()
        second = client.post("/v1/events/push", json={"events": [event]}).json()
        foreign = _event("44444444-4444-4444-4444-444444444444", other["device_id"], "nope")
        rejected = client.post("/v1/events/push", json={"events": [foreign]}).json()
    assert first["acked"] == [{"id": event["id"], "revision": 1}]
    assert first["rejected"]
    assert second["acked"] == [{"id": event["id"], "revision": 1}]
    assert rejected["acked"] == []
    rows = store.execute("SELECT COUNT(*) AS n FROM server_events").fetchone()["n"]
    assert rows == 1
    seq_gap = store.reserve(registered["account_id"])
    later = store.reserve(registered["account_id"])
    store.commit_reserved(
        registered["account_id"], later, registered["device_id"], again
    )
    blocked = store.pull(registered["account_id"], 0, 100, pending_timeout=timedelta(hours=1))
    visible = {item["id"] for item in blocked["events"]}
    assert again["id"] not in visible
    released = store.pull(
        registered["account_id"],
        0,
        100,
        now=datetime.now(UTC) + timedelta(hours=2),
    )
    visible = {item["id"] for item in released["events"]}
    assert event["id"] in visible
    assert again["id"] in visible
    assert seq_gap < int(released["next_cursor"])
    with httpx.Client(
        transport=transport,
        base_url="http://sync.test",
        headers={"Authorization": f"Bearer {registered['token']}"},
    ) as client:
        assert client.delete(f"/v1/devices/{other['device_id']}").status_code == 204
    with httpx.Client(
        transport=transport,
        base_url="http://sync.test",
        headers={"Authorization": f"Bearer {other['token']}"},
    ) as client:
        assert client.get("/v1/devices").status_code == 401
    try:
        assert_bind("203.0.113.10")
    except SystemExit as exc:
        assert "TLS" in str(exc)
    else:
        raise AssertionError("public bind was accepted")


def test_two_devices_sync_tombstones_and_retries(tmp_path: Path) -> None:
    """Peers exchange missing events, keep exit revisions, and recover after a disconnect."""
    store = open_sqlite(tmp_path / "server.sqlite")
    app = create_app(store)
    transport = AppTransport(app)
    laptop = register_device("http://sync.test", "bootstrap", "laptop", "a", transport)
    phone = register_device("http://sync.test", "bootstrap", "phone", "b", transport)
    left = init_db(tmp_path / "left.sqlite")
    right = init_db(tmp_path / "right.sqlite")
    left_config = _settings(laptop)
    right_config = _settings(phone)
    connection = connect(left)
    try:
        command_id = record_command(
            connection, command="echo shared", started_at=_stamp(0), cwd="/work"
        )
        complete_command(connection, command_id, exit_code=4, ended_at=_stamp(2))
    finally:
        connection.close()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("offline")
        return transport.handle_request(request)

    failed = sync_once(
        left,
        config=left_config,
        transport=httpx.MockTransport(handler),
        attempts=1,
        sleep=lambda _seconds: None,
    )
    assert failed["status"] == "error"
    connection = connect(left)
    try:
        assert pending_events(connection, 10)
        assert get_sync_value(connection, "last_error")
    finally:
        connection.close()
    synced = sync_once(
        left,
        config=left_config,
        transport=transport,
        attempts=2,
        sleep=lambda _seconds: None,
    )
    assert synced["status"] == "ok"
    pulled = sync_once(
        right, config=right_config, transport=transport, sleep=lambda _seconds: None
    )
    assert pulled["status"] == "ok"
    connection = connect(right)
    try:
        stored = get_command(connection, command_id)
        assert stored is not None
        assert stored["exit_code"] == 4
        assert stored["origin"] == "import"
        assert pending_events(connection, 10) == []
    finally:
        connection.close()
    before = store.execute("SELECT COUNT(*) AS n FROM server_events").fetchone()["n"]
    quiet = sync_once(right, config=right_config, transport=transport, sleep=lambda _seconds: None)
    assert quiet["pushed"] == 0
    after = store.execute("SELECT COUNT(*) AS n FROM server_events").fetchone()["n"]
    assert before == after
    connection = connect(left)
    try:
        delete_command(connection, command_id, output_root=left.parent / "outputs")
    finally:
        connection.close()
    left_again = sync_once(
        left, config=left_config, transport=transport, sleep=lambda _seconds: None
    )
    right_again = sync_once(
        right, config=right_config, transport=transport, sleep=lambda _seconds: None
    )
    assert left_again["status"] == "ok"
    assert right_again["status"] == "ok"
    connection = connect(right)
    try:
        assert get_command(connection, command_id) is None
    finally:
        connection.close()


def _settings(registered: dict) -> dict:
    return {
        "sync_enabled": True,
        "server_url": "http://sync.test",
        "device_token": registered["token"],
        "device_id": registered["device_id"],
        "sync_batch_size": 500,
    }


def test_cursor_advances_only_with_the_applied_batch(tmp_path: Path) -> None:
    """A bad pulled event leaves the local cursor where it was."""
    path = init_db(tmp_path / "history.sqlite")
    connection = connect(path)
    try:
        try:
            apply_remote_batch(connection, [{"kind": "nope"}], "4")
        except ValueError:
            pass
        else:
            raise AssertionError("bad event was applied")
        assert get_sync_value(connection, "pull_cursor") in {None, "0"}
    finally:
        connection.close()


def test_overlapping_sync_is_busy(tmp_path: Path) -> None:
    """A second sync worker exits while the lock is held."""
    path = init_db(tmp_path / "history.sqlite")
    save_config({"sync_enabled": True, "server_url": "http://sync.test", "device_token": "token"})
    lock = SyncLock(path.parent / "sync.lock")
    assert lock.acquire()
    try:
        assert sync_once(path)["status"] == "busy"
    finally:
        lock.release()


def _enable(database: Path, registered: dict) -> None:
    save_config(
        {
            "sync_enabled": True,
            "server_url": "http://sync.test",
            "device_token": registered["token"],
            "device_id": registered["device_id"],
        }
    )
    del database


def _event(event_id: str, device_id: str, command: str) -> dict:
    return {
        "id": event_id,
        "revision": 1,
        "kind": "command",
        "device_id": device_id,
        "payload": {"command": command, "started_at": _stamp(0), "cwd": "/work"},
    }


def _stamp(seconds: int) -> str:
    return (datetime(2026, 11, 18, 12, 0, tzinfo=UTC) + timedelta(seconds=seconds)).isoformat()


class AppTransport(httpx.BaseTransport):
    """Speak HTTP to the ASGI app without an async client."""

    def __init__(self, app) -> None:
        self.client = TestClient(app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query.decode()}"
        headers = {key: value for key, value in request.headers.items() if key.lower() != "host"}
        response = self.client.request(
            request.method, path, content=request.content, headers=headers
        )
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )
