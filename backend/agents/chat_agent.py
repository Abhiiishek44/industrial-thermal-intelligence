"""
agents/chat_agent.py
--------------------
Stateless streaming chat agent. Each call receives the full conversation
history and the pre-computed summary as system context.

Used by: POST /api/events/:id/chat
"""

from __future__ import annotations

from typing import Generator

from agents._client import stream_llm
from agents.prompts import CHAT_AGENT_SYSTEM


def run_chat_agent(
    summary: str,
    message: str,
    history: list[dict],
    road_summary: list[dict] | None = None,
) -> Generator[str, None, None]:
    """Stream a chat response using the pre-computed summary as context.

    Args:
        summary:      Pre-computed situation overview from ai_summary.json.
        message:      Current user message.
        history:      Prior turns as [{"role": "user"|"assistant", "content": "..."}].
        road_summary: Road status list from fire_context.json (optional).

    Yields:
        Text chunks.
    """
    import json as _json

    context_parts = [f"Current situational report:\n{summary}"] if summary else []
    if road_summary:
        context_parts.append(f"Road status:\n{_json.dumps(road_summary, ensure_ascii=False)}")

    system = CHAT_AGENT_SYSTEM
    if context_parts:
        system += "\n\n" + "\n\n".join(context_parts)

    messages = [*history, {"role": "user", "content": message}]
    try:
        yield from stream_llm(system, messages)
    except Exception as e:
        yield f"\n\n[Error: {e}]"
