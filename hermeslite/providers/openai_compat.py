"""OpenAI-compatible chat client (urllib only).

Covers the 90% case:
  - OpenAI, OpenRouter, DeepSeek, Z.AI, Mistral, Groq, etc.
  - Ollama, vLLM, llama.cpp OpenAI shim
  - Any provider that exposes ``POST {base_url}/chat/completions`` with
    Bearer auth and ``stream: true`` SSE.

We deliberately do NOT support Anthropic's native Messages API — if a
user wants that, they put an OpenAI-compat proxy in front of it.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from ..http_client import (
    HTTPError,
    StreamBody,
    TransientError,
    parse_sse,
    post_json,
    post_json_stream,
)
from .base import ProviderProfile, resolve_api_key

logger = logging.getLogger(__name__)


def _is_balanced_json(s: str) -> bool:
    """Cheap check: does ``s`` parse as JSON? Used to detect providers
    that send the full ``arguments`` string on every delta."""
    if not s or not s.strip():
        return False
    try:
        json.loads(s)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ChatMessage:
    """One turn in the conversation.

    ``tool_calls`` is the assistant-emitted list of tool calls; only set
    on assistant messages. ``tool_call_id`` is set on tool-role messages
    so the provider can match them back to the originating call.

    ``content`` may be a plain string OR a list of content parts for
    multimodal messages (OpenAI vision format)::

        [
            {"type": "text", "text": "What do you see?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        ]

    ``reasoning_content`` holds the model's thinking/reasoning output
    for providers that support it (e.g., DeepSeek, Kimi).
    """
    role: str
    content: Any = None  # str | list[dict] | None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    reasoning_content: Optional[str] = None


@dataclass
class ToolSpec:
    """An OpenAI-style tool/function spec (JSON schema)."""
    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatResult:
    """A complete, non-streaming response."""
    content: str
    reasoning_content: str = ""
    finish_reason: str = ""
    model: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    raw: Optional[Dict[str, Any]] = None


@dataclass
class StreamDelta:
    """One delta from a streaming response.

    ``content`` is the text fragment (may be empty for tool-call deltas).
    ``reasoning_content`` is the thinking/reasoning fragment.
    ``tool_calls_deltas`` mirrors the OpenAI shape: a list of partial
    tool-call chunks keyed by index. ``finish_reason`` is non-empty only
    on the final chunk.
    """
    content: str = ""
    reasoning_content: str = ""
    tool_calls_deltas: List[Dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    usage: Optional[Dict[str, int]] = None
    model: str = ""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class OpenAICompatProvider:
    """A single client class for every OpenAI-compatible endpoint.

    The profile tells us *where* to talk and *how* to authenticate. The
    chat method sends a single request (streaming or not) and returns
    the parsed response.
    """

    def __init__(self, profile: ProviderProfile):
        self.profile = profile
        # Optional callback for debug window: called with the full request
        # payload before each LLM API call.  Signature: ``on_request(dict)``.
        self.on_request: Optional[Any] = None

    # -- public API ----------------------------------------------------------

    def chat(
        self,
        messages: List[ChatMessage],
        *,
        model: str,
        tools: Optional[List[ToolSpec]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        extra: Optional[Dict[str, Any]] = None,
    ):
        """Send a chat completion request.

        Returns a :class:`ChatResult` if ``stream=False``; otherwise returns
        an iterator of :class:`StreamDelta`.

        ``extra`` lets callers pass arbitrary provider-specific fields
        (``top_p``, ``presence_penalty``, ``response_format``,
        ``reasoning_effort``, …) without this client hard-coding every
        parameter. Keys in ``extra`` override the standard fields above.
        """
        url = self._endpoint("/chat/completions")
        payload = self._build_payload(
            messages, model=model, tools=tools,
            temperature=temperature, max_tokens=max_tokens, stream=stream,
            extra=extra,
        )
        headers = self._headers()
        # Notify debug listener (if any) of the outgoing request.
        if self.on_request is not None:
            try:
                safe_headers = {k: v for k, v in headers.items()
                                if k.lower() not in ("authorization", "x-api-key")}
                self.on_request({"url": url, "headers": safe_headers, "body": payload})
            except Exception:  # noqa: BLE001
                pass
        if stream:
            return self._stream_chat(url, payload, headers)
        return self._blocking_chat(url, payload, headers)

    def fetch_models(self) -> Optional[List[str]]:
        """Try to fetch the live model catalog. Returns ``None`` on failure."""
        url = self._endpoint("/models")
        headers = self._headers()
        try:
            from ..http_client import get_json
            data = get_json(url, headers=headers, timeout=8.0)
            items = data if isinstance(data, list) else data.get("data", [])
            return [m["id"] for m in items if isinstance(m, dict) and "id" in m]
        except Exception as exc:
            logger.debug("fetch_models(%s): %s", self.profile.name, exc)
            return None

    # -- request building ----------------------------------------------------

    def _endpoint(self, path: str) -> str:
        base = (self.profile.base_url or "").rstrip("/")
        if not base:
            raise ValueError(
                f"provider {self.profile.name!r} has no base_url configured"
            )
        if not path.startswith("/"):
            path = "/" + path
        return base + path

    def _headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {
            "User-Agent": "hermeslite/0.1 (+stdlib)",
        }
        h.update(self.profile.default_headers or {})
        key = resolve_api_key(self.profile)
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def _needs_thinking_reasoning_pad(self, model: str) -> bool:
        """Return True when the active provider enforces reasoning_content echo-back.

        DeepSeek, Kimi/Moonshot, and Xiaomi MiMo in thinking mode reject
        replays of assistant messages that omit ``reasoning_content`` with
        HTTP 400.  Detection is hostname- and model-driven so that
        aggregators (OpenRouter, etc.) that re-export these models via
        their own protocol are not affected.
        """
        host = self.profile.get_hostname().lower()
        m = (model or "").lower()
        # DeepSeek
        if ("deepseek" in m or "api.deepseek.com" in host):
            return True
        # Kimi / Moonshot
        if any(h in host for h in ("api.kimi.com", "moonshot.ai", "moonshot.cn")):
            return True
        # Xiaomi MiMo
        if ("mimo" in m or "api.xiaomimimo.com" in host or "xiaomimimo.com" in host):
            return True
        return False

    def _build_payload(
        self,
        messages: List[ChatMessage],
        *,
        model: str,
        tools: Optional[List[ToolSpec]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        stream: bool,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # Serialize all messages first, then echo reasoning_content back for
        # providers that require it (DeepSeek/Kimi/MiMo thinking mode).
        api_messages = [self._serialize_message(m) for m in messages]
        needs_pad = self._needs_thinking_reasoning_pad(model)
        if needs_pad:
            from ..agent.thinking import copy_reasoning_content_for_api
            for msg_obj, api_msg in zip(messages, api_messages):
                if msg_obj.role == "assistant":
                    copy_reasoning_content_for_api(msg_obj, api_msg, needs_thinking_pad=True)

        out: Dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "stream": bool(stream),
        }
        if tools:
            out["tools"] = [self._serialize_tool(t) for t in tools]
        if temperature is not None:
            out["temperature"] = temperature
        if max_tokens is not None:
            out["max_tokens"] = max_tokens
        # Pass-through of arbitrary provider-specific fields (top_p,
        # presence_penalty, response_format, reasoning_effort, etc.)
        # so callers can configure the model without us hard-coding every
        # parameter. Keys here override the defaults above; the caller's
        # intent wins.
        if extra:
            for k, v in extra.items():
                if v is None:
                    continue
                # Convenience: ``thinking: true`` is converted to the
                # chat_template_kwargs format used by vLLM, Agnes, and
                # other OpenAI-compatible providers that gate extended
                # thinking behind a template flag.
                if k == "thinking" and v is True:
                    ct = out.setdefault("chat_template_kwargs", {})
                    if isinstance(ct, dict):
                        ct["enable_thinking"] = True
                    continue
                out[k] = v
        return out

    def _serialize_message(self, m: ChatMessage) -> Dict[str, Any]:
        out: Dict[str, Any] = {"role": m.role}
        # Per the OpenAI spec, content may be null for assistant tool-call
        # turns. Most providers accept "" but a few reject it. We emit
        # whatever the caller stored and let ``None`` pass through.
        if m.content is not None:
            out["content"] = m.content
        if m.name:
            out["name"] = m.name
        if m.tool_calls:
            out["tool_calls"] = m.tool_calls
        if m.tool_call_id:
            out["tool_call_id"] = m.tool_call_id
        # Echo reasoning_content back to the API when present.  DeepSeek,
        # Kimi, and MiMo in thinking mode require this field on every
        # assistant message; omitting it causes HTTP 400.  For providers
        # that don't recognise the field, it is silently ignored.
        if m.reasoning_content:
            out["reasoning_content"] = m.reasoning_content
        return out

    def _serialize_tool(self, t: ToolSpec) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                # ``parameters`` defaults to {} which the spec disallows
                # (must be an object); we coerce to a permissive schema.
                "parameters": t.parameters or {"type": "object", "properties": {}},
            },
        }

    # -- blocking call -------------------------------------------------------

    def _blocking_chat(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
    ) -> ChatResult:
        status, _, body = post_json(url, payload, headers, timeout=self.profile.request_timeout)
        if status >= 400:
            raise HTTPError(status, body.decode("utf-8", errors="replace"), url)
        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPError(status, f"invalid JSON: {exc}", url) from exc
        return self._parse_completion(data)

    def _parse_completion(self, data: Dict[str, Any]) -> ChatResult:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        # reasoning_content may be at message level, in provider_specific_fields,
        # or under the 'reasoning' key (Moonshot, Novita, etc.).
        reasoning = msg.get("reasoning_content") or ""
        if not reasoning:
            reasoning = msg.get("reasoning") or ""
        if not reasoning:
            psf = msg.get("provider_specific_fields") or {}
            reasoning = psf.get("reasoning_content") or psf.get("reasoning") or ""
        return ChatResult(
            content=msg.get("content") or "",
            reasoning_content=reasoning,
            finish_reason=choice.get("finish_reason") or "",
            model=data.get("model") or "",
            usage={
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
            tool_calls=list(msg.get("tool_calls") or []),
            raw=data,
        )

    # -- streaming -----------------------------------------------------------

    def _stream_chat(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Iterator[StreamDelta]:
        status, resp_headers, stream = post_json_stream(
            url, payload, headers, timeout=self.profile.request_timeout
        )
        if status >= 400:
            # Drain the body so the connection closes cleanly.
            try:
                buf = b""
                for chunk in stream:
                    buf += chunk
            finally:
                stream.close()
            raise HTTPError(status, buf.decode("utf-8", errors="replace"), url)

        # Accumulator for tool-call deltas. OpenAI streams tool calls
        # fragment-by-fragment: the first delta has id+name+index, later
        # deltas append to the ``arguments`` string. We rebuild full
        # tool-call dicts as we go and emit the cumulative state in
        # ``StreamDelta.tool_calls_deltas`` (one entry per index).
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
                    # Skip malformed frames; the next frame will be the
                    # usual content delta. Some providers send keepalives
                    # that aren't JSON.
                    continue
                delta = self._parse_stream_chunk(data, tool_accum)
                if delta is not None:
                    yield delta
        finally:
            stream.close()

    def _parse_stream_chunk(
        self,
        data: Dict[str, Any],
        tool_accum: Dict[int, Dict[str, Any]],
    ) -> Optional[StreamDelta]:
        """Convert one SSE frame into a :class:`StreamDelta`."""
        choice = (data.get("choices") or [])
        if not choice:
            # Some providers send a final frame with only usage; capture it.
            usage = data.get("usage")
            if usage:
                return StreamDelta(
                    finish_reason="stop",
                    usage={
                        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                        "completion_tokens": int(usage.get("completion_tokens") or 0),
                        "total_tokens": int(usage.get("total_tokens") or 0),
                    },
                    model=data.get("model") or "",
                )
            return None
        choice0 = choice[0]
        delta = choice0.get("delta") or {}
        content = delta.get("content") or ""
        if isinstance(content, list):
            # Anthropic-via-proxy sometimes streams content as a list of
            # typed parts. Flatten the text fragments.
            fragments = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    fragments.append(part.get("text") or "")
            content = "".join(fragments)

        # reasoning_content — some providers (Agnes, DeepSeek) stream
        # thinking tokens in a separate field.  Also check the top-level
        # 'reasoning' key used by Moonshot, Novita, etc.
        reasoning = delta.get("reasoning_content") or ""
        if not reasoning:
            reasoning = delta.get("reasoning") or ""
        if not reasoning:
            psf = delta.get("provider_specific_fields") or {}
            reasoning = psf.get("reasoning_content") or psf.get("reasoning") or ""

        # Tool-call deltas
        out_deltas: List[Dict[str, Any]] = []
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index")
            if idx is None:
                continue
            cur = tool_accum.setdefault(idx, {
                "id": "", "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if tc.get("id"):
                cur["id"] = tc["id"]
            if tc.get("type"):
                cur["type"] = tc["type"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                cur["function"]["name"] = fn["name"]
            # Some providers send the FULL ``arguments`` string on every
            # delta (instead of appending a fragment). The OpenAI spec
            # is for the provider to APPEND fragments; some providers
            # (e.g. Agnes) IGNORE the spec and resend the entire
            # string each time. Concatenating those creates garbage
            # like ``{"a":1}{"a":1}`` which the tool can't parse.
            #
            # Detection rule: if the running string is non-empty AND
            # both the running string and the incoming chunk are
            # valid JSON, the provider is in "full-string mode" and
            # the latest one wins. The equality case (provider
            # resends the same string) is a no-op since assigning
            # incoming to itself doesn't change anything.
            #
            # Standard OpenAI incremental fragments are NOT valid
            # JSON on their own, so they fall through to the else
            # branch (append).
            if fn.get("arguments"):
                incoming = fn["arguments"]
                running = cur["function"]["arguments"]
                if (running
                        and _is_balanced_json(incoming)
                        and _is_balanced_json(running)):
                    # Both sides are valid JSON. The provider is
                    # either resending the same complete string
                    # (no-op) or a different one (replace). Either
                    # way, taking incoming is the correct answer.
                    cur["function"]["arguments"] = incoming
                else:
                    # Standard OpenAI incremental: append the
                    # fragment to the running buffer.
                    cur["function"]["arguments"] = running + incoming
            # Emit a *copy* so callers can mutate without affecting state.
            out_deltas.append({
                "index": idx,
                "id": cur["id"],
                "type": cur["type"],
                "function": {
                    "name": cur["function"]["name"],
                    "arguments": cur["function"]["arguments"],
                },
            })

        finish = choice0.get("finish_reason") or ""
        usage = data.get("usage")
        if usage:
            usage = {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            }
        return StreamDelta(
            content=content,
            reasoning_content=reasoning,
            tool_calls_deltas=out_deltas,
            finish_reason=finish,
            usage=usage,
            model=data.get("model") or "",
        )
