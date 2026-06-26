"""HTTP client built on stdlib ``urllib.request`` + ``http.client``.

We avoid the ``openai`` SDK on purpose. Two capabilities matter:

1. **Plain JSON POST** for non-streaming chat completions
2. **SSE streaming** for streaming chat completions (parsed line-by-line)

This module also provides a minimal retry helper for transient network
errors — three attempts, exponential backoff with jitter. Anything fancier
(``tenacity``-like state machines) is overkill for a CLI.
"""
from __future__ import annotations

import json
import logging
import random
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class HTTPError(Exception):
    """A non-2xx HTTP response. The body and status are preserved."""

    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {body[:200]}")
        self.status = status
        self.body = body
        self.url = url


class TransientError(Exception):
    """Network/IO glitch that retrying might fix."""


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

@dataclass
class RequestOptions:
    method: str = "POST"
    headers: Dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0
    # If set, read this many bytes then close. -1 = unlimited.
    max_response_bytes: int = -1


def _build_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    *,
    timeout: float = 60.0,
) -> Tuple[int, Dict[str, str], bytes]:
    """POST ``payload`` (JSON-encoded) and return ``(status, headers, body)``.

    ``headers`` defaults to ``Content-Type: application/json`` plus whatever
    caller-provided headers. The body is returned as raw bytes — callers
    that want text should decode with the response charset (usually utf-8).
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    final_headers = {"Content-Type": "application/json"}
    if headers:
        final_headers.update(headers)
    req = urllib.request.Request(url, data=data, method="POST", headers=final_headers)
    return _send_with_retry(req, timeout=timeout)


def post_json_stream(
    url: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    *,
    timeout: float = 60.0,
    max_attempts: int = 3,
) -> Tuple[int, Dict[str, str], "StreamBody"]:
    """POST ``payload`` (JSON) and return a streaming body iterator.

    Retries on transient HTTP errors (429, 500, 502, 503, 504, 408, 425)
    and network errors before the stream starts.  Once the stream is open
    and bytes are flowing, no retry happens.
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    final_headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if headers:
        final_headers.update(headers)
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        req = urllib.request.Request(url, data=data, method="POST", headers=final_headers)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return (resp.status, dict(resp.headers), StreamBody(resp))
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            text = body.decode("utf-8", errors="replace")
            if e.code in _TRANSIENT_HTTP_CODES and attempt + 1 < max_attempts:
                last_exc = HTTPError(e.code, text, url)
                logger.debug("post_json_stream: %s on attempt %d, retrying", e.code, attempt + 1)
                _sleep_backoff(attempt)
                continue
            raise HTTPError(e.code, text, url) from e
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            last_exc = e
            if attempt + 1 >= max_attempts:
                break
            logger.debug("post_json_stream: %s on attempt %d, retrying", type(e).__name__, attempt + 1)
            _sleep_backoff(attempt)
            continue
    raise TransientError(f"streaming request failed after {max_attempts} attempts: {last_exc}")


class StreamBody:
    """A streaming HTTP body. Wraps a ``http.client.HTTPResponse`` and
    yields raw bytes. Closes the underlying response on ``close()``.

    We do NOT use a generator with ``try/finally`` here because
    :class:`urllib.response.addinfourl` lacks a clean ``read1`` boundary in
    Python 3.8. The ``read1``-style read loop is enough for SSE, which is
    line-delimited.
    """

    def __init__(self, resp):
        self._resp = resp
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        resp = self._resp
        # Some test mocks only implement __iter__ (yielding chunks).
        # Detect that and delegate; otherwise use chunked reads.
        if not hasattr(resp, "read") and hasattr(resp, "__iter__"):
            try:
                for chunk in resp:
                    if chunk:
                        yield chunk
            finally:
                self.close()
            return
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    return
                yield chunk
        finally:
            self.close()

    def read_chunk(self) -> bytes:
        """Read a single chunk (or raise if the stream is closed)."""
        if self._closed:
            return b""
        return self._resp.read(4096)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._resp.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

_TRANSIENT_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}


def _send_with_retry(
    req: urllib.request.Request,
    *,
    timeout: float,
    max_attempts: int = 3,
) -> Tuple[int, Dict[str, str], bytes]:
    """Issue a request, retrying transient failures with backoff.

    Retries: connection errors, socket timeouts, 5xx, 408, 425, 429.
    Does NOT retry 4xx (other than 408/425/429) — those are caller errors.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return (
                resp.status,
                dict(resp.headers),
                resp.read(),
            )
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            text = body.decode("utf-8", errors="replace")
            if e.code in _TRANSIENT_HTTP_CODES and attempt + 1 < max_attempts:
                last_exc = HTTPError(e.code, text, req.full_url)
                _sleep_backoff(attempt)
                continue
            raise HTTPError(e.code, text, req.full_url) from e
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            last_exc = e
            if attempt + 1 >= max_attempts:
                break
            _sleep_backoff(attempt)
            continue
    raise TransientError(f"request failed after {max_attempts} attempts: {last_exc}")


def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff with jitter: ~0.5s, 1s, 2s + up to 250ms."""
    base = 0.5 * (2 ** attempt)
    time.sleep(base + random.uniform(0, 0.25))


# ---------------------------------------------------------------------------
# SSE parser
# ---------------------------------------------------------------------------

def parse_sse(stream: StreamBody) -> Iterator[Dict[str, str]]:
    """Parse Server-Sent Events from a streaming body.

    Yields one dict per event with the keys ``event`` (default ``"message"``)
    and ``data`` (concatenated data lines joined by ``\\n``). Comments
    (``# ...``) and ``id:``/``retry:`` fields are ignored — we don't
    resume SSE streams.

    The format follows WHATWG HTML §9.2 (the EventSource spec): events
    are separated by blank lines, each field is ``name: value``, the
    value is everything after the first colon (with one leading space
    stripped if present).
    """
    event: str = "message"
    data_lines: List[str] = []

    # We decode chunks incrementally. The boundary is LF (\\n) — \\r\\n is
    # normalised to \\n. A blank line (\\n\\n or \\r\\n\\r\\n) dispatches
    # the current event.
    pending: bytes = b""
    for chunk in stream:
        pending += chunk
        while True:
            nl = pending.find(b"\n")
            if nl < 0:
                break
            raw_line = pending[:nl]
            pending = pending[nl + 1 :]
            # Strip optional \r
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            line = raw_line.decode("utf-8", errors="replace")
            if line == "":
                # Dispatch
                if data_lines or event != "message":
                    yield {"event": event, "data": "\n".join(data_lines)}
                event = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                # Comment — ignore per spec.
                continue
            if ":" in line:
                name, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
            else:
                name, value = line, ""
            if name == "event":
                event = value
            elif name == "data":
                data_lines.append(value)
            # else: id, retry — ignored.
    # Flush trailing event if the server didn't terminate with a blank line.
    if data_lines or event != "message":
        yield {"event": event, "data": "\n".join(data_lines)}


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def get_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    *,
    timeout: float = 8.0,
) -> Dict[str, Any]:
    """Convenience GET + JSON decode. Used for ``/models`` catalog fetch."""
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        raise HTTPError(e.code, body.decode("utf-8", errors="replace"), url) from e
