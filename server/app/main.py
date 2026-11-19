"""Versioned sync API."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from server.app.store import MAX_BATCH, Store, open_postgres, open_sqlite

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class RegisterBody(BaseModel):
    """Device registration request."""

    name: str = Field(min_length=1, max_length=200)
    hostname: str | None = Field(default=None, max_length=200)


class EventIn(BaseModel):
    """One command revision or tombstone."""

    id: str
    revision: int
    kind: str
    device_id: str
    payload: dict


class PushBody(BaseModel):
    """A bounded batch of events."""

    events: list[EventIn]


def create_app(store: Store) -> FastAPI:
    """Build the sync API around a store."""
    app = FastAPI(title="ZH sync", version="1")
    app.state.store = store

    @app.get("/v1/health")
    def health() -> dict:
        """Report that the process is up. This route does not check a device token."""
        return {"status": "ok"}

    @app.post("/v1/devices/register", status_code=201)
    def register(body: RegisterBody, authorization: str | None = Header(default=None)) -> dict:
        """Register a device with the bootstrap token and return its credential once."""
        token = _bearer(authorization)
        try:
            return store.register(token, body.name, body.hostname)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/devices")
    def devices(authorization: str | None = Header(default=None)) -> list[dict]:
        """List devices in the caller's account."""
        device = _device(store, authorization)
        return store.list_devices(device["account_id"])

    @app.delete("/v1/devices/{device_id}", status_code=204)
    def delete_device(device_id: str, authorization: str | None = Header(default=None)) -> None:
        """Revoke a device in the caller's account."""
        device = _device(store, authorization)
        if not store.revoke(device["account_id"], device_id):
            raise HTTPException(status_code=404, detail="device not found")
        return Response(status_code=204)

    @app.post("/v1/events/push")
    def push(body: PushBody, authorization: str | None = Header(default=None)) -> dict:
        """Accept a batch. Only valid events are acknowledged."""
        device = _device(store, authorization)
        if len(body.events) > MAX_BATCH:
            raise HTTPException(status_code=400, detail=f"batch is larger than {MAX_BATCH}")
        events = [item.model_dump() for item in body.events]
        try:
            acked, rejected = store.push(device["account_id"], device["id"], events)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"acked": acked, "rejected": rejected}

    @app.get("/v1/events/pull")
    def pull(
        cursor: str = "0",
        limit: int = MAX_BATCH,
        authorization: str | None = Header(default=None),
    ) -> dict:
        """Return committed events after a commit-safe cursor."""
        device = _device(store, authorization)
        if not cursor.isdigit():
            raise HTTPException(status_code=400, detail="cursor must be a non-negative integer")
        try:
            return store.pull(device["account_id"], int(cursor), limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


def assert_bind(host: str) -> None:
    """Refuse plain HTTP on a public interface unless a TLS proxy is declared."""
    if host in LOOPBACK_HOSTS:
        return
    if os.environ.get("ZH_TLS_PROXY") == "1":
        return
    raise SystemExit(
        "refusing plain HTTP on a non-loopback host; terminate TLS in front and set ZH_TLS_PROXY=1"
    )


def main() -> None:
    """Run the sync API."""
    host = os.environ.get("ZH_SERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("ZH_SERVER_PORT", "8080"))
    assert_bind(host)
    database_url = os.environ.get("DATABASE_URL", "")
    sqlite_path = os.environ.get("ZH_SERVER_DB", "")
    if database_url.startswith("postgres"):
        store = open_postgres(database_url)
    elif sqlite_path:
        store = open_sqlite(Path(sqlite_path))
    else:
        raise SystemExit("Set ZH_SERVER_DB or DATABASE_URL")
    import uvicorn

    uvicorn.run(create_app(store), host=host, port=port, log_level="info")


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="authentication required")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    return token


def _device(store: Store, authorization: str | None) -> dict:
    device = store.device_for_token(_bearer(authorization))
    if device is None or device["revoked_at"]:
        raise HTTPException(status_code=401, detail="authentication required")
    return device
