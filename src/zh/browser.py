"""Loopback HTTP browser for the local command journal."""

from __future__ import annotations

import json
import re
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from zh.database import connect, get_command, init_db
from zh.search import command_activity, command_stats, search_commands


class BadRequest(ValueError):
    """The browser request cannot be served."""

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_CSP = (
    "default-src 'none'; style-src 'self'; script-src 'self'; "
    "img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
_STATIC = {
    "/": ("browse.html", "text/html; charset=utf-8"),
    "/static/browse.css": ("browse.css", "text/css; charset=utf-8"),
    "/static/browse.js": ("browse.js", "text/javascript; charset=utf-8"),
    "/static/icon.svg": ("icon.svg", "image/svg+xml"),
}
_MAX_PATH = 4096


def bind_host(host: str) -> str:
    """Return a loopback address. `localhost` binds as IPv4 loopback."""
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("browser host must be a loopback address")
    if host == "localhost":
        return "127.0.0.1"
    return host


def create_server(db_path: Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Bind the browser. The database is created when it does not exist."""
    if port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")
    bound = bind_host(host)
    journal = init_db(db_path)
    handler = make_handler(journal)
    server_type = ThreadingHTTPServer
    if bound == "::1":
        server_type = _IPv6Server
    server = server_type((bound, port), handler)
    server.daemon_threads = True
    return server


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the browser until interrupted and print its loopback URL."""
    server = create_server(db_path, host, port)
    bound_host, bound_port = server.server_address[:2]
    display_host = f"[{bound_host}]" if ":" in bound_host else bound_host
    print(f"http://{display_host}:{bound_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return
    finally:
        server.server_close()


def make_handler(db_path: Path) -> type[BaseHTTPRequestHandler]:
    """Build a request handler closed over the journal path."""
    journal = db_path

    class Handler(BaseHTTPRequestHandler):
        """Serve the static browser and read-only journal JSON."""

        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            """Route a read request."""
            self._dispatch()

        def do_POST(self) -> None:
            """Reject writes. The browser does not record or run commands."""
            self._reject_method()

        def do_PUT(self) -> None:
            """Reject writes."""
            self._reject_method()

        def do_PATCH(self) -> None:
            """Reject writes."""
            self._reject_method()

        def do_DELETE(self) -> None:
            """Reject writes."""
            self._reject_method()

        def log_message(self, format: str, *args: object) -> None:
            """Skip request logs so search text is not written to the terminal."""
            del format, args

        def _reject_method(self) -> None:
            self._json(405, {"error": "Method not allowed."})

        def _dispatch(self) -> None:
            if len(self.path) > _MAX_PATH:
                self._json(414, {"error": "Request too long."})
                return
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path != "/" and path.endswith("/"):
                path = path[:-1] or "/"
            if path in _STATIC:
                name, content_type = _STATIC[path]
                self._static(name, content_type)
                return
            try:
                if path == "/api/stats":
                    self._stats(parsed.query)
                    return
                if path == "/api/activity":
                    self._activity(parsed.query)
                    return
                if path == "/api/commands":
                    self._commands(parsed.query)
                    return
                prefix = "/api/commands/"
                if path.startswith(prefix):
                    self._command(path[len(prefix) :])
                    return
            except BadRequest as exc:
                self._json(400, {"error": str(exc)})
                return
            except Exception:
                sys.stderr.write("zh browse failed to read the journal\n")
                self._json(500, {"error": "Could not read the journal."})
                return
            self._json(404, {"error": "Not found."})

        def _stats(self, query: str) -> None:
            params = parse_qs(query, keep_blank_values=True)
            with _session(journal) as connection:
                try:
                    body = command_stats(connection, **_search_kwargs(params))
                except ValueError as exc:
                    raise BadRequest(str(exc)) from exc
            self._json(200, body)

        def _activity(self, query: str) -> None:
            params = parse_qs(query, keep_blank_values=True)
            with _session(journal) as connection:
                try:
                    body = command_activity(connection, **_search_kwargs(params))
                except ValueError as exc:
                    raise BadRequest(str(exc)) from exc
            self._json(200, body)

        def _commands(self, query: str) -> None:
            params = parse_qs(query, keep_blank_values=True)
            limit = _int_arg(params, "limit", 50, 1, 100)
            offset = _int_arg(params, "offset", 0, 0, 1_000_000)
            with _session(journal) as connection:
                try:
                    body = search_commands(
                        connection,
                        limit=limit,
                        offset=offset,
                        **_search_kwargs(params),
                    )
                except ValueError as exc:
                    raise BadRequest(str(exc)) from exc
            self._json(200, body)

        def _command(self, command_id: str) -> None:
            if not _UUID.fullmatch(command_id):
                raise BadRequest("command id must be a UUID")
            with _session(journal) as connection:
                body = get_command(connection, command_id)
            if body is None:
                self._json(404, {"error": "Not found."})
                return
            self._json(200, body)

        def _static(self, name: str, content_type: str) -> None:
            try:
                payload = files("zh").joinpath("static").joinpath(name).read_bytes()
            except OSError:
                self._json(500, {"error": "Browser assets are missing."})
                return
            self._bytes(200, payload, content_type)

        def _json(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self._bytes(status, payload, "application/json; charset=utf-8")

        def _bytes(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            self.wfile.write(payload)

    return Handler


class _IPv6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    allow_reuse_address = True


class _Session:
    """Close a journal connection after a read."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = None

    def __enter__(self):
        init_db(self.path)
        self.connection = connect(self.path)
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        if self.connection is not None:
            self.connection.close()


def _session(path: Path) -> _Session:
    return _Session(path)


def _search_kwargs(params: dict) -> dict[str, str | None]:
    """Read the shared search filters from a query string."""
    status = params.get("status", ["all"])[0] or "all"
    text = params.get("q", [""])[0]
    return {
        "query": text,
        "status": status,
        "since": _optional_text(params, "since"),
        "until": _optional_text(params, "until"),
    }


def _optional_text(params: dict, name: str) -> str | None:
    raw = params.get(name, [None])[0]
    if raw is None or raw == "":
        return None
    return raw


def _int_arg(params: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    raw = params.get(name, [None])[0]
    if raw is None or raw == "":
        return default
    if not re.fullmatch(r"-?\d+", raw):
        raise BadRequest(f"{name} must be an integer")
    number = int(raw)
    if number < minimum or number > maximum:
        raise BadRequest(f"{name} must be from {minimum} to {maximum}")
    return number
