"""
api/crowd_processing.py
------------------------
Background AI assessment and spatial clustering for field reports.
Called from crowd.py and crowd_simulate.py.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

_CLUSTER_KM    = 1.0
_CLUSTER_MIN   = 5
_CLUSTER_HOURS = 24


def bg_assess_and_cluster(app, report_id: int) -> None:
    """Background thread: placeholder (intensity assessment removed)."""


def _maybe_generate_theme(app, trigger_report) -> None:
    from db.connection import db
    from db.models import FieldReport, Theme
    from agents.crowd_agent import generate_theme

    cutoff = datetime.utcnow() - timedelta(hours=_CLUSTER_HOURS)
    candidates = (
        db.session.query(FieldReport)
        .filter(
            FieldReport.event_id   == trigger_report.event_id,
            FieldReport.created_at >= cutoff,
            FieldReport.id         != trigger_report.id,
        )
        .all()
    )

    nearby = [r for r in candidates
              if _haversine_km(trigger_report.lat, trigger_report.lon, r.lat, r.lon) <= _CLUSTER_KM]

    if len(nearby) + 1 < _CLUSTER_MIN:
        return

    cluster = nearby + [trigger_report]
    existing_ids = {r.theme_id for r in cluster if r.theme_id is not None}
    if len(existing_ids) == 1 and None not in existing_ids:
        return

    report_dicts = [
        {"post_type": r.post_type, "description": r.description}
        for r in cluster
    ]
    result     = generate_theme(report_dicts)
    center_lat = sum(r.lat for r in cluster) / len(cluster)
    center_lon = sum(r.lon for r in cluster) / len(cluster)

    theme = Theme(
        event_id     = trigger_report.event_id,
        center_lat   = center_lat,
        center_lon   = center_lon,
        radius_m     = _CLUSTER_KM * 1000,
        title        = result.get("title", "Field Reports Cluster"),
        summary      = result.get("summary", ""),
        generated_at = datetime.utcnow(),
    )
    db.session.add(theme)
    db.session.flush()
    for r in cluster:
        r.theme_id = theme.id
    db.session.commit()


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))
