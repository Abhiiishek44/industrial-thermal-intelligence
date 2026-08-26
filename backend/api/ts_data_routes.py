"""
api/ts_data_routes.py
----------------------
Weather, spatial, and AI report endpoints.

Routes (on timesteps_bp):
    GET  /events/<id>/timesteps/<ts_id>/weather
    GET  /events/<id>/timesteps/<ts_id>/wind-field?hour=N
    GET  /events/<id>/timesteps/<ts_id>/roads
    GET  /events/<id>/timesteps/<ts_id>/population
    POST /events/<id>/timesteps/<ts_id>/report
    POST /events/<id>/timesteps/<ts_id>/report-with-crowd
"""

from __future__ import annotations

import json
from pathlib import Path

from flask import Response, jsonify, request
from utils.auth_middleware import token_required, admin_required

from api.timesteps import timesteps_bp, _get_event_and_ts, _pred_dir, _read_json

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _weather_dir(event_id: int, year: int, slot_time) -> Path:
    import pandas as pd
    ts_str = pd.Timestamp(slot_time).strftime("%Y-%m-%dT%H%M")
    return _DATA_DIR / "events" / f"{year}_{event_id:04d}" / "timesteps" / ts_str / "weather"


def _spatial_dir(event_id: int, year: int, slot_time) -> Path:
    import pandas as pd
    ts_str = pd.Timestamp(slot_time).strftime("%Y-%m-%dT%H%M")
    return _DATA_DIR / "events" / f"{year}_{event_id:04d}" / "timesteps" / ts_str / "spatial_analysis"


def _spatial_crowd_dir(event_id: int, year: int, slot_time) -> Path:
    import pandas as pd
    ts_str = pd.Timestamp(slot_time).strftime("%Y-%m-%dT%H%M")
    return _DATA_DIR / "events" / f"{year}_{event_id:04d}" / "timesteps" / ts_str / "spatial_analysis_crowd"


def _ai_report_dir(event_id: int, year: int, slot_time) -> Path:
    import pandas as pd
    ts_str = pd.Timestamp(slot_time).strftime("%Y-%m-%dT%H%M")
    return _DATA_DIR / "events" / f"{year}_{event_id:04d}" / "timesteps" / ts_str / "AI_report"


# ── Weather ───────────────────────────────────────────────────────────────────

@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/weather", methods=["GET"])
@token_required
def get_weather(event_id: int, ts_id: int):
    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result
    path = _weather_dir(event.id, event.year, ts.slot_time) / "forecast.json"
    if not path.exists():
        return jsonify([]), 200
    return Response(path.read_text(encoding="utf-8"), mimetype="application/json"), 200


@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/wind-field", methods=["GET"])
@token_required
def get_wind_field(event_id: int, ts_id: int):
    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result
    path = _weather_dir(event.id, event.year, ts.slot_time) / "wind_field.json"
    if not path.exists():
        return jsonify([]), 200

    hour_param = request.args.get("hour")
    if hour_param is None:
        return Response(path.read_text(encoding="utf-8"), mimetype="application/json"), 200

    try:
        hour = int(hour_param)
    except (TypeError, ValueError):
        hour = 0

    all_hours = json.loads(path.read_text(encoding="utf-8"))
    entry = next((h for h in all_hours if h["hour"] == hour), None)
    if entry is None and all_hours:
        entry = all_hours[0]
    if entry is None:
        return jsonify([]), 200
    return jsonify(entry["data"]), 200


# ── Spatial ───────────────────────────────────────────────────────────────────

@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/roads", methods=["GET"])
@token_required
def get_roads(event_id: int, ts_id: int):
    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result
    crowd = request.args.get("crowd", "false").lower() == "true"
    model = request.args.get("model", "ML")
    if model not in ("ML", "wind_driven"):
        model = "ML"
    if crowd:
        path = _spatial_crowd_dir(event.id, event.year, ts.slot_time) / model / "roads.geojson"
    else:
        path = _spatial_dir(event.id, event.year, ts.slot_time) / model / "roads.geojson"
    from api.timesteps import _read_geojson
    return jsonify(_read_geojson(path)), 200


@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/population", methods=["GET"])
@token_required
def get_population(event_id: int, ts_id: int):
    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result
    crowd = request.args.get("crowd", "false").lower() == "true"
    model = request.args.get("model", "ML")
    if model not in ("ML", "wind_driven"):
        model = "ML"
    if crowd:
        path = _spatial_crowd_dir(event.id, event.year, ts.slot_time) / model / "population.json"
    else:
        path = _spatial_dir(event.id, event.year, ts.slot_time) / model / "population.json"
    if not path.exists():
        return jsonify({}), 200
    return Response(path.read_text(encoding="utf-8"), mimetype="application/json"), 200


# ── Road summary helper ───────────────────────────────────────────────────────

_MAJOR_HW   = {"motorway", "trunk", "primary", "secondary"}
_HW_TYPES   = {"motorway", "motorway_link", "trunk", "trunk_link",
               "primary", "primary_link", "secondary", "secondary_link"}
_STATUS_RANK = {"burning": 0, "burned": 1, "at_risk_3h": 2, "at_risk_6h": 3, "at_risk_12h": 4}


def _build_road_summary(roads_geojson: dict) -> list[dict]:
    """Return major non-clear roads sorted by severity for the evacuation agent."""
    by_road: dict[str, dict] = {}
    for f in (roads_geojson.get("features") or []):
        p      = f.get("properties") or {}
        road   = p.get("road_name", "")
        hw     = p.get("highway", "")
        status = p.get("status", "clear")
        if hw not in _MAJOR_HW or road in _HW_TYPES or status == "clear":
            continue
        sections = p.get("sections") or []
        if isinstance(sections, str):
            try:
                sections = json.loads(sections)
            except Exception:
                sections = []
        rank = _STATUS_RANK.get(status, 99)
        if road not in by_road or rank < _STATUS_RANK.get(by_road[road]["status"], 99):
            by_road[road] = {"road": road, "highway": hw, "status": status, "sections": sections}
    return sorted(by_road.values(), key=lambda x: _STATUS_RANK.get(x["status"], 99))


# ── AI Report helpers ─────────────────────────────────────────────────────────

def _load_ai_report(ai_dir: Path) -> dict | None:
    """Return standard report dict if summary.json exists, else None.
    Includes has_crowd=True when summary_crowd.json also exists."""
    summary_path = ai_dir / "summary.json"
    if not summary_path.exists():
        return None
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    out = {
        "risk_level":        summary.get("risk_level", "Unknown"),
        "key_points":        summary.get("key_points", []),
        "situation":         summary.get("situation", ""),
        "key_risks":         summary.get("key_risks", ""),
        "immediate_actions": summary.get("immediate_actions", ""),
        "has_crowd":         (ai_dir / "summary_crowd.json").exists(),
    }
    for name in ("risk", "impact", "evacuation"):
        p = ai_dir / f"{name}.json"
        if p.exists():
            out[name] = json.loads(p.read_text(encoding="utf-8"))
    crowd_path = ai_dir / "crowd.json"
    if crowd_path.exists():
        out["crowd"] = json.loads(crowd_path.read_text(encoding="utf-8"))
    return out


def _load_crowd_report(ai_dir: Path) -> dict | None:
    """Return crowd-enriched report dict if summary_crowd.json exists, else None."""
    summary_path = ai_dir / "summary_crowd.json"
    if not summary_path.exists():
        return None
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    out = {
        "risk_level":        summary.get("risk_level", "Unknown"),
        "key_points":        summary.get("key_points", []),
        "situation":         summary.get("situation", ""),
        "key_risks":         summary.get("key_risks", ""),
        "immediate_actions": summary.get("immediate_actions", ""),
        "has_crowd":         True,
    }
    for name in ("risk", "impact", "evacuation"):
        p = ai_dir / f"{name}.json"
        if p.exists():
            out[name] = json.loads(p.read_text(encoding="utf-8"))
    crowd_path = ai_dir / "crowd.json"
    if crowd_path.exists():
        out["crowd"] = json.loads(crowd_path.read_text(encoding="utf-8"))
    return out


def _save_ai_report(ai_dir: Path, risk: dict, impact: dict, evacuation: dict,
                    summary: dict, crowd: dict | None = None, crowd_run: bool = False) -> None:
    """Save AI report files. crowd_run=True writes summary_crowd.json instead of summary.json."""
    ai_dir.mkdir(parents=True, exist_ok=True)
    (ai_dir / "risk.json").write_text(json.dumps(risk, ensure_ascii=False, indent=2), encoding="utf-8")
    (ai_dir / "impact.json").write_text(json.dumps(impact, ensure_ascii=False, indent=2), encoding="utf-8")
    (ai_dir / "evacuation.json").write_text(json.dumps(evacuation, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_file = "summary_crowd.json" if crowd_run else "summary.json"
    (ai_dir / summary_file).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if crowd is not None:
        (ai_dir / "crowd.json").write_text(json.dumps(crowd, ensure_ascii=False, indent=2), encoding="utf-8")


# ── AI Report ─────────────────────────────────────────────────────────────────

@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/report", methods=["POST"])
@token_required
def generate_report(event_id: int, ts_id: int):
    """Return cached AI report; any authed user may trigger first generation.
    Body: { force: true } bypasses cache (admin only — non-admins cannot
    regenerate, only kick off the initial run on a cache miss)."""
    is_admin = (request.current_user or {}).get('is_admin', False)

    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result

    force = bool((request.get_json(silent=True) or {}).get("force", False))
    if force and not is_admin:
        return jsonify({"error": "Only admins can regenerate cached reports."}), 403

    ai_dir = _ai_report_dir(event.id, event.year, ts.slot_time)

    if not force:
        cached = _load_ai_report(ai_dir)
        if cached:
            return jsonify(cached), 200

    fire_context = _read_json(_pred_dir(event.id, event.year, ts.slot_time) / "fire_context.json")
    if not fire_context:
        return jsonify({"error": "fire context not available — run prediction first"}), 422

    pop_path = _spatial_dir(event.id, event.year, ts.slot_time) / "ML" / "population.json"
    population = json.loads(pop_path.read_text(encoding="utf-8")) if pop_path.exists() else {}

    roads_path    = _spatial_dir(event.id, event.year, ts.slot_time) / "ML" / "roads.geojson"
    roads_geojson = json.loads(roads_path.read_text(encoding="utf-8")) if roads_path.exists() else {}
    road_summary  = _build_road_summary(roads_geojson)

    landmarks_path = _DATA_DIR / "events" / f"{event.year}_{event.id:04d}" / "landmarks.json"
    landmarks = json.loads(landmarks_path.read_text(encoding="utf-8")) if landmarks_path.exists() else []

    import concurrent.futures
    from agents import run_risk_agent, run_impact_agent, run_evacuation_agent, run_summary_agent
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            f_risk   = pool.submit(run_risk_agent, fire_context)
            f_impact = pool.submit(run_impact_agent, fire_context, population)
            f_evac   = pool.submit(run_evacuation_agent, fire_context, road_summary, landmarks)
            risk_data   = f_risk.result()
            impact_data = f_impact.result()
            evac_data   = f_evac.result()

        overview = run_summary_agent(risk_data, impact_data, evac_data)
    except Exception as e:
        import logging; logging.getLogger(__name__).error("AI agent failed: %s", e)
        return jsonify({"error": "AI report generation failed. Check server logs."}), 502

    _save_ai_report(ai_dir, risk_data, impact_data, evac_data, overview)

    resp = {
        "risk_level":        overview.get("risk_level", "Unknown"),
        "key_points":        overview.get("key_points", []),
        "situation":         overview.get("situation", ""),
        "key_risks":         overview.get("key_risks", ""),
        "immediate_actions": overview.get("immediate_actions", ""),
        "risk":              risk_data,
        "impact":            impact_data,
        "evacuation":        evac_data,
        "has_crowd":         (ai_dir / "summary_crowd.json").exists(),
    }
    crowd_path = ai_dir / "crowd.json"
    if crowd_path.exists():
        resp["crowd"] = json.loads(crowd_path.read_text(encoding="utf-8"))
    return jsonify(resp), 200


@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/report-with-crowd", methods=["POST"])
@admin_required
def generate_report_with_crowd(event_id: int, ts_id: int):
    """Return cached crowd report if available; generate only when needed.
    Body: { force: true } bypasses cache.
    Saves to summary_crowd.json (never overwrites summary.json).
    """
    import concurrent.futures
    import pandas as pd
    from db.models import FieldReport

    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result

    force = bool((request.get_json(silent=True) or {}).get("force", False))
    ai_dir = _ai_report_dir(event.id, event.year, ts.slot_time)

    if not force:
        cached = _load_crowd_report(ai_dir)
        if cached:
            return jsonify(cached), 200

    fire_context = _read_json(_pred_dir(event.id, event.year, ts.slot_time) / "fire_context.json")
    if not fire_context:
        return jsonify({"error": "fire context not available — run prediction first"}), 422

    pop_path  = _spatial_dir(event.id, event.year, ts.slot_time) / "ML" / "population.json"
    population = json.loads(pop_path.read_text(encoding="utf-8")) if pop_path.exists() else {}

    roads_path    = _spatial_dir(event.id, event.year, ts.slot_time) / "ML" / "roads.geojson"
    roads_geojson = json.loads(roads_path.read_text(encoding="utf-8")) if roads_path.exists() else {}
    road_summary  = _build_road_summary(roads_geojson)

    landmarks_path = _DATA_DIR / "events" / f"{event.year}_{event.id:04d}" / "landmarks.json"
    landmarks = json.loads(landmarks_path.read_text(encoding="utf-8")) if landmarks_path.exists() else []

    # Fetch crowd reports within 24h of the slot
    slot_time    = pd.Timestamp(ts.slot_time)
    window_start = (slot_time - pd.Timedelta(hours=24)).to_pydatetime()
    raw_reports  = (
        FieldReport.query
        .filter(
            FieldReport.event_id == event.id,
            FieldReport.created_at >= window_start,
        )
        .order_by(FieldReport.created_at.desc())
        .all()
    )
    report_dicts = [
        {
            "post_type":    r.post_type,
            "description":  r.description,
            "lat":          r.lat,
            "lon":          r.lon,
            "created_at":   r.created_at.isoformat() if r.created_at else None,
        }
        for r in raw_reports
    ]

    from agents import run_risk_agent, run_impact_agent, run_evacuation_agent, run_summary_agent, run_crowd_analysis
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            f_risk   = pool.submit(run_risk_agent, fire_context)
            f_impact = pool.submit(run_impact_agent, fire_context, population)
            f_evac   = pool.submit(run_evacuation_agent, fire_context, road_summary, landmarks)
            f_crowd  = pool.submit(run_crowd_analysis, report_dicts)
            risk_data   = f_risk.result()
            impact_data = f_impact.result()
            evac_data   = f_evac.result()
            crowd_data  = f_crowd.result()

        overview = run_summary_agent(risk_data, impact_data, evac_data, crowd_analysis=crowd_data)
    except Exception as e:
        import logging; logging.getLogger(__name__).error("AI agent failed: %s", e)
        return jsonify({"error": "AI report generation failed. Check server logs."}), 502

    _save_ai_report(ai_dir, risk_data, impact_data, evac_data, overview, crowd=crowd_data, crowd_run=True)

    return jsonify({
        "risk_level":        overview.get("risk_level", "Unknown"),
        "key_points":        overview.get("key_points", []),
        "situation":         overview.get("situation", ""),
        "key_risks":         overview.get("key_risks", ""),
        "immediate_actions": overview.get("immediate_actions", ""),
        "risk":              risk_data,
        "impact":            impact_data,
        "evacuation":        evac_data,
        "crowd":             crowd_data,
        "has_crowd":         True,
    }), 200
