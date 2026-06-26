"""Thinking/reasoning content handling for multi-turn conversations.

This module provides utilities for managing model thinking/reasoning content
across multiple conversation turns, similar to the reference implementation
in hermes-agent.

Key functions:
- copy_reasoning_content_for_api: Copy reasoning fields onto API messages
- drop_thinking_only_and_merge_users: Remove thinking-only assistant turns
- is_thinking_only_assistant: Check if a message is thinking-only
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..providers.openai_compat import ChatMessage

logger = logging.getLogger(__name__)


def is_thinking_only_assistant(msg: ChatMessage) -> bool:
    """Return True if ``msg`` is an assistant turn whose only payload is reasoning.

    "Thinking-only" means the model emitted reasoning but no visible text
    and no tool_calls. When sent back to providers that convert reasoning
    into thinking blocks (native Anthropic, OpenRouter Anthropic, etc.),
    the resulting message has only thinking blocks — which Anthropic rejects
    with HTTP 400 "The final block in an assistant message cannot be `thinking`."
    """
    if msg.role != "assistant":
        return False
    
    if msg.tool_calls:
        return False
    
    # Does it have any actual output?
    content = msg.content
    if isinstance(content, str):
        if content.strip():
            return False
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                if block:  # non-empty non-dict string etc.
                    return False
                continue
            btype = block.get("type")
            if btype in {"thinking", "redacted_thinking"}:
                continue
            if btype == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    return False
                continue
            # tool_use, image, document, etc. — real payload
            return False
    elif content is not None and content != "":
        return False
    
    # Content is empty-ish. Is there reasoning to make it thinking-only?
    reasoning = msg.reasoning_content
    if isinstance(reasoning, str) and reasoning.strip():
        return True
    
    return False


def copy_reasoning_content_for_api(
    source_msg: ChatMessage,
    api_msg: Dict[str, Any],
    needs_thinking_pad: bool = False,
) -> None:
    """Copy provider-facing reasoning fields onto an API replay message.

    This ensures multi-turn reasoning context is preserved for providers
    that require reasoning_content to be passed back (DeepSeek, Kimi, MiMo).

    The function handles three scenarios:
    1. ``reasoning_content`` is already set on the source — preserve it.
    2. Provider requires a pad — inject a single space when missing.
    3. Provider does not require it — strip any residual field.

    Args:
        source_msg: The original ChatMessage with reasoning_content
        api_msg: The API message dict to update (mutated in place)
        needs_thinking_pad: Whether the current provider requires reasoning_content
    """
    if source_msg.role != "assistant":
        return

    # 1. Explicit reasoning_content already set — preserve it verbatim
    # (includes DeepSeek/Kimi's own space-placeholder written at creation
    # time, and any valid reasoning content from the same provider).
    existing = source_msg.reasoning_content
    if isinstance(existing, str):
        if existing == "" and needs_thinking_pad:
            api_msg["reasoning_content"] = " "
        else:
            api_msg["reasoning_content"] = existing
        return

    # 2. Some providers (Moonshot, Novita) use a ``reasoning`` field
    # internally instead of ``reasoning_content``. Promote it.
    # Note: HermesLite's ChatMessage does not have a ``reasoning`` field,
    # but some OpenAI-compat providers may set it on the raw JSON response
    # that gets parsed into provider_specific_fields. We check the api_msg
    # dict directly in case the caller populated it from such a source.
    normalized_reasoning = api_msg.get("reasoning")
    if isinstance(normalized_reasoning, str) and normalized_reasoning:
        api_msg["reasoning_content"] = normalized_reasoning
        api_msg.pop("reasoning", None)
        return

    # 3. Provider requires thinking pad — inject a single space to satisfy
    # the API without leaking another provider's chain of thought.
    if needs_thinking_pad:
        api_msg["reasoning_content"] = " "
        return

    # 4. reasoning_content was present but not a string (e.g. None after
    # context compaction). Don't pass null to the API.
    api_msg.pop("reasoning_content", None)


def drop_thinking_only_and_merge_users(
    messages: List[ChatMessage],
) -> List[ChatMessage]:
    """Drop thinking-only assistant turns; merge any adjacent user messages left behind.

    Runs on the per-call ``api_messages`` copy only. The stored
    conversation history is never mutated, so the user still sees
    the thinking block in the transcript and session persistence
    keeps the full trace.

    Why drop-and-merge rather than inject stub text:
    - Fabricating ``"."`` / ``"(continued)"`` text lies in the history
      and makes future turns see model output the model didn't emit.
    - Dropping the turn preserves honesty; merging adjacent user messages
      preserves the provider's role-alternation invariant.
    """
    if not messages:
        return messages

    # Pass 1: drop thinking-only assistant turns.
    kept = [m for m in messages if not is_thinking_only_assistant(m)]
    dropped = len(messages) - len(kept)
    if dropped == 0:
        return messages

    # Pass 2: merge any newly-adjacent user messages.
    merged: List[ChatMessage] = []
    merges = 0
    for m in kept:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.role == "user"
            and m.role == "user"
        ):
            prev_content = prev.content
            cur_content = m.content
            # Work on a copy of ``prev`` so the caller's input dicts are
            # never mutated.
            prev_copy = ChatMessage(
                role=prev.role,
                content=prev.content,
                name=prev.name,
                tool_calls=prev.tool_calls,
                tool_call_id=prev.tool_call_id,
                reasoning_content=prev.reasoning_content,
            )
            # Only string-content merge is meaningful for role-alternation
            # purposes. If either side is a list (multimodal), append as a
            # separate block rather than collapsing.
            if isinstance(prev_content, str) and isinstance(cur_content, str):
                sep = "\n\n" if prev_content and cur_content else ""
                prev_copy.content = prev_content + sep + cur_content
            elif isinstance(prev_content, list) and isinstance(cur_content, list):
                prev_copy.content = list(prev_content) + list(cur_content)
            elif isinstance(prev_content, list) and isinstance(cur_content, str):
                if cur_content:
                    prev_copy.content = list(prev_content) + [
                        {"type": "text", "text": cur_content}
                    ]
                else:
                    prev_copy.content = list(prev_content)
            elif isinstance(prev_content, str) and isinstance(cur_content, list):
                new_blocks: List[Dict[str, Any]] = []
                if prev_content:
                    new_blocks.append({"type": "text", "text": prev_content})
                new_blocks.extend(cur_content)
                prev_copy.content = new_blocks
            else:
                # Unknown content shape — fall back to appending separately
                # (violates alternation, but safer than raising in a hot path).
                merged.append(m)
                continue
            merged[-1] = prev_copy
            merges += 1
        else:
            merged.append(m)

    logger.debug(
        "Pre-call sanitizer: dropped %d thinking-only assistant turn(s), "
        "merged %d adjacent user message(s)",
        dropped,
        merges,
    )
    return merged