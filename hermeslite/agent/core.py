"""AIAgent — the conversation loop, distilled.

This is the Lite version of the original ``run_agent.AIAgent``. The
core responsibilities:

  1. Hold the message history (system + user + assistant + tool).
  2. Send a request to the model provider (streaming or blocking).
  3. Detect tool calls in the response and dispatch them via the
     :class:`ToolRegistry`.
  4. Append tool results as ``role: tool`` messages and re-prompt.
  5. Repeat until the model produces a final text answer or hits the
     iteration cap.
  6. Persist every turn to the state store.

The agent deliberately does NOT do context compression, error-classifier
fallback chains, prompt caching, or trajectory recording. Those are
upstream features that add complexity for marginal benefit at our scale.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..providers import (
    ChatMessage,
    ChatResult,
    OpenAICompatProvider,
    ProviderProfile,
    StreamDelta,
    ToolSpec,
)
from ..state import Message, StateStore, UsageRecord
from ..tools.registry import Tool, ToolResult, ToolRegistry
from .prompt import build_system_prompt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context-length error detection
# ---------------------------------------------------------------------------

def _is_context_length_error(exc: Exception) -> bool:
    """Return True if the exception signals a context-length / 413 error."""
    from ..http_client import HTTPError
    if isinstance(exc, HTTPError):
        if exc.status == 413:
            return True
        body_lower = (exc.body or "").lower()
        if "context_length_exceeded" in body_lower or "maximum context" in body_lower:
            return True
    msg = str(exc).lower()
    if "context_length_exceeded" in msg or "maximum context" in msg:
        return True
    return False


def _is_transient_error(exc: Exception) -> bool:
    """Return True if the exception signals a transient / retryable error.

    Covers rate limiting (429), server errors (5xx), and network glitches.
    """
    from ..http_client import HTTPError, TransientError
    if isinstance(exc, TransientError):
        return True
    if isinstance(exc, HTTPError):
        return exc.status in {429, 500, 502, 503, 504, 408, 425}
    msg = str(exc).lower()
    if "rate limit" in msg or "throttl" in msg:
        return True
    return False


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class AgentTurnResult:
    """The outcome of one user turn (which may include multiple round-trips
    with the model due to tool calls).
    """
    final_text: str
    iterations: int
    usage: Dict[str, int] = field(default_factory=dict)
    session_id: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

# Cap to prevent runaway loops. 25 is enough for almost every realistic
# task; raise via ``AIAgent(max_iterations=...)`` if you really need more.
DEFAULT_MAX_ITERATIONS = 25


class AIAgent:
    """One agent = one model + one tool set + one session."""

    def __init__(
        self,
        *,
        cfg: Dict[str, Any],
        profile: ProviderProfile,
        registry: ToolRegistry,
        state: StateStore,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        stream: bool = True,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        compress_threshold: float = 0,  # 0 = never auto-compress; else fraction of max_context_tokens
        compress_target: int = 30,    # keep this many recent messages after compression
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ):
        self.cfg = cfg
        self.profile = profile
        self.registry = registry
        self.state = state
        # Pick the right provider adapter (auto-detected from URL).
        from ..providers import get_provider
        self.provider = get_provider(profile)
        self.model = model or (cfg.get("model") or {}).get("name") or ""
        self.session_id = session_id or uuid.uuid4().hex
        self.max_iterations = max_iterations
        self.stream = stream
        self.on_event = on_event  # (kind, payload) callback for the UI
        self.compress_threshold = compress_threshold
        self.compress_target = compress_target
        # Generation parameters. Each can be overridden per-turn by the
        # caller. None means "let the provider decide".
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra = extra or {}
        self.max_context_tokens = int((cfg.get("model") or {}).get("max_context_tokens") or 0)

        # Anti-throttle: skip auto-compression after consecutive
        # ineffective compressions (< 10% token savings each).
        self._compress_ineffective = 0
        self._compress_skip_turns = 0

        # Debug window: when enabled, the agent emits llm_request /
        # llm_response SSE events so the frontend can display them.
        self.debug_enabled: bool = (cfg.get("debug") or {}).get("enabled", True)

        # Interrupt support: another thread can call interrupt() to
        # stop the current turn cleanly (close stream, break loop).
        self._interrupt_event = threading.Event()
        self._interrupt_message: str = ""
        self._active_stream_body: Any = None  # StreamBody for mid-stream abort

        # Build the active tool list (respect enabled/disabled from cfg).
        tools_cfg = (cfg.get("tools") or {})
        self.tools: List[Tool] = registry.filter(
            tools_cfg.get("enabled") or ["*"],
            tools_cfg.get("disabled") or [],
        )
        self.tool_specs: List[ToolSpec] = [
            ToolSpec(
                name=t.name,
                description=t.description,
                parameters=t.parameters,
            )
            for t in self.tools
        ]

        # Lazily-loaded skills section in the system prompt. We pass an
        # empty list here; the CLI's ``chat`` command discovers skills
        # once at session start and passes the result via the
        # ``system_prompt`` arg.
        self._system_prompt = system_prompt or ""

        # Ensure the session row exists in the store.
        existing = state.get_session(self.session_id)
        if existing is None:
            state.create_session(
                session_id=self.session_id,
                model=self.model,
                provider=profile.name,
                source=("cli" if not session_id else "web"),
            )
        else:
            # On session resumption, reuse the stored system prompt
            # (aligned with ref: keeps prompt cache warm).
            stored = state.get_session_system_prompt(self.session_id)
            if stored and not self._system_prompt:
                self._system_prompt = stored

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    @property
    def system_prompt(self) -> str:
        """Return the configured system prompt. Computed on first access
        if the user did not pass one in. Persisted to the session on
        first build so that resumption reuses the exact same text.
        """
        if not self._system_prompt:
            from .prompt import build_system_prompt
            from ..skills.loader import discover_skills
            self._system_prompt = build_system_prompt(
                self.cfg, self.tools, skills=discover_skills(),
                model_override=self.model,
            )
            # Persist for reuse on session resumption.
            try:
                self.state.update_session_system_prompt(
                    self.session_id, self._system_prompt
                )
            except Exception:  # noqa: BLE001
                logger.debug("failed to persist system_prompt for session %s", self.session_id)
        return self._system_prompt

    # ------------------------------------------------------------------
    # Conversation loop
    # ------------------------------------------------------------------

    def run_turn(self, user_message) -> AgentTurnResult:
        """Run one user turn. Returns the final assistant text + metadata.

        ``user_message`` may be a plain string or a list of content parts
        (OpenAI multimodal format) for image/file attachments.
        """
        # Clear any leftover interrupt state from a previous turn.
        self._interrupt_event.clear()
        self._interrupt_message = ""

        # Clean up orphaned assistant messages from interrupted turns.
        self._cleanup_orphaned_tool_calls()

        # Optional auto-compression before the turn starts.
        if self.compress_threshold > 0:
            if self._compress_skip_turns > 0:
                self._compress_skip_turns -= 1
            else:
                # Compute absolute threshold from percentage.
                # Floor at MINIMUM_CONTEXT_LENGTH (64K) to avoid
                # useless compression on small context windows.
                MINIMUM_CONTEXT_LENGTH = 64_000
                abs_threshold = max(
                    int(self.max_context_tokens * self.compress_threshold),
                    MINIMUM_CONTEXT_LENGTH,
                )
                try:
                    from .compress import compress_session
                    result_cmp = compress_session(
                        self.state, self.session_id,
                        profile=self.profile, model=self.model,
                        threshold=abs_threshold,
                        target=self.compress_target,
                        use_model_summary=False,  # count-based by default
                    )
                    # Anti-throttle: track ineffective compressions.
                    if result_cmp.triggered:
                        savings = result_cmp.tokens_before - result_cmp.tokens_after
                        pct = savings / max(1, result_cmp.tokens_before)
                        if pct < 0.10:
                            self._compress_ineffective += 1
                            if self._compress_ineffective >= 2:
                                self._compress_skip_turns = 5
                                self._compress_ineffective = 0
                        else:
                            self._compress_ineffective = 0
                except Exception as exc:  # noqa: BLE001
                    logger.debug("auto-compress: %s", exc)

        # Persist the user message.
        self.state.add_message(self.session_id, Message(role="user", content=user_message))

        messages: List[ChatMessage] = [ChatMessage(role="system", content=self.system_prompt)]
        # Replay history for the model. Skip our own system message — we
        # have a single one already in the head.
        for m in self.state.list_messages(self.session_id):
            messages.append(self._to_chat_message(m))

        # Drop thinking-only assistant turns and merge adjacent user messages.
        # This runs on the API copy only — the stored history is never mutated.
        from .thinking import drop_thinking_only_and_merge_users
        messages = drop_thinking_only_and_merge_users(messages)

        iterations = 0
        total_prompt = 0
        total_completion = 0
        all_tool_calls: List[Dict[str, Any]] = []

        final_text = ""
        final_reasoning = ""
        context_retries = 0
        transient_retries = 0
        MAX_TRANSIENT_RETRIES = 3
        while iterations < self.max_iterations:
            if self._interrupt_event.is_set():
                break
            iterations += 1
            self._emit("iteration_start", {"n": iterations})
            try:
                assistant_text, tool_calls, usage, reasoning_content = self._step(messages)
            except Exception as exc:  # noqa: BLE001
                # If interrupted (e.g. StreamBody.close() from another
                # thread), break out immediately — do not retry.
                if self._interrupt_event.is_set():
                    break
                # Detect context-length errors (413 / context_length_exceeded)
                # and trigger compression + retry once.
                if context_retries < 1 and _is_context_length_error(exc):
                    context_retries += 1
                    logger.info("context length exceeded — compressing and retrying")
                    try:
                        from .compress import compress_session
                        compress_session(
                            self.state, self.session_id,
                            profile=self.profile, model=self.model,
                            threshold=0,  # force compress regardless of size
                            target=self.compress_target,
                            use_model_summary=False,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    # Rebuild the messages list from compressed history.
                    messages = [ChatMessage(role="system", content=self.system_prompt)]
                    for m in self.state.list_messages(self.session_id):
                        messages.append(self._to_chat_message(m))
                    continue
                # Detect transient / retryable errors (429, 5xx, network)
                # and retry with exponential backoff.
                if transient_retries < MAX_TRANSIENT_RETRIES and _is_transient_error(exc):
                    transient_retries += 1
                    import time as _time
                    backoff = min(2.0 * (2 ** (transient_retries - 1)), 30.0)
                    logger.info(
                        "transient error (attempt %d/%d) — retrying in %.1fs: %s",
                        transient_retries, MAX_TRANSIENT_RETRIES, backoff, exc,
                    )
                    self._emit("retry_status", {
                        "attempt": transient_retries,
                        "max_attempts": MAX_TRANSIENT_RETRIES,
                        "wait_seconds": round(backoff, 1),
                        "reason": str(exc)[:200],
                    })
                    _time.sleep(backoff)
                    continue
                raise
            # prompt_tokens: use the LATEST call's value (represents the
            # full context sent to the model on that call). Do NOT
            # accumulate — each iteration already includes the full context.
            total_prompt = int(usage.get("prompt_tokens") or 0)
            # completion_tokens: accumulate across iterations (each
            # iteration generates new output).
            total_completion += int(usage.get("completion_tokens") or 0)
            all_tool_calls.extend(tool_calls)
            final_text = assistant_text or final_text
            final_reasoning = reasoning_content or final_reasoning

            if not tool_calls:
                # Model returned a final text — we're done.
                break

            # Append the assistant turn to history (with tool calls).
            self.state.add_message(
                self.session_id,
                Message(
                    role="assistant",
                    content=assistant_text,
                    tool_calls=tool_calls if tool_calls else None,
                    reasoning_content=reasoning_content,
                ),
            )
            messages.append(ChatMessage(
                role="assistant",
                content=assistant_text or None,
                tool_calls=tool_calls,
                reasoning_content=reasoning_content,
            ))

            # Execute each tool call and append the result.
            for tc in tool_calls:
                if self._interrupt_event.is_set():
                    break
                result = self._execute_tool_call(tc)
                self._emit("tool_result", {"call_id": tc.get("id"), "ok": result.ok, "data": _truncate_for_log(result.data), "error": result.error})
                # Persist large tool results to disk (spillover).
                from .compress import maybe_persist_tool_result
                result_text = result.to_message()
                result_text = maybe_persist_tool_result(
                    result_text,
                    tool_name=(tc.get("function") or {}).get("name", ""),
                    tool_use_id=tc.get("id", ""),
                )
                self.state.add_message(
                    self.session_id,
                    Message(
                        role="tool",
                        content=result_text,
                        tool_call_id=tc.get("id"),
                        name=(tc.get("function") or {}).get("name"),
                    ),
                )
                messages.append(ChatMessage(
                    role="tool",
                    content=result.to_message(),
                    tool_call_id=tc.get("id"),
                    name=(tc.get("function") or {}).get("name"),
                ))

        # Emit interrupt event if the turn was interrupted.
        if self._interrupt_event.is_set():
            self._emit("turn_interrupted", {
                "message": self._interrupt_message or "Interrupted by user",
                "partial_text": final_text or "",
            })

        # Persist final assistant text.
        if final_text:
            self.state.add_message(self.session_id, Message(role="assistant", content=final_text, reasoning_content=final_reasoning))

        # Record usage once per user turn.
        if total_prompt or total_completion:
            self.state.record_usage(UsageRecord(
                session_id=self.session_id,
                prompt_tokens=total_prompt,
                completion_tokens=total_completion,
                total_tokens=total_prompt + total_completion,
                model=self.model,
            ))

        # Touch the session so it sorts to the top.
        self.state.update_session(self.session_id, model=self.model, provider=self.profile.name)

        return AgentTurnResult(
            final_text=final_text,
            iterations=iterations,
            usage={
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "total_tokens": total_prompt + total_completion,
                "max_context_tokens": self.max_context_tokens,
            },
            session_id=self.session_id,
            tool_calls=all_tool_calls,
        )

    # ------------------------------------------------------------------
    # One model call
    # ------------------------------------------------------------------

    def _step(self, messages: List[ChatMessage]):
        """Make one model call, return ``(text, tool_calls, usage)``.

        For streaming calls we accumulate the final text and tool-call
        state. For blocking calls we use the parsed :class:`ChatResult`.
        Both code paths emit incremental ``text`` events to the UI.
        """
        if self.stream:
            return self._step_stream(messages)
        return self._step_blocking(messages)

    def _step_blocking(self, messages: List[ChatMessage]):
        # Set up debug callback for this provider call.
        if self.debug_enabled:
            self.provider.on_request = lambda req: self._emit("llm_request", req)
        result: ChatResult = self.provider.chat(
            messages, model=self.model, tools=self.tool_specs or None,
            temperature=self.temperature, max_tokens=self.max_tokens,
            extra=self.extra,
        )
        if result.reasoning_content:
            self._emit("thinking_content", {"text": result.reasoning_content})
        self._emit("assistant_text_done", {"text": result.content})
        # Emit debug response summary.
        if self.debug_enabled:
            self._emit("llm_response", {
                "model": result.model,
                "finish_reason": result.finish_reason,
                "usage": result.usage or {},
                "content_preview": (result.content or "")[:500],
                "tool_calls_count": len(result.tool_calls),
            })
        self.provider.on_request = None
        return result.content, result.tool_calls, result.usage, result.reasoning_content

    def _step_stream(self, messages: List[ChatMessage]):
        # Set up debug callback for this provider call.
        if self.debug_enabled:
            self.provider.on_request = lambda req: self._emit("llm_request", req)
        stream = self.provider.chat(
            messages, model=self.model, tools=self.tool_specs or None,
            stream=True, temperature=self.temperature, max_tokens=self.max_tokens,
            extra=self.extra,
        )
        # Save StreamBody reference for interrupt() — allows closing the
        # underlying HTTP socket from another thread.
        self._active_stream_body = getattr(self.provider, '_last_stream_body', None)
        text_chunks: List[str] = []
        reasoning_chunks: List[str] = []
        # Index-keyed accumulator for streaming tool calls.
        tool_accum: Dict[int, Dict[str, Any]] = {}
        usage: Dict[str, int] = {}
        finish_reason = ""

        try:
            for delta in stream:
                if self._interrupt_event.is_set():
                    break
                if delta.reasoning_content:
                    reasoning_chunks.append(delta.reasoning_content)
                    self._emit("thinking_content", {"text": delta.reasoning_content})
                if delta.content:
                    text_chunks.append(delta.content)
                    self._emit("assistant_text_delta", {"text": delta.content})
                for tc in delta.tool_calls_deltas or []:
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
                    # _parse_stream_chunk already accumulates arguments
                    # correctly (handles both incremental fragments and
                    # full-string-mode providers like Agnes). Just take
                    # the latest cumulative value — do NOT += it again.
                    if "arguments" in fn:
                        cur["function"]["arguments"] = fn["arguments"]
                if delta.finish_reason:
                    finish_reason = delta.finish_reason
                if delta.usage:
                    usage = delta.usage
        finally:
            self._active_stream_body = None

        text = "".join(text_chunks)
        reasoning_content = "".join(reasoning_chunks) if reasoning_chunks else ""
        tool_calls = [tool_accum[i] for i in sorted(tool_accum.keys())]
        # Filter out empty / malformed tool calls (the model occasionally
        # emits a half-formed one when it changes its mind mid-stream).
        tool_calls = [t for t in tool_calls if t.get("function", {}).get("name")]
        self._emit("assistant_text_done", {"text": text, "finish_reason": finish_reason})
        # Emit debug response summary.
        if self.debug_enabled:
            self._emit("llm_response", {
                "model": "",
                "finish_reason": finish_reason,
                "usage": usage,
                "content_preview": text[:500],
                "tool_calls_count": len(tool_calls),
            })
            self.provider.on_request = None
        return text, tool_calls, usage, reasoning_content

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _execute_tool_call(self, tc: Dict[str, Any]) -> ToolResult:
        """Parse one tool call's arguments (a JSON string) and dispatch."""
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        raw_args = fn.get("arguments") or ""
        if not name:
            return ToolResult.failure("tool call missing function name")
        try:
            kwargs = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError as exc:
            return ToolResult.failure(f"bad tool arguments JSON: {exc}")
        if not isinstance(kwargs, dict):
            return ToolResult.failure(f"tool arguments must be a JSON object, got {type(kwargs).__name__}")
        self._emit("tool_call", {"name": name, "args": kwargs})
        # Wire _emit_fn for approval checks in terminal/file tools.
        tool_obj = self.registry.get(name)
        if tool_obj is not None:
            tool_obj._emit_fn = self._emit
            tool_obj._interrupt_event = self._interrupt_event
        # Inject parent agent context for tools that need it (e.g. delegate_task).
        if name == "delegate_task":
            kwargs["parent_agent"] = self
        return self.registry.call(name, **kwargs)

    # ------------------------------------------------------------------
    # History conversion
    # ------------------------------------------------------------------

    def _to_chat_message(self, m: Message) -> ChatMessage:
        # content may be str (plain text) or list (multimodal parts).
        # Preserve empty strings for assistant tool-call turns — converting
        # "" to None causes the serialiser to drop the "content" key, which
        # some providers reject with "content is not set".
        return ChatMessage(
            role=m.role,
            content=m.content,
            name=m.name,
            tool_calls=m.tool_calls,
            tool_call_id=m.tool_call_id,
            reasoning_content=m.reasoning_content,
        )

    # ------------------------------------------------------------------
    # Event sink
    # ------------------------------------------------------------------

    def _emit(self, kind: str, payload: Dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, payload)
        except Exception:  # noqa: BLE001
            # A buggy callback must never break the agent loop.
            logger.exception("agent: on_event(%s) raised", kind)

    # ------------------------------------------------------------------
    # Interrupt
    # ------------------------------------------------------------------

    def interrupt(self, message: str = "") -> None:
        """Interrupt the current turn. Thread-safe.

        Sets the interrupt flag, closes any active HTTP stream to abort
        in-flight reads, and stores the message for the caller.
        """
        self._interrupt_message = message
        self._interrupt_event.set()
        body = self._active_stream_body
        if body is not None:
            try:
                body.close()
            except Exception:  # noqa: BLE001
                pass

    def is_interrupted(self) -> bool:
        """Return True if interrupt() was called."""
        return self._interrupt_event.is_set()

    def _cleanup_orphaned_tool_calls(self) -> None:
        """Remove assistant messages with tool_calls that lack matching tool results.

        This happens when a turn is interrupted during tool execution:
        the assistant message (with tool_calls) is persisted, but the
        corresponding tool results are not.  On the next turn the API
        would reject the malformed conversation, so we clean up here.
        """
        msgs = self.state.list_messages(self.session_id)
        if not msgs:
            return
        # Scan from the end: find the last assistant message with tool_calls.
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if m.role == "assistant" and m.tool_calls:
                # Collect the tool_call_ids declared in this message.
                call_ids = {tc.get("id") for tc in m.tool_calls if tc.get("id")}
                if not call_ids:
                    break
                # Check if all have matching tool results AFTER this message.
                result_ids = {
                    msg.tool_call_id
                    for msg in msgs[i + 1:]
                    if msg.role == "tool" and msg.tool_call_id
                }
                missing = call_ids - result_ids
                if missing:
                    # Delete orphaned assistant message and any partial
                    # tool results that follow it.
                    to_delete = [m.id]
                    for msg in msgs[i + 1:]:
                        if msg.role == "tool" and msg.tool_call_id in call_ids:
                            to_delete.append(msg.id)
                    for mid in to_delete:
                        if mid is not None:
                            self.state.delete_message(mid)
                    logger.info(
                        "cleaned up %d orphaned message(s) for session %s",
                        len(to_delete), self.session_id,
                    )
                break  # only check the last assistant(tool_calls) message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate_for_log(data: Any) -> Any:
    """Best-effort truncation of a tool result for the UI event log.

    Strings get a length cap; everything else is repr'd as-is.
    The cap is generous (8 K) so that expanded tool results in the
    web UI show meaningful content during live streaming — the full
    result is always persisted to SQLite separately.
    """
    if isinstance(data, str) and len(data) > 8000:
        return data[:8000] + "...(truncated)"
    return data


def run_agent_turn(
    cfg: Dict[str, Any],
    profile: ProviderProfile,
    registry: ToolRegistry,
    state: StateStore,
    user_message: str,
    *,
    session_id: Optional[str] = None,
    on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> AgentTurnResult:
    """Convenience wrapper for one-shot turn execution.

    Builds an :class:`AIAgent` and runs a single user turn. Use this from
    the CLI's ``chat`` command; the web frontend builds its own agent
    per request to keep state isolated.
    """
    agent = AIAgent(
        cfg=cfg, profile=profile, registry=registry, state=state,
        session_id=session_id, on_event=on_event,
    )
    return agent.run_turn(user_message)
