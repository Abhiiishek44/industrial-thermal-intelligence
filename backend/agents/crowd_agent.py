"""
agents/crowd_agent.py
---------------------
AI assessment for individual field reports and report clusters.
"""
from __future__ import annotations

import json
import re

from agents._client import call_llm
from agents.prompts import CROWD_INTENSITY_SYSTEM, CROWD_THEME_SYSTEM, CROWD_ANALYSIS_SYSTEM


_EMPTY_CROWD = {
    "report_counts": {"fire_report": 0, "info": 0, "request_help": 0, "offer_help": 0, "need_help": 0, "total": 0},
    "fire_observations": "No crowd reports available for this timestep.",
    "urgent_help": [],
    "situational_info": "",
    "notable_patterns": "",
}


def run_crowd_analysis(reports: list[dict]) -> dict:
    """Return crowd intelligence analysis dict for the summary agent.

    Each report dict: {post_type, description, lat, lon, created_at}
    Returns empty structure dict if reports is empty.
    """
    if not reports:
        return dict(_EMPTY_CROWD)

    # Build structured summary for the prompt
    counts: dict[str, int] = {}
    lines = []

    for r in reports:
        pt = r.get("post_type", "unknown")
        counts[pt] = counts.get(pt, 0) + 1
        line = f"[{pt}]"
        if r.get("lat") and r.get("lon"):
            line += f" @ ({r['lat']:.4f}, {r['lon']:.4f})"
        if r.get("description"):
            line += f": {r['description'][:200]}"
        lines.append(line)

    count_str = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))

    user_msg = (
        f"Total reports: {len(reports)} ({count_str})\n\n"
        "REPORTS:\n" + "\n".join(lines)
    )
    text = call_llm(CROWD_ANALYSIS_SYSTEM, user_msg)
    try:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    # Fallback
    return {
        "report_counts": {**{k: counts.get(k, 0) for k in ("fire_report", "info", "request_help", "offer_help", "need_help")}, "total": len(reports)},
        "fire_observations": text,
        "urgent_help": [],
        "situational_info": "",
        "notable_patterns": "",
    }


def assess_intensity(post_type: str, description: str | None, bearing: float | None = None) -> str:
    """Return 'low' | 'mid' | 'high' for a single field report."""
    parts = [f"Post type: {post_type}"]
    if description:
        parts.append(f"Description: {description}")
    if bearing is not None:
        parts.append(f"Camera bearing: {bearing:.1f}\u00b0")
    user_msg = "\n".join(parts)

    result = call_llm(CROWD_INTENSITY_SYSTEM, user_msg).strip().lower()
    if result in ("low", "mid", "high"):
        return result
    # Flexible fallback for verbose responses
    if "high" in result or "critical" in result or "extreme" in result:
        return "high"
    if "mid" in result or "moderate" in result or "medium" in result:
        return "mid"
    return "low"



def generate_theme(reports: list[dict]) -> dict:
    """Return {title, summary} for a cluster of field reports."""
    lines = []
    for i, r in enumerate(reports, 1):
        line = f"{i}. [{r.get('post_type', 'unknown')}]"
        if r.get("description"):
            line += f" {r['description']}"
        lines.append(line)
    user_msg = "\n".join(lines)

    raw = call_llm(CROWD_THEME_SYSTEM, user_msg).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {"title": "Field Reports Cluster", "summary": raw[:300]}
