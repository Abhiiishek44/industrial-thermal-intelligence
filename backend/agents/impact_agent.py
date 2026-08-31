"""
agents/impact_agent.py
----------------------
Summarises human population impact.
"""

from __future__ import annotations

import json
import re

from agents._client import call_llm
from agents.prompts import IMPACT_NARRATIVE_SYSTEM


_WILDFIRE_FIELDS = (
    "affected_population", "at_risk_3h", "at_risk_6h", "at_risk_12h",
)
_THERMAL_FIELDS = ("within_1km", "within_3km", "within_5km")


def _authoritative_population(fire_context: dict, population: dict) -> dict:
    mode = fire_context.get("analysis_mode", "wildfire_prediction")
    thermal = mode == "thermal_monitoring"
    fields = _THERMAL_FIELDS if thermal else _WILDFIRE_FIELDS
    available = population.get("data_available")
    if available is None:
        available = any(population.get(field) is not None for field in fields)
    return {
        **{field: population.get(field) if available else None for field in fields},
        "data_available": bool(available),
        "exposure_mode": population.get(
            "exposure_mode", "proximity_buffers" if thermal else "forecast_zones"
        ),
        "reason": population.get("reason") if not available else None,
        "source": population.get("source") if available else None,
    }


def run_impact_agent(fire_context: dict, population: dict) -> dict:
    """Return impact analysis dict for the current timestep.

    Returns keys: population, communities_affected, worsening_factors, impact_summary
    """
    authoritative = _authoritative_population(fire_context, population or {})
    if not authoritative["data_available"]:
        return {
            "population": authoritative,
            "communities_affected": [],
            "worsening_factors": [],
            "impact_summary": (
                authoritative.get("reason")
                or "Population exposure data is unavailable for this observation."
            ),
        }

    evidence = {
        "population": authoritative,
        "region": fire_context.get("region"),
        "landmarks": fire_context.get("landmarks", []),
        "analysis_mode": fire_context.get("analysis_mode"),
        "observation_time": fire_context.get("observation_time"),
    }
    text = call_llm(
        IMPACT_NARRATIVE_SYSTEM,
        "Impact evidence (JSON):\n" + json.dumps(evidence, separators=(",", ":")),
    )
    try:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            data = json.loads(m.group())
            data["population"] = authoritative
            data["communities_affected"] = list(data.get("communities_affected", []))
            data["worsening_factors"] = list(data.get("worsening_factors", []))
            data["impact_summary"] = str(data.get("impact_summary", ""))
            return data
    except Exception:
        pass
    # The narrative may fail, but authoritative numbers still survive unchanged.
    return {
        "population": authoritative,
        "communities_affected": [],
        "worsening_factors": [],
        "impact_summary": text,
    }
