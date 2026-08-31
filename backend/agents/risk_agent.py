"""
agents/risk_agent.py
--------------------
Analyses fire behaviour data and produces a risk analysis report.
"""

from __future__ import annotations

import json
import re

from agents._client import call_llm
from agents.prompts import RISK_AGENT_SYSTEM, THERMAL_ANALYSIS_SYSTEM


def run_risk_agent(fire_context: dict) -> dict:
    """Return risk analysis dict for the current timestep.

    Returns keys: fire_behaviour, growth_trajectory, weather_drivers,
                  risk_factors, overall_assessment
    """
    thermal_mode = fire_context.get("analysis_mode") == "thermal_monitoring"
    user_msg = (
        ("Thermal monitoring evidence (JSON):\n" if thermal_mode else "Fire situation context (JSON):\n")
        + json.dumps(fire_context, separators=(",", ":"))
    )
    text = call_llm(THERMAL_ANALYSIS_SYSTEM if thermal_mode else RISK_AGENT_SYSTEM, user_msg)
    try:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    if thermal_mode:
        return {
            "detection_summary": text,
            "source_assessment": "",
            "persistence_assessment": "",
            "context_factors": [],
            "uncertainties": ["The model response was not valid structured JSON."],
            "recommended_checks": [],
        }
    # Wildfire fallback: wrap raw text
    return {
        "fire_behaviour": text,
        "growth_trajectory": "",
        "weather_drivers": "",
        "risk_factors": [],
        "overall_assessment": "",
    }
