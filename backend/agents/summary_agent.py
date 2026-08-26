"""
agents/summary_agent.py
-----------------------
Synthesises three specialist reports into a structured executive briefing.
"""

from __future__ import annotations

import json
import re

from agents._client import call_llm
from agents.prompts import SUMMARY_AGENT_SYSTEM


def run_summary_agent(
    risk_analysis: dict,
    impact_analysis: dict,
    evacuation_analysis: dict,
    crowd_analysis: dict | None = None,
) -> dict:
    """Return structured dict: {risk_level, key_points, briefing}.

    crowd_analysis: if provided (non-None), included as a fourth input section.
    """
    parts = [
        "RISK ANALYSIS:\n" + json.dumps(risk_analysis, ensure_ascii=False),
        "IMPACT ANALYSIS:\n" + json.dumps(impact_analysis, ensure_ascii=False),
        "EVACUATION ANALYSIS:\n" + json.dumps(evacuation_analysis, ensure_ascii=False),
    ]
    if crowd_analysis is not None:
        parts.append("CROWD INTELLIGENCE:\n" + json.dumps(crowd_analysis, ensure_ascii=False))
    user_msg = "\n\n".join(parts)
    text = call_llm(SUMMARY_AGENT_SYSTEM, user_msg)

    # Extract JSON from the response (handles accidental markdown fences)
    try:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            data = json.loads(m.group())
            situation   = str(data.get("situation", ""))
            key_risks   = str(data.get("key_risks", ""))
            imm_actions = str(data.get("immediate_actions", ""))
            # LLM may still return old `briefing` field — distribute it
            briefing = str(data.get("briefing", ""))
            if briefing and not (situation or key_risks or imm_actions):
                situation = briefing
            return {
                "risk_level":        str(data.get("risk_level", "Unknown")),
                "key_points":        list(data.get("key_points", [])),
                "situation":         situation,
                "key_risks":         key_risks,
                "immediate_actions": imm_actions,
            }
    except Exception:
        pass

    # Fallback
    return {
        "risk_level": "Unknown", "key_points": [],
        "situation": text, "key_risks": "", "immediate_actions": "",
    }
