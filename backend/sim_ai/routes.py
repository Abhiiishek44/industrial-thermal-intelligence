"""
sim_ai/routes.py
-----------------
POST /api/events/<event_id>/field-reports/simulate

Registered on crowd_bp (imported at the bottom of api/crowd.py).
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request
from utils.auth_middleware import token_required

from api.crowd import crowd_bp
from api.crowd_processing import bg_assess_and_cluster as _bg_assess_and_cluster
from utils.auth_middleware import admin_required
from sim_ai.geospatial import extract_gis_context
from sim_ai.generator import generate_reports

# Blueprint exported so __init__.py can re-export it
simulate_bp = crowd_bp


@crowd_bp.route("/<int:event_id>/field-reports/simulate", methods=["POST"])
@admin_required
def simulate_field_reports(event_id: int):
    """AI-generate N GIS-informed fake field reports.

    Body:
        {
            "n":     5,                          # 1–20
            "hints": "heavy smoke near highway", # optional scenario
            "ts_id": 245                         # optional: use this timestep's GIS data
        }

    Returns: list of created field report objects (same schema as GET /field-reports)
    """
    from db.connection import db
    from db.models import EventTimestep, FieldReport, FireEvent
    from shapely import wkb
    from utils.background import run_in_background

    event = FireEvent.query.get(event_id)
    if not event:
        return jsonify({"error": "event not found"}), 404

    data         = request.get_json(force=True) or {}
    n            = max(1, min(int(data.get("n", 5)), 20))
    hints        = str(data.get("hints", "")).strip()
    ts_id        = data.get("ts_id")
    virtual_time = data.get("virtual_time")  # ISO string from replay clock

    geom = wkb.loads(bytes(event.bbox.data))
    bbox = geom.bounds   # (lon_min, lat_min, lon_max, lat_max)

    # ── Resolve timestep for GIS context ─────────────────────────────────────
    ts_row = None
    if ts_id:
        ts_row = EventTimestep.query.filter_by(id=ts_id, event_id=event_id).first()
    if not ts_row:
        # prediction_status lives in STATUS.json, not a DB column — scan filesystem
        from pathlib import Path
        import json as _json
        _data_dir = Path(__file__).resolve().parents[2] / "data"
        candidates = (
            EventTimestep.query
            .filter_by(event_id=event_id)
            .order_by(EventTimestep.slot_time.desc())
            .all()
        )
        for _ts in candidates:
            import pandas as pd
            _ts_str  = pd.Timestamp(_ts.slot_time).strftime("%Y-%m-%dT%H%M")
            _ml_dir  = _data_dir / "events" / f"{event.year}_{event_id:04d}" / "timesteps" / _ts_str / "prediction" / "ML"
            _status_file = _ml_dir / "STATUS.json"
            # Accept STATUS.json "done" or fire_context.json sentinel (no STATUS.json)
            _done = False
            if _status_file.exists():
                try:
                    _done = _json.loads(_status_file.read_text()).get("status") == "done"
                except Exception:
                    pass
            elif (_ml_dir / "fire_context.json").exists():
                _done = True
            if _done:
                ts_row = _ts
                break

    ctx = extract_gis_context(event, ts_row)
    # Override slot_time with the replay clock position so created_at timestamps
    # are distributed within 24h before the virtual time, not the DB slot_time.
    if virtual_time:
        ctx.slot_time = virtual_time

    # ── Generate reports via LLM ──────────────────────────────────────────────
    try:
        generated = generate_reports(bbox, n, hints, ctx)
    except Exception as e:
        current_app.logger.exception("sim_ai generate_reports failed")
        return jsonify({"error": f"AI generation failed: {e}"}), 502

    if not generated:
        return jsonify({"error": "AI returned no reports"}), 502

    # ── Persist to DB + trigger background assessment ─────────────────────────
    from datetime import datetime, timezone
    from db.models import FieldReportComment
    from sqlalchemy import text

    created = []
    app = current_app._get_current_object()
    for item in generated:
        report = FieldReport(
            event_id     = event_id,
            user_id      = None,
            post_type    = item.get("post_type", "info"),
            description  = item.get("description", ""),
            lat          = float(item.get("lat", 0)),
            lon          = float(item.get("lon", 0)),
        )
        db.session.add(report)
        db.session.flush()  # get report.id

        # Override created_at (server_default bypasses Python-set values)
        raw_dt = item.get("created_at")
        if raw_dt:
            try:
                dt_val = datetime.fromisoformat(raw_dt)
                db.session.execute(
                    text("UPDATE field_reports SET created_at = :ts WHERE id = :id"),
                    {"ts": dt_val, "id": report.id},
                )
            except Exception:
                pass

        # Insert comments with their timestamps
        for c in (item.get("comments") or []):
            content = str(c.get("content", "")).strip()
            if not content:
                continue
            comment = FieldReportComment(
                report_id = report.id,
                user_id   = None,
                content   = content,
            )
            db.session.add(comment)
            db.session.flush()
            c_dt = c.get("created_at")
            if c_dt:
                try:
                    dt_val = datetime.fromisoformat(c_dt)
                    db.session.execute(
                        text("UPDATE field_report_comments SET created_at = :ts WHERE id = :id"),
                        {"ts": dt_val, "id": comment.id},
                    )
                except Exception:
                    pass

        created.append(report)

    db.session.commit()

    return jsonify([{
        "id":           r.id,
        "post_type":    r.post_type,
        "lat":          r.lat,
        "lon":          r.lon,
        "description":  r.description,
        "bearing":      None,
        "theme_id":     None,
        "like_count":   0,
        "created_at":   r.created_at.isoformat() if r.created_at else None,
    } for r in created]), 201
