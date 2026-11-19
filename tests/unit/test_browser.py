"""Tests for the loopback history browser."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zh.browser import bind_host, create_server
from zh.database import connect, init_db, record_command

ROOT = Path(__file__).resolve().parents[2]
STARTED = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def at(seconds: int) -> str:
    """Return an ISO timestamp offset from a fixed start."""
    return (STARTED + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def browser(tmp_path: Path):
    """Serve a journal that includes a script-like command string."""
    path = init_db(tmp_path / "history.sqlite")
    connection = connect(path)
    record_command(
        connection,
        command="<script>alert(1)</script>",
        started_at=at(0),
        ended_at=at(1),
        exit_code=1,
        cwd="/work",
        hostname="origin-host",
        username="origin-user",
    )
    record_command(
        connection,
        command="git status",
        started_at=at(2),
        ended_at=at(3),
        exit_code=0,
        cwd="/work",
    )
    connection.close()
    server = create_server(path, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, path
    finally:
        server.shutdown()
        server.server_close()


def test_bind_host_is_loopback_only() -> None:
    """Public addresses are rejected before a socket is opened."""
    assert bind_host("localhost") == "127.0.0.1"
    assert bind_host("127.0.0.1") == "127.0.0.1"
    assert bind_host("::1") == "::1"
    with pytest.raises(ValueError, match="loopback"):
        bind_host("0.0.0.0")


def test_page_is_static_and_locked_down(browser) -> None:
    """The page, stylesheet, and script load from this host with a strict policy."""
    base, _path = browser
    status, headers, body = request(f"{base}/")
    page = body.decode("utf-8")
    assert status == 200
    assert "Choosing a command does not run it." in page
    assert "Last hour" in page
    assert "Last 24 hours" in page
    assert "Last day" in page
    assert 'id="view-graph"' in page
    assert "<footer" in page
    assert "innerHTML" not in page
    assert "unsafe-inline" not in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert 'src="/static/icon.svg"' in page
    assert 'alt="ZH"' in page
    assert request(f"{base}/static/browse.css")[0] == 200
    icon_status, icon_headers, icon = request(f"{base}/static/icon.svg")
    assert icon_status == 200
    assert icon_headers["Content-Type"].startswith("image/svg+xml")
    assert b"zh-icon-title" in icon
    script_status, _script_headers, script = request(f"{base}/static/browse.js")
    assert script_status == 200
    source = script.decode("utf-8")
    assert "innerHTML" not in source
    assert "eval(" not in source
    assert "document.write" not in source
    assert request(f"{base}/static/../database.py")[0] == 404


def test_api_searches_without_executing(browser) -> None:
    """Search and detail return stored text, and writes are refused."""
    base, _path = browser
    status, _headers, body = request(f"{base}/api/commands?q=%3Cscript%3E&status=failed")
    payload = json.loads(body)
    assert status == 200
    assert payload["total"] == 1
    assert payload["items"][0]["command"] == "<script>alert(1)</script>"
    command_id = payload["items"][0]["id"]
    detail_status, detail_headers, detail = request(f"{base}/api/commands/{command_id}")
    assert detail_status == 200
    assert detail_headers["Content-Type"].startswith("application/json")
    assert json.loads(detail)["cwd"] == "/work"
    assert json.loads(detail)["hostname"] == "origin-host"
    assert json.loads(detail)["username"] == "origin-user"
    assert payload["items"][0]["hostname"] == "origin-host"
    assert payload["items"][0]["username"] == "origin-user"
    assert "Host" in request(f"{base}/static/browse.js")[2].decode("utf-8")
    assert request(f"{base}/api/commands/{command_id}", method="POST")[0] == 405
    stats_status, _stats_headers, stats = request(f"{base}/api/stats")
    assert stats_status == 200
    assert json.loads(stats)["failed"] == 1
    since = urllib.parse.quote(at(2))
    ranged_status, _ranged_headers, ranged = request(f"{base}/api/commands?since={since}")
    ranged_body = json.loads(ranged)
    assert ranged_status == 200
    assert ranged_body["total"] == 1
    assert ranged_body["items"][0]["command"] == "git status"
    activity_status, _activity_headers, activity = request(f"{base}/api/activity?since={since}")
    assert activity_status == 200
    assert sum(point["succeeded"] for point in json.loads(activity)["points"]) == 1
    assert request(f"{base}/api/commands?since=yesterday")[0] == 400
    until = urllib.parse.quote(at(0))
    assert request(f"{base}/api/commands?since={since}&until={until}")[0] == 400


def test_api_rejects_bad_queries_and_missing_rows(browser) -> None:
    """Bad filters, paging, and ids fail without reading as a successful search."""
    base, _path = browser
    assert request(f"{base}/api/commands?status=nope")[0] == 400
    assert request(f"{base}/api/commands?limit=101")[0] == 400
    assert request(f"{base}/api/commands?offset=-1")[0] == 400
    assert request(f"{base}/api/commands/not-a-uuid")[0] == 400
    missing = "00000000-0000-0000-0000-000000000000"
    assert request(f"{base}/api/commands/{missing}")[0] == 404
    assert request(f"{base}/api/missing")[0] == 404
    long_query = "a" * 5000
    assert request(f"{base}/api/commands?q={long_query}")[0] == 414


def test_corrupt_journal_returns_an_error(browser) -> None:
    """A replaced journal file becomes a read error, not an empty success."""
    base, path = browser
    path.write_bytes(b"this is not a sqlite database")
    status, _headers, body = request(f"{base}/api/stats")
    assert status == 500
    assert json.loads(body)["error"] == "Could not read the journal."


def test_ipv6_loopback_serves_the_page(tmp_path: Path) -> None:
    """The browser can bind IPv6 loopback when the host allows it."""
    path = init_db(tmp_path / "history.sqlite")
    try:
        server = create_server(path, "::1", 0)
    except OSError as exc:
        pytest.skip(f"IPv6 loopback is unavailable: {exc}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, _headers, body = request(f"http://[::1]:{port}/")
        assert status == 200
        assert b"ZH history" in body or b"History" in body
    finally:
        server.shutdown()
        server.server_close()


def request(url: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    """Perform one HTTP request and return status, headers, and body."""
    call = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(call, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
