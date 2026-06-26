"""Git, datetime, env, env-vars, web search (multi-engine), http_post, diff."""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# git_status / git_diff / git_log
# ---------------------------------------------------------------------------

def _run_git(args: List[str], cwd: str, timeout: int = 10) -> ToolResult:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return ToolResult.failure("git executable not found in PATH")
    except subprocess.TimeoutExpired:
        return ToolResult.failure(f"git timeout after {timeout}s")
    except OSError as exc:
        return ToolResult.failure(f"{type(exc).__name__}: {exc}")
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and not proc.stdout:
        return ToolResult(data=out, ok=False, error=f"exit {proc.returncode}")
    return ToolResult.success(out.rstrip())


class GitStatusTool(Tool):
    name = "git_status"
    description = "Run `git status` in `cwd` (default: current directory)."
    parameters = {
        "type": "object",
        "properties": {"cwd": {"type": "string"}, "porcelain": {"type": "boolean"}},
    }

    def run(self, cwd: str = "", porcelain: bool = False, **_) -> ToolResult:
        args = ["status"] + (["--porcelain"] if porcelain else [])
        return _run_git(args, cwd=cwd)


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Show `git diff` (working tree vs index by default)."
    parameters = {
        "type": "object",
        "properties": {
            "cwd": {"type": "string"},
            "staged": {"type": "boolean", "description": "Show staged changes (--staged). Default false."},
            "path": {"type": "string", "description": "Limit diff to this path."},
            "max_output": {"type": "integer", "description": "Cap output bytes. Default 50000."},
        },
    }

    def run(self, cwd: str = "", staged: bool = False, path: str = "", max_output: int = 50_000, **_) -> ToolResult:
        args = ["diff"]
        if staged:
            args.append("--staged")
        if path:
            args.extend(["--", path])
        result = _run_git(args, cwd=cwd)
        if result.ok and len(result.data or "") > max_output:
            data = (result.data or "")[:max_output] + "\n... [truncated]"
            return ToolResult.success(data)
        return result


class GitLogTool(Tool):
    name = "git_log"
    description = "Show recent `git log` entries (one line each)."
    parameters = {
        "type": "object",
        "properties": {
            "cwd": {"type": "string"},
            "n": {"type": "integer", "description": "Number of commits. Default 10."},
            "branch": {"type": "string", "description": "Limit to a branch. Optional."},
        },
    }

    def run(self, cwd: str = "", n: int = 10, branch: str = "", **_) -> ToolResult:
        args = ["log", f"-n{n}", "--pretty=format:%h %ad %s", "--date=short"]
        if branch:
            args.append(branch)
        return _run_git(args, cwd=cwd)


# ---------------------------------------------------------------------------
# datetime_now
# ---------------------------------------------------------------------------

class DateTimeNowTool(Tool):
    name = "datetime_now"
    description = "Return the current local date+time in ISO format, plus Unix timestamp."
    parameters = {
        "type": "object",
        "properties": {
            "utc": {"type": "boolean", "description": "Return UTC instead of local time. Default false."},
            "fmt": {"type": "string", "description": "strftime format string. Overrides ISO."},
        },
    }

    def run(self, utc: bool = False, fmt: str = "", **_) -> ToolResult:
        now = datetime.datetime.now(tz=datetime.timezone.utc) if utc else datetime.datetime.now()
        if fmt:
            return ToolResult.success(now.strftime(fmt))
        return ToolResult.success(f"{now.isoformat()}\nUnix: {int(now.timestamp())}")


# ---------------------------------------------------------------------------
# env_get / env_list
# ---------------------------------------------------------------------------

class EnvGetTool(Tool):
    name = "env_get"
    description = "Read a single environment variable. Returns empty string if unset. Sensitive values are redacted — the tool returns a placeholder; use the variable directly in your code."
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }

    _SENSITIVE_KEYWORDS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "AUTH", "CREDENTIAL", "DSN")

    def run(self, name: str, **_) -> ToolResult:
        value = os.environ.get(name, "")
        if value and len(value) > 8 and any(s in name.upper() for s in self._SENSITIVE_KEYWORDS):
            return ToolResult.success(f"[REDACTED: use ${name}]")
        return ToolResult.success(value)


class EnvListTool(Tool):
    name = "env_list"
    description = "List environment variables whose names match a prefix (default: all)."
    parameters = {
        "type": "object",
        "properties": {"prefix": {"type": "string", "description": "Filter. Default empty (all)."}, "redact": {"type": "boolean", "description": "Redact values longer than 8 chars. Default true."}},
    }

    def run(self, prefix: str = "", redact: bool = True, **_) -> ToolResult:
        keys = sorted(k for k in os.environ if k.startswith(prefix))
        out = []
        for k in keys:
            v = os.environ[k]
            if redact and len(v) > 8 and any(s in k.upper() for s in ("KEY", "SECRET", "TOKEN", "PASSWORD", "AUTH")):
                v = f"[REDACTED: use ${k}]"
            out.append(f"{k}={v}")
        return ToolResult.success("\n".join(out) if out else "(no matches)")


# ---------------------------------------------------------------------------
# web_search (multi-engine Chinese search aggregator)
# ---------------------------------------------------------------------------

import concurrent.futures
import re as _re


# --- Per-engine fetchers ------------------------------------------------

def _fetch_url(url: str, timeout: int = 12) -> str:
    """Fetch a URL and return decoded HTML. Raises on any error."""
    req = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(300_000).decode("utf-8", errors="replace")


def _search_baidu(query: str, limit: int) -> List[Dict[str, str]]:
    url = "https://www.baidu.com/s?" + urllib.parse.urlencode({"wd": query})
    html = _fetch_url(url)
    results: List[Dict[str, str]] = []
    # Each result is in <div class="result c-container ..."> with an <h3> link inside.
    for m in _re.finditer(
        r'<h3[^>]*>\s*<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>\s*</h3>',
        html, _re.DOTALL,
    ):
        href, title_html = m.group(1), m.group(2)
        title = _re.sub(r'<[^>]+>', '', title_html).strip()
        if not title:
            continue
        # Try to grab a snippet from the nearby <span class="content-right_...">
        # or any text block after the h3.
        snippet = ""
        after = html[m.end():m.end() + 2000]
        sm = _re.search(r'<span[^>]*class="[^"]*content-right[^"]*"[^>]*>(.*?)</span>', after, _re.DOTALL)
        if not sm:
            sm = _re.search(r'<div[^>]*class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</div>', after, _re.DOTALL)
        if sm:
            snippet = _re.sub(r'<[^>]+>', '', sm.group(1)).strip()
        results.append({"title": title, "snippet": snippet, "url": href})
        if len(results) >= limit:
            break
    return results


def _search_bing_cn(query: str, limit: int) -> List[Dict[str, str]]:
    url = "https://cn.bing.com/search?" + urllib.parse.urlencode({"q": query, "ensearch": "0"})
    html = _fetch_url(url)
    results: List[Dict[str, str]] = []
    for m in _re.finditer(
        r'<li class="b_algo"[^>]*>.*?<h2><a[^>]+href="([^"]*)"[^>]*>(.*?)</a></h2>'
        r'.*?<p[^>]*>(.*?)</p>',
        html, _re.DOTALL,
    ):
        href = m.group(1)
        title = _re.sub(r'<[^>]+>', '', m.group(2)).strip()
        snippet = _re.sub(r'<[^>]+>', '', m.group(3)).strip()
        if title:
            results.append({"title": title, "snippet": snippet, "url": href})
        if len(results) >= limit:
            break
    return results


def _search_360(query: str, limit: int) -> List[Dict[str, str]]:
    url = "https://www.so.com/s?" + urllib.parse.urlencode({"q": query})
    html = _fetch_url(url)
    results: List[Dict[str, str]] = []
    for m in _re.finditer(
        r'<h3[^>]*>\s*<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>\s*</h3>',
        html, _re.DOTALL,
    ):
        href = m.group(1)
        title = _re.sub(r'<[^>]+>', '', m.group(2)).strip()
        snippet = ""
        after = html[m.end():m.end() + 2000]
        sm = _re.search(r'<p[^>]*class="[^"]*res-desc[^"]*"[^>]*>(.*?)</p>', after, _re.DOTALL)
        if sm:
            snippet = _re.sub(r'<[^>]+>', '', sm.group(1)).strip()
        if title:
            results.append({"title": title, "snippet": snippet, "url": href})
        if len(results) >= limit:
            break
    return results


def _search_sogou(query: str, limit: int) -> List[Dict[str, str]]:
    url = "https://sogou.com/web?" + urllib.parse.urlencode({"query": query})
    html = _fetch_url(url)
    results: List[Dict[str, str]] = []
    for m in _re.finditer(
        r'<h3[^>]*>\s*<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>\s*</h3>',
        html, _re.DOTALL,
    ):
        href = m.group(1)
        title = _re.sub(r'<[^>]+>', '', m.group(2)).strip()
        snippet = ""
        after = html[m.end():m.end() + 2000]
        sm = _re.search(r'<p[^>]*class="[^"]*str_info[^"]*"[^>]*>(.*?)</p>', after, _re.DOTALL)
        if not sm:
            sm = _re.search(r'<div[^>]*class="[^"]*space-txt[^"]*"[^>]*>(.*?)</div>', after, _re.DOTALL)
        if sm:
            snippet = _re.sub(r'<[^>]+>', '', sm.group(1)).strip()
        if title:
            results.append({"title": title, "snippet": snippet, "url": href})
        if len(results) >= limit:
            break
    return results


def _search_wechat(query: str, limit: int) -> List[Dict[str, str]]:
    url = "https://wx.sogou.com/weixin?" + urllib.parse.urlencode({"type": "2", "query": query})
    html = _fetch_url(url)
    results: List[Dict[str, str]] = []
    for m in _re.finditer(
        r'<h3[^>]*>\s*<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>\s*</h3>',
        html, _re.DOTALL,
    ):
        href = m.group(1)
        title = _re.sub(r'<[^>]+>', '', m.group(2)).strip()
        snippet = ""
        after = html[m.end():m.end() + 2000]
        sm = _re.search(r'<p[^>]*class="[^"]*txt-info[^"]*"[^>]*>(.*?)</p>', after, _re.DOTALL)
        if not sm:
            sm = _re.search(r'<span[^>]*class="[^"]*txt[^"]*"[^>]*>(.*?)</span>', after, _re.DOTALL)
        if sm:
            snippet = _re.sub(r'<[^>]+>', '', sm.group(1)).strip()
        if title:
            results.append({"title": title, "snippet": snippet, "url": href})
        if len(results) >= limit:
            break
    return results


# Engine registry: (name, fetcher_func)
_ENGINES = [
    ("Baidu",    _search_baidu),
    ("Bing CN",  _search_bing_cn),
    ("360",      _search_360),
    ("Sogou",    _search_sogou),
    ("WeChat",   _search_wechat),
]


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Aggregate search across Baidu, Bing CN, 360, Sogou, and WeChat. "
        "Returns up to `limit` results per engine with title, snippet, and URL. "
        "Failures in one engine do not affect others. "
        "Use 'mock' as the query to return canned results (offline testing)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "description": "Results per engine. Default 5. Max 20."},
        },
        "required": ["query"],
    }

    def run(self, query: str, limit: int = 5, **_) -> ToolResult:
        if query == "mock":
            return ToolResult.success(self._mock_results(limit))
        limit = max(1, min(int(limit), 20))

        # Fetch from all engines concurrently. Each engine is isolated —
        # a timeout or parse error in one does not prevent others from
        # returning results.
        engine_results: Dict[str, Any] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(_ENGINES)) as pool:
            future_to_name = {
                pool.submit(fn, query, limit): name
                for name, fn in _ENGINES
            }
            for fut in concurrent.futures.as_completed(future_to_name):
                name = future_to_name[fut]
                try:
                    engine_results[name] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    engine_results[name] = f"[error: {type(exc).__name__}: {exc}]"

        # Format output grouped by engine.
        parts: List[str] = []
        total = 0
        for name, _ in _ENGINES:
            res = engine_results.get(name, "[not attempted]")
            if isinstance(res, str):
                # Error message.
                parts.append(f"=== {name} ===\n{res}")
                continue
            if not res:
                parts.append(f"=== {name} ===\n(no results)")
                continue
            lines: List[str] = []
            for i, r in enumerate(res, 1):
                total += 1
                snippet_line = f"\n  {r['snippet']}" if r.get("snippet") else ""
                lines.append(f"[{i}] {r['title']}{snippet_line}\n  {r['url']}")
            parts.append(f"=== {name} ({len(res)}) ===\n" + "\n".join(lines))

        if not total:
            return ToolResult.success("(no results from any engine)")
        parts.append(f"\n--- total: {total} results across {sum(1 for v in engine_results.values() if isinstance(v, list))} engines ---")
        return ToolResult.success("\n\n".join(parts))

    @staticmethod
    def _mock_results(limit: int) -> str:
        sample = [
            ("Example Domain", "Reserved for documentation examples.", "https://example.com/"),
            ("Python docs", "Official Python language documentation.", "https://docs.python.org/3/"),
            ("PEP 8", "Style Guide for Python Code.", "https://peps.python.org/pep-0008/"),
        ]
        lines = [f"[{i+1}] {t}\n  {s}\n  {u}" for i, (t, s, u) in enumerate(sample[:limit])]
        return f"=== Mock ({len(lines)}) ===\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# http_post
# ---------------------------------------------------------------------------

class HttpPostTool(Tool):
    name = "http_post"
    description = (
        "POST a JSON body to a URL. Returns (status, response body). "
        "Use this for custom APIs (the agent's `http_get` handles GETs)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "body": {"type": "string", "description": "JSON-encoded body."},
            "headers": {"type": "string", "description": "Optional JSON object of header name→value."},
            "timeout": {"type": "integer", "description": "Default 20 seconds."},
        },
        "required": ["url", "body"],
    }

    def run(self, url: str, body: str, headers: str = "", timeout: int = 20, **_) -> ToolResult:
        try:
            req_body = body.encode("utf-8")
            hdrs = {"Content-Type": "application/json", "User-Agent": "hermeslite/0.1"}
            if headers:
                extra = json.loads(headers)
                if not isinstance(extra, dict):
                    return ToolResult.failure("headers must be a JSON object")
                hdrs.update({str(k): str(v) for k, v in extra.items()})
            req = urllib.request.Request(url, data=req_body, method="POST", headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_body = resp.read(100_000)
                text = resp_body.decode("utf-8", errors="replace")
                return ToolResult.success(f"[{resp.status}]\n{text[:5000]}")
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            return ToolResult.success(f"[{e.code}] {body.decode('utf-8', errors='replace')[:2000]}")
        except (urllib.error.URLError, OSError) as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        except json.JSONDecodeError as exc:
            return ToolResult.failure(f"bad JSON in headers: {exc}")


# ---------------------------------------------------------------------------
# diff (text)
# ---------------------------------------------------------------------------

def _diff_lines(a: str, b: str) -> str:
    """A tiny unified diff (no LCS — we just emit a simple +/- listing)."""
    a_lines = a.splitlines()
    b_lines = b.splitlines()
    out: List[str] = []
    out.append("--- old")
    out.append("+++ new")
    max_len = max(len(a_lines), len(b_lines))
    for i in range(max_len):
        av = a_lines[i] if i < len(a_lines) else None
        bv = b_lines[i] if i < len(b_lines) else None
        if av == bv:
            out.append(f" {av}" if av is not None else "")
            continue
        if av is not None:
            out.append(f"-{av}")
        if bv is not None:
            out.append(f"+{bv}")
    return "\n".join(out)


class DiffTool(Tool):
    name = "diff"
    description = "Compute a simple line diff between two strings or two files."
    parameters = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "description": "First text, or a file path prefixed with 'file:'."},
            "b": {"type": "string", "description": "Second text, or a file path prefixed with 'file:'."},
        },
        "required": ["a", "b"],
    }

    def run(self, a: str, b: str, **_) -> ToolResult:
        def resolve(s: str) -> str:
            if s.startswith("file:"):
                return Path(s[5:]).read_text(encoding="utf-8", errors="replace")
            return s
        try:
            return ToolResult.success(_diff_lines(resolve(a), resolve(b)))
        except OSError as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# hash (file)
# ---------------------------------------------------------------------------

class HashFileTool(Tool):
    name = "hash_file"
    description = "Compute the SHA-256 (or MD5 / SHA-1) hex digest of a file."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "algorithm": {"type": "string", "enum": ["md5", "sha1", "sha256"], "description": "Default sha256."},
        },
        "required": ["path"],
    }

    def run(self, path: str, algorithm: str = "sha256", **_) -> ToolResult:
        algo = algorithm.lower()
        if algo not in ("md5", "sha1", "sha256"):
            return ToolResult.failure(f"unsupported algorithm: {algorithm}")
        try:
            h = hashlib.new(algo)
            with open(Path(path).expanduser(), "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
            return ToolResult.success(h.hexdigest())
        except OSError as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# image_gen (zero-dependency placeholder)
# ---------------------------------------------------------------------------

class ImageGenTool(Tool):
    name = "image_gen"
    description = (
        "Generate an image from a text prompt. NOTE: the bundled default "
        "backend is a *placeholder* that writes a small SVG with the "
        "prompt text. Configure a real backend (DALL·E / SD / etc.) via "
        "the `image_gen` section in config.json to enable actual images."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "output_path": {"type": "string", "description": "Where to write the result. Default ./generated.svg."},
            "width": {"type": "integer", "description": "Default 512."},
            "height": {"type": "integer", "description": "Default 512."},
        },
        "required": ["prompt"],
    }

    def run(self, prompt: str, output_path: str = "generated.svg", width: int = 512, height: int = 512, **_) -> ToolResult:
        # XML-escape the prompt.
        safe = prompt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        svg = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
            f'  <defs>\n'
            f'    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">\n'
            f'      <stop offset="0%" stop-color="#fef3c7"/>\n'
            f'      <stop offset="100%" stop-color="#fde68a"/>\n'
            f'    </linearGradient>\n'
            f'  </defs>\n'
            f'  <rect width="100%" height="100%" fill="url(#g)"/>\n'
            f'  <text x="50%" y="50%" font-family="serif" font-size="24" text-anchor="middle" fill="#92400e">\n'
            f'    <tspan x="50%" dy="-1em">HermesLite image placeholder</tspan>\n'
            f'    <tspan x="50%" dy="1.4em">{safe[:100]}</tspan>\n'
            f'  </text>\n'
            f'</svg>\n'
        )
        try:
            Path(output_path).expanduser().write_text(svg, encoding="utf-8")
        except OSError as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        return ToolResult.success(f"wrote placeholder SVG to {output_path}")


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

registry.register(GitStatusTool())
registry.register(GitDiffTool())
registry.register(GitLogTool())
registry.register(DateTimeNowTool())
registry.register(EnvGetTool())
registry.register(EnvListTool())
registry.register(WebSearchTool())
registry.register(HttpPostTool())
registry.register(DiffTool())
registry.register(HashFileTool())
registry.register(ImageGenTool())
