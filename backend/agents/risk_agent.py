"""
agents/risk_agent.py
--------------------
Analyses fire behaviour data and produces a risk analysis report.
"""

from __future__ import annotations

import json
import re

from agents._client import call_llm
from agents.prompts import RISK_AGENT_SYSTEM


def run_risk_agent(fire_context: dict) -> dict:
    """Return risk analysis dict for the current timestep.

    Returns keys: fire_behaviour, growth_trajectory, weather_drivers,
                  risk_factors, overall_assessment
    """
    user_msg = (
        "Fire situation context (JSON):\n"
        f"{json.dumps(fire_context, separators=(',', ':'))}"
    )
    text = call_llm(RISK_AGENT_SYSTEM, user_msg)
    try:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    # Fallback: wrap raw text
    return {
        "fire_behaviour": text,
        "growth_trajectory": "",
        "weather_drivers": "",
        "risk_factors": [],
        "overall_assessment": "",
    }
