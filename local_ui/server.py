"""Loopback-only HTTP server for the local workflow page."""
from __future__ import annotations

import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .alerts import AlertTable
from .operations import OperationBusyError, OperationManager
from .project import BoundProject, ConfigurationChangedError


MAX_REQUEST_BYTES = 1024 * 1024
STATIC_ROOT = Path(__file__).with_name("static")


class LocalUIApp:
    def __init__(self, config_path: str | Path, *, services=None):
        self.project = BoundProject(config_path)
        self.alerts = AlertTable(self.project)
        self.operations = OperationManager(self.project, services=services)


class LocalUIServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: LocalUIApp):
        self.app = app
        super().__init__(address, LocalUIRequestHandler)


class LocalUIRequestHandler(BaseHTTPRequestHandler):
    server: LocalUIServer

    def log_message(self, format: str, *args: Any) -> None:
        status = str(args[1]) if len(args) > 1 else ""
        if status.startswith(("4", "5")):
            print(f"[local-ui] {self.address_string()} - {format % args}", file=sys.stderr)

    def _send_bytes(
        self,
        status: int,
        content: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self'; "
            "style-src 'self'; script-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(content)

    def _json(self, status: int, value: Any) -> None:
        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            payload = json.dumps({"error": f"response is not JSON serializable: {error}"}).encode()
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._send_bytes(status, payload, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _static(self, name: str, content_type: str) -> None:
        path = STATIC_ROOT / name
        try:
            content = path.read_bytes()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "resource not found")
            return
        self._send_bytes(HTTPStatus.OK, content, content_type)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/":
                self._static("index.html", "text/html; charset=utf-8")
                return
            if parsed.path == "/app.js":
                self._static("app.js", "text/javascript; charset=utf-8")
                return
            if parsed.path == "/style.css":
                self._static("style.css", "text/css; charset=utf-8")
                return
            if parsed.path == "/api/project":
                self._json(HTTPStatus.OK, self.server.app.project.to_dict())
                return
            if parsed.path == "/api/alerts":
                query = parse_qs(parsed.query)
                warning_type = query.get("type", ["all"])[0]
                order = query.get("order", ["desc"])[0]
                self._json(
                    HTTPStatus.OK,
                    self.server.app.alerts.load(warning_type, order),
                )
                return
            if parsed.path == "/api/operations/current":
                self._json(HTTPStatus.OK, self.server.app.operations.current())
                return
            self._error(HTTPStatus.NOT_FOUND, "resource not found")
        except ConfigurationChangedError as error:
            self._error(HTTPStatus.CONFLICT, str(error))
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def _read_json(self) -> Any:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as error:
            raise ValueError("invalid Content-Length") from error
        if length < 1:
            raise ValueError("request body must not be empty")
        if length > MAX_REQUEST_BYTES:
            raise ValueError("request body is too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid JSON request: {error}") from error

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        prefix = "/api/operations/"
        if not parsed.path.startswith(prefix):
            self._error(HTTPStatus.NOT_FOUND, "resource not found")
            return
        kind = parsed.path[len(prefix):]
        try:
            payload = self._read_json()
            state = self.server.app.operations.submit(kind, payload)
            self._json(HTTPStatus.ACCEPTED, state)
        except OperationBusyError as error:
            self._error(HTTPStatus.CONFLICT, str(error))
        except ConfigurationChangedError as error:
            self._error(HTTPStatus.CONFLICT, str(error))
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))


def make_server(
    config_path: str | Path,
    port: int = 8765,
    *,
    services=None,
) -> LocalUIServer:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return LocalUIServer(("127.0.0.1", port), LocalUIApp(config_path, services=services))


def serve(config_path: str | Path, port: int = 8765) -> None:
    server = make_server(config_path, port)
    actual_port = int(server.server_address[1])
    print(f"Local workflow UI: http://127.0.0.1:{actual_port}")
    print(
        "Remote access: "
        f"ssh -L {actual_port}:127.0.0.1:{actual_port} USER@HOST",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local workflow UI…")
    finally:
        server.app.operations.shutdown()
        server.server_close()
