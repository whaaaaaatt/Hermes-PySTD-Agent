"""HTTP fetch tools: ``http_get`` (URL text/JSON) and ``web_extract``
(strip HTML to readable text). Uses stdlib ``urllib`` + ``html.parser``.

We deliberately do NOT include a web_search tool — there is no
zero-dependency search API in the standard library. Users who want search
should add a custom tool that calls their preferred provider.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from ..http_client import HTTPError, get_json
from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)


def _resolve_env_vars(value: str) -> str:
    """Resolve ``${VAR}`` and ``$VAR`` references in a string.

    Looks up each variable in ``os.environ``.  If the variable is not
    set, the reference is left as-is so the caller sees what went wrong.
    """
    # Match ${VAR} or $VAR (word characters only).
    def _replace(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return os.environ.get(name, m.group(0))
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", _replace, value)


class _TextExtractor(HTMLParser):
    """Tiny HTML-to-text converter.

    Strategy: emit text inside most block-level elements, with blank
    lines for headings / paragraphs / list items. Strip <script> and
    <style> entirely. Links get ``[text](url)`` formatting so the model
    can see what they point at without losing the URL.
    """

    SKIP_TAGS = {"script", "style", "noscript", "iframe", "svg"}
    BLOCK_TAGS = {
        "p", "div", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "ul", "ol", "tr", "table",
        "br", "hr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: List[str] = []
        self._skip_depth = 0
        self._link_href: Optional[str] = None
        self._link_text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List) -> None:
        a = dict(attrs)
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in self.BLOCK_TAGS:
            self._chunks.append("\n")
        if tag == "a":
            self._link_href = a.get("href")
            self._link_text = []
        if tag in ("br",):
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in self.BLOCK_TAGS:
            self._chunks.append("\n")
        if tag == "a" and self._link_href is not None:
            text = "".join(self._link_text).strip()
            if text:
                self._chunks.append(f" [{text}]({self._link_href})")
            self._link_href = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._link_href is not None:
            self._link_text.append(data)
        else:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse runs of blank lines.
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _strip_html(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    return p.text()


class HttpGetTool(Tool):
    name = "http_get"
    description = (
        "Fetch a URL and return its body. JSON responses are decoded; "
        "HTML is stripped to readable text. `max_bytes` caps the response "
        "at 200_000 by default. Supports custom headers and timeout."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_bytes": {"type": "integer", "description": "Cap on response size. Default 200000."},
            "headers": {"type": "string", "description": "Optional JSON object of header name to value."},
            "timeout": {"type": "integer", "description": "Timeout in seconds. Default 20."},
        },
        "required": ["url"],
    }

    def run(
        self,
        url: str,
        max_bytes: int = 200_000,
        headers: str = "",
        timeout: int = 20,
        **_: Any,
    ) -> ToolResult:
        try:
            hdrs = {"User-Agent": "hermeslite/0.1"}
            if headers:
                extra = json.loads(headers)
                if not isinstance(extra, dict):
                    return ToolResult.failure("headers must be a JSON object")
                # Resolve ${VAR} and $VAR references in header values.
                hdrs.update({str(k): _resolve_env_vars(str(v)) for k, v in extra.items()})
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ctype = resp.headers.get_content_type()
                body = resp.read(max_bytes)
            text = body.decode("utf-8", errors="replace")
            if ctype == "application/json" or url.endswith(".json"):
                try:
                    parsed = json.loads(text)
                    return ToolResult.success(json.dumps(parsed, indent=2, ensure_ascii=False)[:max_bytes])
                except json.JSONDecodeError:
                    return ToolResult.success(text)
            if "html" in ctype:
                return ToolResult.success(_strip_html(text))
            return ToolResult.success(text)
        except HTTPError as exc:
            return ToolResult.failure(str(exc))
        except (urllib.error.URLError, OSError) as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        except json.JSONDecodeError as exc:
            return ToolResult.failure(f"bad JSON in headers: {exc}")


class WebExtractTool(Tool):
    """Alias of http_get with an explicit name for prompting clarity."""

    name = "web_extract"
    description = (
        "Fetch a URL and return its content as readable text. Always "
        "strips HTML — use this for articles, blog posts, and documentation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_bytes": {"type": "integer", "description": "Cap on response size. Default 200000."},
            "headers": {"type": "string", "description": "Optional JSON object of header name to value."},
            "timeout": {"type": "integer", "description": "Timeout in seconds. Default 20."},
        },
        "required": ["url"],
    }

    def run(
        self,
        url: str,
        max_bytes: int = 200_000,
        headers: str = "",
        timeout: int = 20,
        **_: Any,
    ) -> ToolResult:
        try:
            hdrs = {"User-Agent": "hermeslite/0.1"}
            if headers:
                extra = json.loads(headers)
                if not isinstance(extra, dict):
                    return ToolResult.failure("headers must be a JSON object")
                # Resolve ${VAR} and $VAR references in header values.
                hdrs.update({str(k): _resolve_env_vars(str(v)) for k, v in extra.items()})
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(max_bytes)
            text = body.decode("utf-8", errors="replace")
            return ToolResult.success(_strip_html(text) if "<" in text else text)
        except HTTPError as exc:
            return ToolResult.failure(str(exc))
        except (urllib.error.URLError, OSError) as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        except json.JSONDecodeError as exc:
            return ToolResult.failure(f"bad JSON in headers: {exc}")


registry.register(HttpGetTool())
registry.register(WebExtractTool())
