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
from agents.prompts import CHAT_AGENT_SYSTEM, THERMAL_CHAT_AGENT_SYSTEM


def run_chat_agent(
    summary: str,
    message: str,
    history: list[dict],
    road_summary: list[dict] | None = None,
    analysis_mode: str | None = None,
) -> Generator[str, None, None]:
    """Stream a chat response using the pre-computed summary as context.

    Args:
        summary:      Pre-computed situation overview from ai_summary.json.
        message:      Current user message.
        history:      Prior turns as [{"role": "user"|"assistant", "content": "..."}].
        road_summary: Road status list from fire_context.json (optional).
        analysis_mode: Selects the thermal-monitoring or wildfire assistant prompt.

    Yields:
        Text chunks.
    """
    import json as _json

    thermal_mode = analysis_mode == "thermal_monitoring"
    context_label = (
        "Current industrial thermal intelligence report"
        if thermal_mode else "Current situational report"
    )
    context_parts = [f"{context_label}:\n{summary}"] if summary else []
    if road_summary:
        context_parts.append(f"Road status:\n{_json.dumps(road_summary, ensure_ascii=False)}")

    system = THERMAL_CHAT_AGENT_SYSTEM if thermal_mode else CHAT_AGENT_SYSTEM
    if context_parts:
        system += "\n\n" + "\n\n".join(context_parts)

    messages = [*history, {"role": "user", "content": message}]
    try:
        yield from stream_llm(system, messages)
    except Exception as e:
        yield f"\n\n[Error: {e}]"
