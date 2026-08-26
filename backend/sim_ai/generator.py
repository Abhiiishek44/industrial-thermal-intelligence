"""
sim_ai/generator.py
--------------------
LLM-powered field report generator.

Public API:
    generate_reports(bbox, n, hints, ctx) -> list[dict]

Each returned dict:
    {post_type, description, lat, lon, created_at (ISO), comments: [{content, created_at}]}
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agents._client import call_llm
from sim_ai.prompt import SIMULATE_REPORTS_SYSTEM
from sim_ai.geospatial import GisContext


def generate_reports(
    bbox:  tuple[float, float, float, float],
    n:     int,
    hints: str        = "",
    ctx:   GisContext | None = None,
) -> list[dict]:
    """Call the LLM and return a list of simulated field report dicts.

    Args:
        bbox:  (lon_min, lat_min, lon_max, lat_max)
        n:     number of reports to generate (1–20)
        hints: optional scenario description
        ctx:   GisContext with perimeter_pts, road_pts, landmark_pts, slot_time

    Returns:
        List of dicts: {post_type, description, lat, lon,
                        created_at (ISO str), comments: [{content, created_at}]}
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    ctx = ctx or GisContext()

    # ── Parse slot_time ───────────────────────────────────────────────────────
    slot_dt: datetime | None = None
    if ctx.slot_time:
        try:
            slot_dt = datetime.fromisoformat(ctx.slot_time)
            if slot_dt.tzinfo is None:
                slot_dt = slot_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            slot_dt = None
    if slot_dt is None:
        slot_dt = datetime.now(tz=timezone.utc)

    # ── Build prompt user message ─────────────────────────────────────────────
    lines = [
        f"Bounding box: lon_min={lon_min:.4f}, lat_min={lat_min:.4f}, "
        f"lon_max={lon_max:.4f}, lat_max={lat_max:.4f}",
        f"Number of reports: {n}",
        f"SLOT_TIME: {ctx.slot_time or slot_dt.isoformat()}",
    ]

    if ctx.perimeter_pts:
        sample = ctx.perimeter_pts[::max(1, len(ctx.perimeter_pts) // 20)][:20]
        lines.append(
            "PERIMETER_POINTS (lat,lon): "
            + "; ".join(f"{p[0]:.5f},{p[1]:.5f}" for p in sample)
        )
    else:
        lines.append("PERIMETER_POINTS: none — place fire_reports near bbox centre")

    if ctx.road_pts:
        sample = ctx.road_pts[::max(1, len(ctx.road_pts) // 30)][:30]
        lines.append(
            "ROAD_POINTS (lat,lon): "
            + "; ".join(f"{p[0]:.5f},{p[1]:.5f}" for p in sample)
        )
    else:
        lines.append("ROAD_POINTS: none — use LANDMARK_POINTS for info/help reports")

    if ctx.landmark_pts:
        sample = ctx.landmark_pts[:20]
        lines.append(
            "LANDMARK_POINTS (name|lat,lon|type): "
            + "; ".join(f"{lm['name']}|{lm['lat']:.5f},{lm['lon']:.5f}|{lm['type']}" for lm in sample)
        )
    else:
        lines.append("LANDMARK_POINTS: none — use ROAD_POINTS for request_help/offer_help")

    if hints:
        lines.append(f"Scenario hint: {hints}")

    # ── Call LLM (batch if n > 5 to avoid timeouts) ──────────────────────────
    _BATCH = 5
    if n <= _BATCH:
        batches = [n]
    else:
        full, rem = divmod(n, _BATCH)
        batches = [_BATCH] * full + ([rem] if rem else [])

    reports: list = []
    base_lines = lines.copy()
    for batch_n in batches:
        batch_lines = base_lines.copy()
        batch_lines[1] = f"Number of reports: {batch_n}"
        raw = call_llm(SIMULATE_REPORTS_SYSTEM, "\n".join(batch_lines)).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                reports.extend(parsed)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass  # partial failure — skip this batch

    if not reports:
        return []

    # ── Post-process ──────────────────────────────────────────────────────────
    result = []
    for r in reports[:n]:
        # Clamp coordinates
        lat = max(lat_min, min(lat_max, float(r.get("lat", (lat_min + lat_max) / 2))))
        lon = max(lon_min, min(lon_max, float(r.get("lon", (lon_min + lon_max) / 2))))

        # Compute absolute created_at from hours_ago
        hours_ago  = max(0.2, min(10.0, float(r.get("hours_ago", 5.0))))
        report_dt  = slot_dt - timedelta(hours=hours_ago)

        # Process comments
        comments = []
        for c in (r.get("comments") or [])[:4]:
            c_hours_ago = max(0.0, min(hours_ago - 0.05, float(c.get("hours_ago", hours_ago * 0.5))))
            comment_dt  = slot_dt - timedelta(hours=c_hours_ago)
            comments.append({
                "content":    str(c.get("content", "")).strip(),
                "created_at": comment_dt.isoformat(),
            })

        result.append({
            "post_type":    r.get("post_type", "info"),
            "description":  str(r.get("description", "")).strip(),
            "lat":          lat,
            "lon":          lon,
            "created_at":   report_dt.isoformat(),
            "comments":     comments,
        })

    return result
