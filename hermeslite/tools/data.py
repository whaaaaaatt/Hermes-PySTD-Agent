"""Data / format conversion tools.

These are pure-stdlib alternatives to ``jq``, ``yq``, ``xmllint`` etc.
- ``json_query``     — extract a dotted path from a JSON value
- ``json_format``    — pretty-print / minify JSON
- ``csv_to_json``    — convert a CSV file/string to JSON
- ``json_to_csv``    — flatten a list of objects to CSV
- ``yaml_to_json``   — best-effort simple YAML→JSON (no deps, restricted subset)
- ``json_to_yaml``   — JSON→YAML emission
- ``base64_encode``  — base64 encode/decode
- ``url_encode``     — URL percent-encoding decode
- ``hex_dump``       — show the first N bytes of a file as hex+ASCII
- ``word_count``     — count words/lines/bytes in a file
"""
from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# json_query
# ---------------------------------------------------------------------------

class JsonQueryTool(Tool):
    name = "json_query"
    description = (
        "Extract a value from a JSON document by dotted path. "
        "Arrays use numeric indices (``users.0.name``). Returns the value "
        "as a JSON string; pass `default` to override the not-found value."
    )
    parameters = {
        "type": "object",
        "properties": {
            "data": {"type": "string", "description": "JSON string to query."},
            "path": {"type": "string", "description": "Dotted path, e.g. 'users.0.name'."},
            "default": {"type": "string", "description": "Return this if path is not found."},
        },
        "required": ["data", "path"],
    }

    def run(self, data: str, path: str, default: str = "", **_) -> ToolResult:
        try:
            doc = json.loads(data)
        except json.JSONDecodeError as exc:
            return ToolResult.failure(f"invalid JSON: {exc}")
        node: Any = doc
        for seg in path.split("."):
            if seg == "":
                continue
            if isinstance(node, list):
                try:
                    idx = int(seg)
                except ValueError:
                    return ToolResult.failure(f"array index must be int, got {seg!r}")
                if not (0 <= idx < len(node)):
                    return ToolResult.success(default)
                node = node[idx]
                continue
            if isinstance(node, dict):
                if seg not in node:
                    return ToolResult.success(default)
                node = node[seg]
                continue
            return ToolResult.success(default)
        if isinstance(node, (dict, list)):
            return ToolResult.success(json.dumps(node, ensure_ascii=False))
        if node is None:
            return ToolResult.success("null")
        return ToolResult.success(str(node))


# ---------------------------------------------------------------------------
# json_format
# ---------------------------------------------------------------------------

class JsonFormatTool(Tool):
    name = "json_format"
    description = "Pretty-print or minify a JSON string. `minify`=true removes whitespace."
    parameters = {
        "type": "object",
        "properties": {
            "data": {"type": "string"},
            "minify": {"type": "boolean", "description": "Default false (pretty)."},
        },
        "required": ["data"],
    }

    def run(self, data: str, minify: bool = False, **_) -> ToolResult:
        try:
            doc = json.loads(data)
        except json.JSONDecodeError as exc:
            return ToolResult.failure(f"invalid JSON: {exc}")
        if minify:
            return ToolResult.success(json.dumps(doc, separators=(",", ":"), ensure_ascii=False))
        return ToolResult.success(json.dumps(doc, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# csv_to_json
# ---------------------------------------------------------------------------

class CsvToJsonTool(Tool):
    name = "csv_to_json"
    description = "Convert CSV (string or file path) to a JSON array of objects."
    parameters = {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "CSV text, or a path prefixed with 'file:'."},
            "delimiter": {"type": "string", "description": "Field delimiter. Default ','."},
        },
        "required": ["input"],
    }

    def run(self, input: str, delimiter: str = ",", **_) -> ToolResult:
        text = input
        if input.startswith("file:"):
            try:
                text = Path(input[5:]).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        try:
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            rows = list(reader)
        except (csv.Error, ValueError) as exc:
            return ToolResult.failure(f"csv: {exc}")
        return ToolResult.success(json.dumps(rows, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# base64_encode + decode
# ---------------------------------------------------------------------------

class Base64EncodeTool(Tool):
    name = "base64_encode"
    description = "Base64-encode a string."
    parameters = {
        "type": "object",
        "properties": {"data": {"type": "string"}, "urlsafe": {"type": "boolean"}},
        "required": ["data"],
    }

    def run(self, data: str, urlsafe: bool = False, **_) -> ToolResult:
        try:
            enc = base64.urlsafe_b64encode if urlsafe else base64.b64encode
            return ToolResult.success(enc(data.encode("utf-8")).decode("ascii"))
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")


class Base64DecodeTool(Tool):
    name = "base64_decode"
    description = "Base64-decode a string to text."
    parameters = {
        "type": "object",
        "properties": {"data": {"type": "string"}, "urlsafe": {"type": "boolean"}},
        "required": ["data"],
    }

    def run(self, data: str, urlsafe: bool = False, **_) -> ToolResult:
        try:
            dec = base64.urlsafe_b64decode if urlsafe else base64.b64decode
            return ToolResult.success(dec(data.encode("ascii")).decode("utf-8", errors="replace"))
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            return ToolResult.failure(f"base64 decode: {exc}")


# ---------------------------------------------------------------------------
# url_encode / url_decode
# ---------------------------------------------------------------------------

class UrlEncodeTool(Tool):
    name = "url_encode"
    description = "Percent-encode a string for use in URLs."
    parameters = {
        "type": "object",
        "properties": {"data": {"type": "string"}, "path": {"type": "boolean", "description": "Use path-safe encoding (slash allowed). Default false."}},
        "required": ["data"],
    }

    def run(self, data: str, path: bool = False, **_) -> ToolResult:
        return ToolResult.success(urllib.parse.quote(data, safe="/" if path else ""))


class UrlDecodeTool(Tool):
    name = "url_decode"
    description = "Decode a percent-encoded URL string."
    parameters = {
        "type": "object",
        "properties": {"data": {"type": "string"}},
        "required": ["data"],
    }

    def run(self, data: str, **_) -> ToolResult:
        return ToolResult.success(urllib.parse.unquote(data))


# ---------------------------------------------------------------------------
# hex_dump
# ---------------------------------------------------------------------------

class HexDumpTool(Tool):
    name = "hex_dump"
    description = "Show the first N bytes of a file as hex+ASCII (xxd-style)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_bytes": {"type": "integer", "description": "Default 256."},
        },
        "required": ["path"],
    }

    def run(self, path: str, max_bytes: int = 256, **_) -> ToolResult:
        try:
            p = Path(path).expanduser()
            data = p.read_bytes()[:max_bytes]
        except OSError as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        out_lines: List[str] = []
        for i in range(0, len(data), 16):
            chunk = data[i:i+16]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            out_lines.append(f"{i:08x}  {hex_part:<48}  {ascii_part}")
        return ToolResult.success("\n".join(out_lines) if out_lines else "(empty)")


# ---------------------------------------------------------------------------
# word_count
# ---------------------------------------------------------------------------

class WordCountTool(Tool):
    name = "word_count"
    description = "Count lines, words, and bytes in a file or string."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Optional. If set, read this file."},
            "text": {"type": "string", "description": "Otherwise use this inline text."},
        },
    }

    def run(self, path: str = "", text: str = "", **_) -> ToolResult:
        try:
            if path:
                data = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
            else:
                data = text
        except OSError as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        lines = data.splitlines()
        words = re.findall(r"\S+", data)
        return ToolResult.success(
            f"lines: {len(lines)}\nwords: {len(words)}\nbytes: {len(data.encode('utf-8'))}"
        )


# ---------------------------------------------------------------------------
# yaml_to_json / json_to_yaml  — restricted subset
# ---------------------------------------------------------------------------

def _yaml_scalar(token: str) -> Any:
    """Best-effort scalar coercion for our tiny YAML subset."""
    if token in ("null", "~", ""):
        return None
    low = token.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if re.fullmatch(r"-?\d+\.\d+", token):
        return float(token)
    return token


def _yaml_to_json(text: str) -> Any:
    """A *very* restricted YAML parser.

    Supports: nested mappings (``key: value``), sequences (``- item``),
    strings, numbers, booleans, null. Strings with special characters
    must be quoted.

    This is NOT a full YAML implementation. It exists so the agent can
    read simple config files without a third-party dependency.
    """
    # Strip comments and blank lines.
    raw_lines: List[str] = []
    for line in text.splitlines():
        # Drop trailing comments outside of quotes (good-enough heuristic).
        in_quote = False
        out_chars: List[str] = []
        for ch in line:
            if ch in ('"', "'"):
                in_quote = not in_quote
            if ch == "#" and not in_quote:
                break
            out_chars.append(ch)
        stripped = "".join(out_chars).rstrip()
        if stripped.strip() == "":
            continue
        raw_lines.append(stripped)
    return _yaml_block(raw_lines, 0, 0)[0]


def _yaml_block(lines: List[str], idx: int, indent: int) -> tuple:
    """Parse a block of lines at the given indent. Returns (value, next_idx)."""
    if not lines or idx >= len(lines):
        return None, idx
    line = lines[idx]
    leading = len(line) - len(line.lstrip(" "))
    if leading < indent:
        return None, idx
    if line.lstrip().startswith("- "):
        return _yaml_seq(lines, idx, leading)
    return _yaml_map(lines, idx, leading)


def _yaml_map(lines: List[str], idx: int, indent: int) -> tuple:
    result: Dict[str, Any] = {}
    while idx < len(lines):
        line = lines[idx]
        lead = len(line) - len(line.lstrip(" "))
        if lead < indent:
            break
        if lead > indent or not line.lstrip().startswith("- "):
            stripped = line.lstrip()
            if ":" not in stripped:
                idx += 1
                continue
            key, _, rest = stripped.partition(":")
            key = key.strip()
            rest = rest.strip()
            if rest == "":
                # Nested block.
                if idx + 1 < len(lines):
                    next_lead = len(lines[idx + 1]) - len(lines[idx + 1].lstrip(" "))
                    if next_lead > indent:
                        child, idx = _yaml_block(lines, idx + 1, next_lead)
                        result[key] = child
                        continue
                result[key] = None
                idx += 1
                continue
            if rest.startswith("[") or rest.startswith("{"):
                # Inline flow style — not supported; fall back to scalar.
                result[key] = _yaml_scalar(rest)
                idx += 1
                continue
            result[key] = _yaml_scalar(rest)
            idx += 1
        else:
            break
    return result, idx


def _yaml_seq(lines: List[str], idx: int, indent: int) -> tuple:
    items: List[Any] = []
    while idx < len(lines):
        line = lines[idx]
        lead = len(line) - len(line.lstrip(" "))
        if lead < indent:
            break
        if not line.lstrip().startswith("- "):
            break
        body = line.lstrip()[2:]  # drop "- "
        if body == "":
            # Block nested under this item.
            if idx + 1 < len(lines):
                next_lead = len(lines[idx + 1]) - len(lines[idx + 1].lstrip(" "))
                if next_lead > indent:
                    child, idx = _yaml_block(lines, idx + 1, next_lead)
                    items.append(child)
                    continue
            items.append(None)
            idx += 1
            continue
        if ":" in body and not body.startswith(("'", '"')):
            # Inline map start: "- key: value" with possible continuation
            sub_lines = [" " * (indent + 2) + body]
            while True:
                if idx + 1 >= len(lines):
                    break
                next_line = lines[idx + 1]
                next_lead = len(next_line) - len(next_line.lstrip(" "))
                if next_lead <= indent:
                    break
                sub_lines.append(next_line)
                idx += 1
            sub: Dict[str, Any] = {}
            for sl in sub_lines:
                pass
            # Fall back to a small parser for the inline case.
            sub = _yaml_inline_map(body)
            items.append(sub)
            idx += 1
            continue
        items.append(_yaml_scalar(body))
        idx += 1
    return items, idx


def _yaml_inline_map(body: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    k, _, v = body.partition(":")
    out[k.strip()] = _yaml_scalar(v.strip())
    return out


class YamlToJsonTool(Tool):
    name = "yaml_to_json"
    description = (
        "Convert a restricted subset of YAML to JSON. Supports mappings, "
        "sequences, scalars (string / int / float / bool / null). Strings "
        "with special characters should be quoted. Not a full YAML parser."
    )
    parameters = {
        "type": "object",
        "properties": {
            "data": {"type": "string", "description": "YAML text."},
        },
        "required": ["data"],
    }

    def run(self, data: str, **_) -> ToolResult:
        try:
            parsed = _yaml_to_json(data)
            return ToolResult.success(json.dumps(parsed, indent=2, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"yaml parse: {exc}")


class JsonToYamlTool(Tool):
    name = "json_to_yaml"
    description = "Convert JSON to YAML. Limited subset (no anchors, no flow style)."
    parameters = {
        "type": "object",
        "properties": {"data": {"type": "string"}},
        "required": ["data"],
    }

    def run(self, data: str, **_) -> ToolResult:
        try:
            doc = json.loads(data)
        except json.JSONDecodeError as exc:
            return ToolResult.failure(f"invalid JSON: {exc}")
        return ToolResult.success(self._json_to_yaml(doc, indent=0))

    def _json_to_yaml(self, obj: Any, indent: int) -> str:  # noqa: A002
        pad = "  " * indent
        if isinstance(obj, dict):
            if not obj:
                return "{}"
            lines: List[str] = []
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    lines.append(f"{pad}{k}:")
                    lines.append(self._json_to_yaml(v, indent + 1))
                else:
                    lines.append(f"{pad}{k}: {_yaml_emit(v)}")
            return "\n".join(lines)
        if isinstance(obj, list):
            if not obj:
                return "[]"
            lines = []
            for item in obj:
                if isinstance(item, (dict, list)):
                    lines.append(f"{pad}-")
                    lines.append(self._json_to_yaml(item, indent + 1))
                else:
                    lines.append(f"{pad}- {_yaml_emit(item)}")
            return "\n".join(lines)
        return f"{pad}{_yaml_emit(obj)}"


def _yaml_emit(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # Quote if it contains YAML metacharacters.
    if re.search(r"[:#\-?\[\]{}\n,&\*!|>'\"%@`]", s) or s.strip() != s or s == "":
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

registry.register(JsonQueryTool())
registry.register(JsonFormatTool())
registry.register(CsvToJsonTool())
registry.register(Base64EncodeTool())
registry.register(Base64DecodeTool())
registry.register(UrlEncodeTool())
registry.register(UrlDecodeTool())
registry.register(HexDumpTool())
registry.register(WordCountTool())
registry.register(YamlToJsonTool())
registry.register(JsonToYamlTool())
