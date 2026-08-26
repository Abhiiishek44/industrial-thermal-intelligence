"""
agents/impact_agent.py
----------------------
Summarises human population impact.
"""

from __future__ import annotations

import json
import re

from agents._client import call_llm
from agents.prompts import IMPACT_AGENT_SYSTEM


def run_impact_agent(fire_context: dict, population: dict) -> dict:
    """Return impact analysis dict for the current timestep.

    Returns keys: population, communities_affected, worsening_factors, impact_summary
    """
    pop = population or {}
    lines = [
        "Population exposure:",
        f"  Within fire perimeter:  {pop.get('affected_population') or 0:,}",
        f"  At risk +3h forecast:   {pop.get('at_risk_3h') or 0:,}",
        f"  At risk +6h forecast:   {pop.get('at_risk_6h') or 0:,}",
        f"  At risk +12h forecast:  {pop.get('at_risk_12h') or 0:,}",
        "",
        "Fire situation context (JSON):",
        json.dumps(fire_context, separators=(',', ':')),
    ]
    text = call_llm(IMPACT_AGENT_SYSTEM, "\n".join(lines))
    try:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    # Fallback
    return {
        "population": {
            "within_perimeter": pop.get("affected_population") or 0,
            "at_risk_3h":       pop.get("at_risk_3h") or 0,
            "at_risk_6h":       pop.get("at_risk_6h") or 0,
            "at_risk_12h":      pop.get("at_risk_12h") or 0,
        },
        "communities_affected": [],
        "worsening_factors": [],
        "impact_summary": text,
    }
