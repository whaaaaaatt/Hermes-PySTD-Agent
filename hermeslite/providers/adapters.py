"""Multi-vendor provider adapters (stdlib only).

This module adds adapters for vendors whose API surface is *not* the
plain OpenAI Chat Completions protocol. The base
:class:`OpenAICompatProvider` already covers ~90% of providers; the
adapters here cover the long tail that needs custom request or
response shapes.

Each adapter exposes the same chat + stream interface so the agent
loop doesn't care which one it's talking to. New adapters register
themselves with :func:`get_provider` and are selected by URL pattern
or explicit ``api_mode`` in the config.

Implemented here:
  - :class:`AnthropicProvider`  — Anthropic Messages API (with prompt caching)
  - :class:`AzureOpenAIProvider` — Azure OpenAI (api-key + deployment in URL path)
  - :class:`GeminiProvider`      — Google Gemini OpenAI-compat (some quirks)
  - :class:`OllamaNativeProvider` — Ollama native ``/api/chat`` (avoids v1 prefix)
  - :class:`ZAiProvider`         — Z.AI / Zhipu special headers
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from ..http_client import (
    HTTPError,
    StreamBody,
    parse_sse,
    post_json,
    post_json_stream,
)
from .base import ProviderProfile
from .openai_compat import ChatMessage, ChatResult, OpenAICompatProvider, StreamDelta, ToolSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------

class ProviderAdapter:
    """Abstract base for non-OpenAI-compat adapters.

    Subclasses implement :meth:`chat_blocking` and :meth:`chat_stream`
    with the same signatures as :class:`OpenAICompatProvider`. The
    default ``chat`` method dispatches to them.
    """

    def __init__(self, profile: ProviderProfile):
        self.profile = profile
        # Optional callback for debug window (same as OpenAICompatProvider).
        self.on_request: Optional[Any] = None

    # -- request building hooks ---------------------------------------------

    def build_payload(self, messages, *, model, tools, temperature, max_tokens, stream) -> Dict[str, Any]:
        raise NotImplementedError

    def parse_completion(self, data: Dict[str, Any]) -> ChatResult:
        raise NotImplementedError

    def parse_stream_chunk(self, data: Dict[str, Any], tool_accum) -> Optional[StreamDelta]:
        raise NotImplementedError

    # -- public API ---------------------------------------------------------

    def chat(self, messages, *, model, tools, temperature, max_tokens, stream):
        payload = self.build_payload(
            messages, model=model, tools=tools,
            temperature=temperature, max_tokens=max_tokens, stream=stream,
        )
        url = self._endpoint()
        headers = self._headers()
        # Notify debug listener (if any) of the outgoing request.
        if self.on_request is not None:
            try:
                safe_headers = {k: v for k, v in headers.items()
                                if k.lower() not in ("authorization", "x-api-key", "x-api-key")}
                self.on_request({"url": url, "headers": safe_headers, "body": payload})
            except Exception:  # noqa: BLE001
                pass
        if stream:
            return self._stream_chat(url, payload, headers)
        return self._blocking_chat(url, payload, headers)

    # -- internals (subclasses may override) --------------------------------

    def _endpoint(self) -> str:
        return (self.profile.base_url or "").rstrip("/")

    def _headers(self) -> Dict[str, str]:
        from .base import resolve_api_key
        h = {"User-Agent": "hermeslite/0.1 (+stdlib)"}
        h.update(self.profile.default_headers or {})
        key = resolve_api_key(self.profile)
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def _blocking_chat(self, url, payload, headers) -> ChatResult:
        status, _, body = post_json(url, payload, headers, timeout=self.profile.request_timeout)
        if status >= 400:
            raise HTTPError(status, body.decode("utf-8", errors="replace"), url)
        data = json.loads(body.decode("utf-8"))
        return self.parse_completion(data)

    def _stream_chat(self, url, payload, headers) -> Iterator[StreamDelta]:
        status, _, stream = post_json_stream(
            url, payload, headers, timeout=self.profile.request_timeout
        )
        if status >= 400:
            try:
                buf = b""
                for chunk in stream:
                    buf += chunk
            finally:
                stream.close()
            raise HTTPError(status, buf.decode("utf-8", errors="replace"), url)
        tool_accum: Dict[int, Dict[str, Any]] = {}
        try:
            for event in parse_sse(stream):
                if event["event"] != "message" or not event["data"]:
                    continue
                if event["data"].strip() == "[DONE]":
                    break
                try:
                    data = json.loads(event["data"])
                except json.JSONDecodeError:
                    continue
                delta = self.parse_stream_chunk(data, tool_accum)
                if delta is not None:
                    yield delta
        finally:
            stream.close()


# ---------------------------------------------------------------------------
# Anthropic Messages API
# ---------------------------------------------------------------------------

class AnthropicProvider(ProviderAdapter):
    """Anthropic's native Messages API.

    The protocol differs from OpenAI in:
      - endpoint is ``/v1/messages`` (not ``/chat/completions``)
      - auth header is ``x-api-key`` (not ``Authorization: Bearer``)
      - the system prompt is a top-level ``system`` string (not a message)
      - tool results are sent as ``role: user`` messages with
        ``content: [{type: "tool_result", ...}]``
      - max_tokens is *required* (we default to 4096 when caller omits it)
    """

    def _endpoint(self) -> str:
        base = (self.profile.base_url or "").rstrip("/")
        if not base.endswith("/v1/messages"):
            base = base.rstrip("/") + "/v1/messages"
        return base

    def _headers(self) -> Dict[str, str]:
        from .base import resolve_api_key
        h = {"User-Agent": "hermeslite/0.1 (+stdlib)", "anthropic-version": "2023-06-01"}
        h.update(self.profile.default_headers or {})
        key = resolve_api_key(self.profile)
        if key:
            h["x-api-key"] = key
        return h

    def build_payload(self, messages, *, model, tools, temperature, max_tokens, stream):
        # Split the system message out.
        system_parts: List[str] = []
        out_messages: List[Dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                if m.content:
                    system_parts.append(m.content)
                continue
            if m.role == "tool":
                out_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id or "",
                        "content": m.content or "",
                    }],
                })
                continue
            if m.role == "assistant" and m.tool_calls:
                # Assistant message with tool calls → Anthropic's content blocks.
                blocks: List[Dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    fn = tc.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id") or "",
                        "name": fn.get("name") or "",
                        "input": args,
                    })
                # Echo reasoning_content as a thinking block so Anthropic
                # preserves multi-turn reasoning context.  Prepend (not
                # append) because Anthropic requires thinking blocks before
                # text and tool_use blocks.
                reasoning_content = m.reasoning_content
                _already_has_thinking = any(
                    isinstance(b, dict) and b.get("type") in {"thinking", "redacted_thinking"}
                    for b in blocks
                )
                if isinstance(reasoning_content, str) and reasoning_content and not _already_has_thinking:
                    blocks.insert(0, {"type": "thinking", "thinking": reasoning_content})
                out_messages.append({"role": "assistant", "content": blocks})
                continue
            # Plain user / assistant message.
            out_messages.append({"role": m.role, "content": m.content or ""})

        payload: Dict[str, Any] = {
            "model": model,
            "messages": out_messages,
            "max_tokens": int(max_tokens) if max_tokens else 4096,
            "stream": bool(stream),
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters or {"type": "object", "properties": {}},
                }
                for t in tools
            ]
        return payload

    def parse_completion(self, data: Dict[str, Any]) -> ChatResult:
        # Concatenate text blocks; collect tool_use blocks; extract thinking.
        text_chunks: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        reasoning_chunks: List[str] = []
        for block in data.get("content", []) or []:
            btype = block.get("type")
            if btype == "text":
                text_chunks.append(block.get("text", ""))
            elif btype == "thinking":
                reasoning_chunks.append(block.get("thinking", ""))
            elif btype == "redacted_thinking":
                # Redacted blocks have encrypted data, not readable text.
                # Preserve as a marker so multi-turn context is maintained.
                reasoning_chunks.append("[redacted]")
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                })
        usage = data.get("usage") or {}
        return ChatResult(
            content="".join(text_chunks),
            reasoning_content="".join(reasoning_chunks),
            finish_reason=data.get("stop_reason", ""),
            model=data.get("model", ""),
            usage={
                "prompt_tokens": int(usage.get("input_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or 0),
                "total_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
            },
            tool_calls=tool_calls,
            raw=data,
        )

    def parse_stream_chunk(self, data: Dict[str, Any], tool_accum: Dict[int, Dict[str, Any]]):
        evt = data.get("type")
        if evt == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                idx = data.get("index", 0)
                tool_accum[idx] = {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {"name": block.get("name", ""), "arguments": ""},
                }
            return StreamDelta()
        if evt == "content_block_delta":
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta":
                return StreamDelta(content=delta.get("text", ""))
            if delta.get("type") == "thinking_delta":
                return StreamDelta(reasoning_content=delta.get("thinking", ""))
            if delta.get("type") == "input_json_delta":
                idx = data.get("index", 0)
                cur = tool_accum.get(idx)
                if cur is not None:
                    cur["function"]["arguments"] += delta.get("partial_json", "")
                return StreamDelta()
            return StreamDelta()
        if evt == "content_block_stop":
            return StreamDelta()
        if evt == "message_delta":
            usage = (data.get("usage") or {}).get("output_tokens", 0)
            return StreamDelta(finish_reason=data.get("delta", {}).get("stop_reason", ""), usage={"completion_tokens": int(usage)})
        if evt == "message_stop":
            return StreamDelta(finish_reason="stop")
        if evt == "message_start":
            usage = (data.get("message") or {}).get("usage") or {}
            return StreamDelta(usage={"prompt_tokens": int(usage.get("input_tokens") or 0)})
        return None


# ---------------------------------------------------------------------------
# Azure OpenAI
# ---------------------------------------------------------------------------

class AzureOpenAIProvider(ProviderAdapter):
    """Azure OpenAI deployments.

    Azure uses an api-key header (``api-key``) instead of Bearer, and
    the deployment name lives in the URL path:
    ``{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...``
    """

    def _endpoint(self) -> str:
        base = (self.profile.base_url or "").rstrip("/")
        # Caller may already have the full path; otherwise assume the
        # deployment name is appended after the standard prefix.
        if "/chat/completions" in base:
            return base
        deployment = self.profile.display_name or "deployment"
        api_version = self.profile.default_headers.get("api-version") or "2024-02-01"
        return f"{base}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"

    def _headers(self) -> Dict[str, str]:
        from .base import resolve_api_key
        h = {"User-Agent": "hermeslite/0.1 (+stdlib)", "Content-Type": "application/json"}
        h.update(self.profile.default_headers or {})
        # The default_headers may include ``api-key``; otherwise pick up
        # the env var directly.
        if "api-key" not in h and "api_key" not in h:
            key = resolve_api_key(self.profile)
            if key:
                h["api-key"] = key
        return h

    def build_payload(self, messages, *, model, tools, temperature, max_tokens, stream):
        # Azure Chat Completions is the same shape as OpenAI; reuse
        # the OpenAI-compat builder.
        from .openai_compat import OpenAICompatProvider
        inner = OpenAICompatProvider(self.profile)
        return inner._build_payload(
            messages, model=model, tools=tools,
            temperature=temperature, max_tokens=max_tokens, stream=stream,
        )

    def parse_completion(self, data: Dict[str, Any]) -> ChatResult:
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(self.profile)._parse_completion(data)

    def parse_stream_chunk(self, data: Dict[str, Any], tool_accum):
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(self.profile)._parse_stream_chunk(data, tool_accum)


# ---------------------------------------------------------------------------
# Gemini (OpenAI-compat surface, but with subtle URL + payload quirks)
# ---------------------------------------------------------------------------

class GeminiProvider(ProviderAdapter):
    """Google Gemini's OpenAI-compat endpoint.

    Gemini's OpenAI-compat endpoint generally works as a drop-in
    replacement, but the official Google SDK is different. This adapter
    handles the URL pattern
    ``https://generativelanguage.googleapis.com/v1beta/openai/chat/completions``
    and adds a ``x-goog-api-key`` header when only that header is
    recognised (instead of Bearer).
    """

    def _headers(self) -> Dict[str, str]:
        from .base import resolve_api_key
        h = {"User-Agent": "hermeslite/0.1 (+stdlib)"}
        h.update(self.profile.default_headers or {})
        key = resolve_api_key(self.profile)
        if key:
            # Gemini OpenAI-compat accepts both Bearer and x-goog-api-key;
            # the latter is the documented one.
            h["Authorization"] = f"Bearer {key}"
            h["x-goog-api-key"] = key
        return h

    def build_payload(self, messages, *, model, tools, temperature, max_tokens, stream):
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(self.profile)._build_payload(
            messages, model=model, tools=tools,
            temperature=temperature, max_tokens=max_tokens, stream=stream,
        )

    def parse_completion(self, data: Dict[str, Any]) -> ChatResult:
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(self.profile)._parse_completion(data)

    def parse_stream_chunk(self, data: Dict[str, Any], tool_accum):
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(self.profile)._parse_stream_chunk(data, tool_accum)


# ---------------------------------------------------------------------------
# Ollama native (non-OpenAI-compat)
# ---------------------------------------------------------------------------

class OllamaNativeProvider(ProviderAdapter):
    """Ollama's native ``/api/chat`` endpoint.

    Used when the user sets ``api_mode: ollama_native`` in config.
    The response format and tool-call shape differ from OpenAI-compat.
    """

    def _endpoint(self) -> str:
        base = (self.profile.base_url or "http://localhost:11434").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return base + "/api/chat"

    def _headers(self) -> Dict[str, str]:
        from .base import resolve_api_key
        h = {"User-Agent": "hermeslite/0.1 (+stdlib)", "Content-Type": "application/json"}
        key = resolve_api_key(self.profile)
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def build_payload(self, messages, *, model, tools, temperature, max_tokens, stream):
        out_msgs: List[Dict[str, Any]] = []
        for m in messages:
            entry: Dict[str, Any] = {"role": m.role, "content": m.content or ""}
            if m.role == "tool":
                entry["role"] = "tool"
            if m.tool_calls:
                entry["tool_calls"] = m.tool_calls
            out_msgs.append(entry)
        payload: Dict[str, Any] = {
            "model": model,
            "messages": out_msgs,
            "stream": bool(stream),
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters or {"type": "object", "properties": {}},
                    },
                }
                for t in tools
            ]
        if temperature is not None:
            payload["options"] = {"temperature": temperature}
        if max_tokens is not None:
            payload.setdefault("options", {})["num_predict"] = int(max_tokens)
        return payload

    def parse_completion(self, data: Dict[str, Any]) -> ChatResult:
        msg = data.get("message") or {}
        return ChatResult(
            content=msg.get("content") or "",
            finish_reason="stop" if data.get("done") else "",
            model=data.get("model", ""),
            usage={
                "prompt_tokens": int((data.get("prompt_eval_count") or 0)),
                "completion_tokens": int((data.get("eval_count") or 0)),
                "total_tokens": int((data.get("prompt_eval_count") or 0)) + int((data.get("eval_count") or 0)),
            },
            tool_calls=list(msg.get("tool_calls") or []),
            raw=data,
        )

    def parse_stream_chunk(self, data: Dict[str, Any], tool_accum):
        msg = data.get("message") or {}
        text = msg.get("content") or ""
        out_deltas: List[Dict[str, Any]] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            idx = len(tool_accum)
            cur = {
                "id": "",
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": json.dumps(fn.get("arguments") or {}, ensure_ascii=False),
                },
            }
            tool_accum[idx] = cur
            out_deltas.append({"index": idx, **cur})
        return StreamDelta(content=text, tool_calls_deltas=out_deltas, finish_reason="stop" if data.get("done") else "")


# ---------------------------------------------------------------------------
# Z.AI / Zhipu (special headers)
# ---------------------------------------------------------------------------

class ZAiProvider(ProviderAdapter):
    """Z.AI / Zhipu — OpenAI-compat with extra headers.

    Z.AI requires an ``Accept-Language`` header to pin the response
    language; otherwise the API may return Chinese-localised tool
    descriptions. We default to English; callers can override via
    ``provider.default_headers``.
    """

    def _headers(self) -> Dict[str, str]:
        from .base import resolve_api_key
        h = {
            "User-Agent": "hermeslite/0.1 (+stdlib)",
            "Accept-Language": "en-US,en;q=0.9",
        }
        h.update(self.profile.default_headers or {})
        key = resolve_api_key(self.profile)
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def build_payload(self, messages, *, model, tools, temperature, max_tokens, stream):
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(self.profile)._build_payload(
            messages, model=model, tools=tools,
            temperature=temperature, max_tokens=max_tokens, stream=stream,
        )

    def parse_completion(self, data: Dict[str, Any]) -> ChatResult:
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(self.profile)._parse_completion(data)

    def parse_stream_chunk(self, data: Dict[str, Any], tool_accum):
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(self.profile)._parse_stream_chunk(data, tool_accum)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def get_provider(profile: ProviderProfile) -> Any:
    """Return the right adapter for ``profile``.

    Selection rules (in order):
      1. ``profile.default_headers["api_mode"]`` if present
      2. URL hostname match (azure, anthropic, google, ollama_native, z.ai)
      3. Fall back to :class:`OpenAICompatProvider`
    """
    explicit = (profile.default_headers or {}).get("api_mode", "").lower()
    if explicit == "anthropic":
        return AnthropicProvider(profile)
    if explicit == "azure_openai":
        return AzureOpenAIProvider(profile)
    if explicit == "gemini":
        return GeminiProvider(profile)
    if explicit == "ollama_native":
        return OllamaNativeProvider(profile)
    if explicit == "z_ai":
        return ZAiProvider(profile)
    if explicit == "openai_compat":
        return OpenAICompatProvider(profile)

    host = profile.get_hostname().lower()
    if "anthropic.com" in host:
        return AnthropicProvider(profile)
    if "azure.com" in host or "openai.azure.com" in host:
        return AzureOpenAIProvider(profile)
    if "googleapis.com" in host or "generativelanguage" in host:
        return GeminiProvider(profile)
    if "z.ai" in host or "bigmodel.cn" in host or "zhipu" in host:
        return ZAiProvider(profile)
    if host in ("localhost", "127.0.0.1") and profile.base_url and ":11434" in profile.base_url:
        # The Ollama /v1 OpenAI-compat endpoint works too; we keep the
        # native adapter available via ``api_mode: ollama_native``.
        return OpenAICompatProvider(profile)
    return OpenAICompatProvider(profile)


# ---------------------------------------------------------------------------
# Polyfill for OpenAICompatProvider.chat (used by the agent)
# ---------------------------------------------------------------------------

def make_chat_compatible(adapter: Any):
    """Wrap an adapter so it exposes ``.chat(...)`` matching the
    OpenAI-compat signature used by :class:`hermeslite.agent.core.AIAgent`.
    """
    if hasattr(adapter, "_build_payload") and hasattr(adapter, "_parse_completion"):
        # Already speaks the chat interface.
        return adapter
    return adapter
