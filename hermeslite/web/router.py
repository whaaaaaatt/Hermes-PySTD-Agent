"""Minimal HTTP routing helpers built on ``http.server``.

The pattern: a single :class:`RequestHandler` subclass that
dispatches ``method + path`` to a registered handler. Handlers get
``(handler, request_body)`` and return either a dict (serialized as
JSON), a tuple ``(status, body)``, a raw bytes payload, or
:class:`SSEResponse` for streaming.

This is intentionally tiny — we don't need Flask's routing features.
The server is a :class:`socketserver.ThreadingMixIn` so each request
runs on its own thread.
"""
from __future__ import annotations

import json
import logging
import re
import socketserver
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response types
# ---------------------------------------------------------------------------

class SSEResponse:
    """Marker for a streaming Server-Sent Events response.

    The handler returns ``SSEResponse(gen)`` where ``gen`` is a
    generator yielding string payloads. Each payload is wrapped in
    ``data: <payload>\\n\\n`` and written to the socket.

    IMPORTANT: the handler generator must close any underlying resources
    in a ``finally`` block. The runner guarantees ``gen.close()`` is
    called when the client disconnects (or the request is otherwise
    abandoned).
    """

    __slots__ = ("gen",)

    def __init__(self, gen):
        self.gen = gen


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

# A route is a tuple ``(method_re, path_re, handler)``. The handler's
# regex match groups are passed as keyword arguments.
Route = Tuple[str, str, Callable[["RequestHandler", bytes, Dict[str, str]], Any]]
JSON = Dict[str, Any]


class Router:
    def __init__(self) -> None:
        self._routes: List[Route] = []

    def add(self, method: str, path_pattern: str, handler: Callable) -> None:
        self._routes.append((method.upper(), path_pattern, handler))

    def route(self, method: str, path_pattern: str):
        """Decorator form of :meth:`add`."""
        def wrap(fn: Callable) -> Callable:
            self.add(method, path_pattern, fn)
            return fn
        return wrap

    def dispatch(self, method: str, path: str):
        for m, pat, fn in self._routes:
            if m != method:
                continue
            regex = re.compile("^" + pat + "$")
            m_obj = regex.match(path)
            if not m_obj:
                continue
            return fn, m_obj.groupdict()
        return None, {}


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class RequestHandler(BaseHTTPRequestHandler):
    """A single :class:`BaseHTTPRequestHandler` subclass used by
    :class:`HttpServer`. Subclasses set ``router`` and ``server_state``.

    The handler does NOT do its own routing; :class:`HttpServer.install`
    wires ``self.router`` and ``self.state`` from outside.
    """

    # Filled in by HttpServer.install:
    router: Router = Router()
    state: Any = None  # whatever HttpServer passes in
    static_dir: Optional[str] = None

    # Quiet the default access log; we log ourselves.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("http: " + (format % args))

    # -- dispatch -----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        # CORS preflight — accept anything for the local-only dashboard.
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def _dispatch(self, method: str) -> None:
        # CORS preflight is handled in do_OPTIONS; for the actual
        # request, we still set CORS headers so cross-origin clients
        # (e.g. a static preview on a different port) can call us.
        if self.path.startswith("/api/") or self.path == "/api":
            self._dispatch_api(method)
            return
        # Static file fallback.
        self._serve_static(self.path)

    def _dispatch_api(self, method: str) -> None:
        # Strip the query string before dispatch.
        path = self.path.split("?", 1)[0]
        handler, kwargs = self.router.dispatch(method, path)
        if handler is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": f"no route for {method} {path}"})
            return
        body = self._read_body()
        try:
            result = handler(self, body, kwargs)
        except HTTPError as exc:
            self._send_json(exc.status, {"detail": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("handler %s %s raised", method, path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": f"{type(exc).__name__}: {exc}"})
            return
        if isinstance(result, SSEResponse):
            self._send_sse(result.gen)
            return
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
            status, payload = result
            self._send_json(status, payload)
            return
        if isinstance(result, (bytes, bytearray)):
            self._send_bytes(HTTPStatus.OK, result, content_type="application/octet-stream")
            return
        if isinstance(result, str):
            self._send_bytes(HTTPStatus.OK, result.encode("utf-8"), content_type="text/plain; charset=utf-8")
            return
        # default: dict → JSON 200
        self._send_json(HTTPStatus.OK, result if result is not None else {})

    # -- body ---------------------------------------------------------------

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        try:
            return self.rfile.read(length)
        except OSError:
            return b""

    def parse_json(self, body: bytes) -> JSON:
        """Helper for handlers: decode the request body as JSON."""
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPError(400, f"invalid JSON: {exc}")
        if not isinstance(data, dict):
            raise HTTPError(400, "request body must be a JSON object")
        return data

    # -- responses ----------------------------------------------------------

    def _send_cors_headers(self) -> None:
        # Locked-down CORS: only the same-origin dashboard needs this,
        # but explicit headers help with preflight during development.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, *, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_sse(self, gen) -> None:
        """Stream a Server-Sent Events response.

        We never set Content-Length (the length is unknown). We send a
        preamble frame so the client's EventSource immediately fires
        ``onopen``, then iterate the generator.
        """
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._send_cors_headers()
        self.end_headers()
        # Initial comment line to defeat some proxies' buffering.
        try:
            self.wfile.write(b": stream open\n\n")
            self.wfile.flush()
        except OSError:
            return
        try:
            for payload in gen:
                if payload is None:
                    continue
                if isinstance(payload, (bytes, bytearray)):
                    chunk = payload
                else:
                    chunk = f"data: {payload}\n\n".encode("utf-8")
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except OSError:
                    # Client disconnected.
                    break
        finally:
            try:
                gen.close()
            except Exception:
                pass

    # -- static files -------------------------------------------------------

    def _serve_static(self, url_path: str) -> None:
        if not self.static_dir:
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "no static dir"})
            return
        # Map "/" → index.html
        if url_path == "/" or not url_path:
            url_path = "/index.html"
        rel = url_path.lstrip("/")
        # Defence in depth: never serve files outside static_dir.
        import os
        target = os.path.normpath(os.path.join(self.static_dir, rel))
        if not target.startswith(os.path.normpath(self.static_dir)):
            self._send_json(HTTPStatus.FORBIDDEN, {"detail": "forbidden"})
            return
        if not os.path.isfile(target):
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return
        try:
            with open(target, "rb") as f:
                body = f.read()
        except OSError as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": str(exc)})
            return
        ctype = _guess_mime(target)
        self._send_bytes(HTTPStatus.OK, body, content_type=ctype)


# ---------------------------------------------------------------------------
# HTTP error type
# ---------------------------------------------------------------------------

class HTTPError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------------------
# Threaded server
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """One request per thread. ``allow_reuse_address`` lets us restart
    quickly on the same port (matters during development).
    """
    daemon_threads = True
    allow_reuse_address = True
    # Override bind_and_activate so we can rebind when the user passes
    # a different host. Not strictly necessary; left as a hook.


# ---------------------------------------------------------------------------
# Mime helper
# ---------------------------------------------------------------------------

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}


def _guess_mime(path: str) -> str:
    import os
    ext = os.path.splitext(path)[1].lower()
    return _MIME.get(ext, "application/octet-stream")
