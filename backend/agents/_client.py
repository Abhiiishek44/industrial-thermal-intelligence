"""
agents/_client.py
-----------------
Provider-agnostic LLM client.

Set LLM_PROVIDER=claude (default) or LLM_PROVIDER=gemini in your .env.

Claude:  requires ANTHROPIC_API_KEY
Gemini:  requires GEMINI_API_KEY
"""

from __future__ import annotations

import os
from typing import Generator

_PROVIDER = os.environ.get("LLM_PROVIDER", "claude").lower()

# ── Claude ─────────────────────────────────────────────────────────────────────

_CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
_CLAUDE_MAX_TOKENS = 1024


def _claude_call(system: str, user_msg: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=_CLAUDE_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return response.content[0].text


def _claude_stream(system: str, messages: list[dict]) -> Generator[str, None, None]:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    with client.messages.stream(
        model=_CLAUDE_MODEL,
        max_tokens=_CLAUDE_MAX_TOKENS,
        system=system,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            yield text


# ── Gemini ──────────────────────────────────────────────────────────────────────

_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")


def _gemini_call(system: str, user_msg: str) -> str:
    import requests
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent"
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
    }
    res = requests.post(url, params={"key": os.environ["GEMINI_API_KEY"]}, json=body, timeout=120)
    if not res.ok:
        import logging
        logging.getLogger(__name__).error("Gemini error %s: %s", res.status_code, res.text[:500])
        raise RuntimeError(f"Gemini API error: HTTP {res.status_code}")
    return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def _gemini_stream(system: str, messages: list[dict]) -> Generator[str, None, None]:
    """Stream a multi-turn Gemini chat via REST SSE.

    Claude history format  → {"role": "user"|"assistant", "content": "..."}
    Gemini history format  → {"role": "user"|"model",     "parts": [{"text": "..."}]}
    """
    import requests, json as _json

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:streamGenerateContent"
    contents = [
        {
            "role": "model" if m["role"] == "assistant" else "user",
            "parts": [{"text": m["content"]}],
        }
        for m in messages
    ]
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": contents,
    }
    with requests.post(
        url,
        params={"key": os.environ["GEMINI_API_KEY"], "alt": "sse"},
        json=body,
        stream=True,
        timeout=120,
    ) as res:
        if not res.ok:
            raise RuntimeError(f"Gemini API error: HTTP {res.status_code}")
        for raw in res.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = _json.loads(payload)
                text = chunk["candidates"][0]["content"]["parts"][0]["text"]
                if text:
                    yield text
            except (KeyError, IndexError, _json.JSONDecodeError):
                continue


# ── Public interface ────────────────────────────────────────────────────────────

def call_llm(system: str, user_msg: str) -> str:
    """Single-turn synchronous call. Returns the response text."""
    if _PROVIDER == "gemini":
        return _gemini_call(system, user_msg)
    return _claude_call(system, user_msg)


def stream_llm(system: str, messages: list[dict]) -> Generator[str, None, None]:
    """Multi-turn streaming call. Yields text chunks."""
    if _PROVIDER == "gemini":
        yield from _gemini_stream(system, messages)
    else:
        yield from _claude_stream(system, messages)
