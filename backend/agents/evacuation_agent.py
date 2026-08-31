"""
agents/evacuation_agent.py
--------------------------
Analyses road network status and recommends evacuation priorities.
"""

from __future__ import annotations

import json
import re

from agents._client import call_llm
from agents.prompts import EVACUATION_AGENT_SYSTEM


def run_evacuation_agent(
    fire_context: dict,
    road_summary: list,
    landmarks: list | None = None,
    *,
    roads_available: bool = True,
) -> dict:
    """Return evacuation analysis dict with top_route and alternative_route.

    Returns keys: top_route, alternative_route, road_warnings
    """
    if not roads_available:
        return {
            "data_available": False,
            "reason": "Road-network analysis is not configured for this region.",
            "top_route": None,
            "alternative_route": None,
            "road_warnings": [],
        }
    if not road_summary:
        return {
            "data_available": True,
            "reason": None,
            "top_route": None,
            "alternative_route": None,
            "road_warnings": [],
            "status_summary": "No affected major-road segments were identified.",
        }

    wind_forecast = fire_context.get("wind_forecast") or []
    parts = [
        "ROAD_STATUS:",
        json.dumps(road_summary, separators=(',', ':')),
        "",
        "WIND_FORECAST:",
        json.dumps(wind_forecast, separators=(',', ':')),
    ]
    if landmarks:
        lm_compact = [{"name": lm["name"], "type": lm.get("type", "")} for lm in landmarks]
        parts += ["", "LANDMARKS:", json.dumps(lm_compact, separators=(',', ':'))]

    text = call_llm(EVACUATION_AGENT_SYSTEM, "\n".join(parts))
    try:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            data = json.loads(m.group())
            data["data_available"] = True
            data["reason"] = None
            return data
    except Exception:
        pass
    # Fallback
    return {
        "data_available": True,
        "reason": None,
        "top_route": {"path": [], "status": "", "window": "", "reasoning": text},
        "alternative_route": {"path": [], "status": "", "window": "", "reasoning": ""},
        "road_warnings": [],
    }
