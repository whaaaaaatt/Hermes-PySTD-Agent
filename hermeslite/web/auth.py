"""Authentication for the web dashboard.

A single shared bearer token gates the API. The token is generated
when the server starts (and persisted to ``config.web.auth_token``) if
the bind host is non-loopback. Loopback binds skip the gate entirely
unless ``--insecure`` was passed without a token (in which case we
still gate — the operator is asking for trouble).

The token can be passed via:
  - ``Authorization: Bearer <token>`` header
  - ``?token=<token>`` query string (for SSE clients that can't set headers)

The token is also returned to the dashboard's index page so the
frontend can stash it in localStorage. The first load therefore *can*
succeed even if the user has never seen the token — but for non-loopback
binds, we still log the token to stdout so a copy is recoverable.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------

class Auth:
    def __init__(self, token: str = "", disabled: bool = False):
        self.token = token or ""
        # When ``disabled`` is True the operator has explicitly opted
        # out of the auth gate (via ``--insecure`` on the CLI). We
        # must short-circuit ``required()`` to False regardless of the
        # bind host — this is the whole point of the flag.
        self.disabled = bool(disabled)

    @classmethod
    def generate(cls) -> "Auth":
        """Return an :class:`Auth` with a fresh 32-byte URL-safe token."""
        return cls(token=secrets.token_urlsafe(32))

    # -- check --------------------------------------------------------------

    def is_loopback(self, host: str) -> bool:
        h = (host or "").strip().lower()
        return h in ("127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1")

    def required(self, host: str) -> bool:
        """Return True if a token must be presented for the given bind host.

        Auth is disabled when:
          1. The operator passed ``--insecure`` (``self.disabled``).
          2. We're bound to a loopback interface (the only sane default
             for a dev tool — LAN exposure is opt-in).
        """
        if self.disabled:
            return False
        if self.is_loopback(host):
            return False
        if not self.token:
            return False
        return True

    def check(self, request_headers, query_string: str, host: str) -> bool:
        """Return True iff the request is authorized.

        Public methods (loopback, no token required) always pass.
        Otherwise we check the Authorization header and the ``token``
        query parameter. ``hmac.compare_digest`` avoids timing leaks.
        ``request_headers`` may be:
          - a dict (Authorization header lookup is direct)
          - a BaseHTTPRequestHandler-like object with a ``headers`` attr
            (a ``http.client.HTTPMessage`` instance, which has ``.get``)
        """
        if not self.required(host):
            return True
        if not self.token:
            return False
        # 1. Authorization header
        auth = ""
        if isinstance(request_headers, dict):
            auth = request_headers.get("Authorization") or ""
        elif hasattr(request_headers, "headers"):
            try:
                auth = request_headers.headers.get("Authorization") or ""
            except (AttributeError, TypeError):
                auth = ""
        elif hasattr(request_headers, "get"):
            auth = request_headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            presented = auth[7:].strip()
            if hmac.compare_digest(presented.encode(), self.token.encode()):
                return True
        # 2. ?token=… query string
        for chunk in (query_string or "").split("&"):
            if not chunk:
                continue
            k, _, v = chunk.partition("=")
            if k == "token":
                if hmac.compare_digest((v or "").encode(), self.token.encode()):
                    return True
        return False
