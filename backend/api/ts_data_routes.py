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
import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Response, jsonify, request
from utils.auth_middleware import token_required, admin_required

from api.timesteps import (
    timesteps_bp,
    _get_event_and_ts,
    _hotspot_dir,
    _pred_dir,
    _read_json,
)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_REPORT_SCHEMA_VERSION = 2
_PROMPT_VERSION = "2026-08-31.mode-aware-v1"
log = logging.getLogger(__name__)


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
        from pipeline.event_config import THERMAL_MONITORING_MODE, get_event_config

        if get_event_config(event).analysis_mode == THERMAL_MONITORING_MODE:
            from pipeline.weather import ensure_open_meteo_forecast

            records = ensure_open_meteo_forecast(event, ts.slot_time, path.parent)
            return jsonify(records), 200
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
    return jsonify(_population_for_observation(event, ts, path)), 200


def _population_for_observation(event, ts, path: Path) -> dict:
    """Load cached exposure or calculate it from the local WorldPop grid."""
    from pipeline.event_config import get_event_config
    from pipeline.spatial.spatial_helpers import (
        load_geom,
        population_counts,
        unavailable_population,
    )

    config = get_event_config(event)
    population = _read_json(path) if path.exists() else {}
    if population.get("data_available"):
        return population

    if config.analysis_mode == "thermal_monitoring" and config.population_provider == "worldpop_2026":
        default_source = _DATA_DIR / "static" / "ind_pop_2026_CN_1km_R2025A_UA_v1.tif"
        source = Path(os.getenv("WORLDPOP_RASTER_PATH", str(default_source))).expanduser()
        if source.exists():
            population = population_counts(
                source,
                None,
                {},
                event.year,
                analysis_mode=config.analysis_mode,
                hotspot_geom=load_geom(
                    _hotspot_dir(event.id, event.year, ts.slot_time) / "hotspots.geojson"
                ),
            )
            if population.get("data_available"):
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        json.dumps(population, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except OSError as exc:
                    log.info("[population] cache write skipped for %s: %s", path, exc)
                return population

    return population or unavailable_population(
        config.analysis_mode,
        "Population dataset is not configured for this region.",
    )


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

def _metadata_matches(actual: dict, expected: dict | None) -> bool:
    if expected is None:
        return True
    return all(actual.get(key) == value for key, value in expected.items())


def _has_crowd_cache(ai_dir: Path) -> bool:
    return (
        (ai_dir / "summary_crowd.json").exists()
        and (ai_dir / "metadata_crowd.json").exists()
    )


def _load_ai_report(ai_dir: Path, expected_metadata: dict | None = None) -> dict | None:
    """Return standard report dict if summary.json exists, else None.
    Includes has_crowd=True when summary_crowd.json also exists."""
    summary_path = ai_dir / "summary.json"
    metadata_path = ai_dir / "metadata.json"
    if not summary_path.exists() or not metadata_path.exists():
        return None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not _metadata_matches(metadata, expected_metadata):
        return None
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    out = {
        "risk_level":        summary.get("risk_level", "Unknown"),
        "assessment_level":  summary.get("assessment_level"),
        "report_mode":       summary.get("report_mode", "wildfire_prediction"),
        "key_points":        summary.get("key_points", []),
        "situation":         summary.get("situation", ""),
        "key_risks":         summary.get("key_risks", ""),
        "immediate_actions": summary.get("immediate_actions", ""),
        "has_crowd":         _has_crowd_cache(ai_dir),
        "metadata":          metadata,
    }
    for name in ("risk", "impact", "evacuation"):
        p = ai_dir / f"{name}.json"
        if p.exists():
            out[name] = json.loads(p.read_text(encoding="utf-8"))
    return out


def _load_crowd_report(ai_dir: Path, expected_metadata: dict | None = None) -> dict | None:
    """Return crowd-enriched report dict if summary_crowd.json exists, else None."""
    summary_path = ai_dir / "summary_crowd.json"
    metadata_path = ai_dir / "metadata_crowd.json"
    if not summary_path.exists() or not metadata_path.exists():
        return None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not _metadata_matches(metadata, expected_metadata):
        return None
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    out = {
        "risk_level":        summary.get("risk_level", "Unknown"),
        "assessment_level":  summary.get("assessment_level"),
        "report_mode":       summary.get("report_mode", "wildfire_prediction"),
        "key_points":        summary.get("key_points", []),
        "situation":         summary.get("situation", ""),
        "key_risks":         summary.get("key_risks", ""),
        "immediate_actions": summary.get("immediate_actions", ""),
        "has_crowd":         True,
        "metadata":          metadata,
    }
    for name in ("risk", "impact", "evacuation"):
        p = ai_dir / f"{name}.json"
        if p.exists():
            out[name] = json.loads(p.read_text(encoding="utf-8"))
    crowd_path = ai_dir / "crowd.json"
    if crowd_path.exists():
        out["crowd"] = json.loads(crowd_path.read_text(encoding="utf-8"))
    return out


def _save_ai_report(
    ai_dir: Path,
    risk: dict,
    impact: dict,
    evacuation: dict,
    summary: dict,
    metadata: dict,
    crowd: dict | None = None,
    crowd_run: bool = False,
) -> None:
    """Save AI report files. crowd_run=True writes summary_crowd.json instead of summary.json."""
    ai_dir.mkdir(parents=True, exist_ok=True)
    (ai_dir / "risk.json").write_text(json.dumps(risk, ensure_ascii=False, indent=2), encoding="utf-8")
    (ai_dir / "impact.json").write_text(json.dumps(impact, ensure_ascii=False, indent=2), encoding="utf-8")
    (ai_dir / "evacuation.json").write_text(json.dumps(evacuation, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_file = "summary_crowd.json" if crowd_run else "summary.json"
    (ai_dir / summary_file).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata_file = "metadata_crowd.json" if crowd_run else "metadata.json"
    (ai_dir / metadata_file).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if crowd is not None:
        (ai_dir / "crowd.json").write_text(json.dumps(crowd, ensure_ascii=False, indent=2), encoding="utf-8")


def _report_evidence(event, ts) -> tuple[dict, dict, list[dict], list[dict], bool]:
    """Load authoritative inputs and add region/provenance context."""
    from pipeline.event_config import get_event_config
    config = get_event_config(event)
    fire_context = _read_json(
        _pred_dir(event.id, event.year, ts.slot_time) / "fire_context.json"
    )
    if not fire_context:
        return {}, {}, [], [], False

    pop_path = _spatial_dir(event.id, event.year, ts.slot_time) / "ML" / "population.json"
    population = _population_for_observation(event, ts, pop_path)

    roads_path = _spatial_dir(event.id, event.year, ts.slot_time) / "ML" / "roads.geojson"
    roads_geojson = _read_json(roads_path) if roads_path.exists() else {}
    road_summary = _build_road_summary(roads_geojson or {})
    roads_available = config.roads_provider != "none" and roads_path.exists()

    weather_path = _weather_dir(event.id, event.year, ts.slot_time) / "forecast.json"
    weather = _read_json(weather_path) if weather_path.exists() else []
    weather = weather or []

    landmarks_path = _DATA_DIR / "events" / f"{event.year}_{event.id:04d}" / "landmarks.json"
    landmarks = _read_json(landmarks_path) if landmarks_path.exists() else []
    landmarks = landmarks or []

    population_available = bool(population.get("data_available"))
    spread_available = bool(
        config.analysis_mode == "wildfire_prediction"
        and any((fire_context.get("fire") or {}).get(field) is not None for field in (
            "burned_area_km2", "new_area_km2", "growth_rate_km2h",
        ))
    )
    warnings = []
    if not population_available:
        warnings.append(population.get("reason") or "Population exposure is unavailable.")
    if not roads_available:
        warnings.append("Road-network analysis is not configured for this region.")
    if not spread_available:
        warnings.append("No validated wildfire-spread forecast is available for this observation.")

    region = {
        "event_id": event.id,
        "region_id": config.region_id,
        "name": event.name,
        "state": config.state,
        "country_code": config.country_code,
        "monitoring_focus": config.monitoring_focus,
        "bbox": list(config.view_bbox or config.bbox),
    }
    enriched = {
        **fire_context,
        "analysis_mode": config.analysis_mode,
        "region": region,
        "landmarks": [
            {"name": item.get("name"), "type": item.get("type", "")}
            for item in landmarks
            if item.get("name")
        ],
        "data_availability": {
            "population": population_available,
            "roads": roads_available,
            "spread_forecast": spread_available,
            "weather": bool(weather or fire_context.get("fwi_t1")),
        },
        "data_warnings": warnings,
        "weather_forecast": weather,
        "wind_forecast": weather,
        "data_sources": {
            "thermal_observations": "NASA FIRMS" if config.analysis_mode == "thermal_monitoring" else None,
            "weather": weather[0].get("source") if weather else None,
            "population": population.get("source"),
            "roads": config.roads_provider if roads_available else None,
            "landcover": config.landcover_provider,
            "industrial_context": config.industrial_context_provider,
        },
    }
    return enriched, population, road_summary, landmarks, roads_available


def _expected_report_metadata(
    evidence: dict,
    population: dict,
    road_summary: list[dict],
    *,
    report_kind: str,
    crowd_reports: list[dict] | None = None,
) -> tuple[dict, dict]:
    from agents._client import get_llm_metadata

    llm = get_llm_metadata()
    input_payload = {
        "evidence": evidence,
        "population": population,
        "road_summary": road_summary,
        "crowd_reports": crowd_reports if report_kind == "crowd" else None,
    }
    input_hash = hashlib.sha256(
        json.dumps(input_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    expected = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "prompt_version": _PROMPT_VERSION,
        "provider": llm["provider"],
        "model": llm["model"],
        "input_hash": input_hash,
        "report_kind": report_kind,
    }
    metadata = {
        **expected,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "region": evidence.get("region"),
        "observation_time": evidence.get("observation_time"),
        "analysis_mode": evidence.get("analysis_mode"),
        "data_availability": evidence.get("data_availability", {}),
        "data_sources": evidence.get("data_sources", {}),
        "warnings": evidence.get("data_warnings", []),
    }
    return expected, metadata


def _report_response(
    overview: dict,
    risk: dict,
    impact: dict,
    evacuation: dict,
    metadata: dict,
    *,
    has_crowd: bool,
    crowd: dict | None = None,
) -> dict:
    response = {
        "risk_level": overview.get("risk_level", "Unknown"),
        "assessment_level": overview.get("assessment_level"),
        "report_mode": overview.get("report_mode", metadata.get("analysis_mode")),
        "key_points": overview.get("key_points", []),
        "situation": overview.get("situation", ""),
        "key_risks": overview.get("key_risks", ""),
        "immediate_actions": overview.get("immediate_actions", ""),
        "risk": risk,
        "impact": impact,
        "evacuation": evacuation,
        "has_crowd": has_crowd,
        "metadata": metadata,
    }
    if crowd is not None:
        response["crowd"] = crowd
    return response


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
    fire_context, population, road_summary, landmarks, roads_available = _report_evidence(
        event, ts
    )
    if not fire_context:
        return jsonify({"error": "fire context not available — run prediction first"}), 422
    expected_metadata, metadata = _expected_report_metadata(
        fire_context,
        population,
        road_summary,
        report_kind="standard",
    )

    if not force:
        cached = _load_ai_report(ai_dir, expected_metadata)
        if cached:
            return jsonify(cached), 200

    import concurrent.futures
    from agents import run_risk_agent, run_impact_agent, run_evacuation_agent, run_summary_agent
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            f_risk   = pool.submit(run_risk_agent, fire_context)
            f_impact = pool.submit(run_impact_agent, fire_context, population)
            f_evac   = pool.submit(
                run_evacuation_agent,
                fire_context,
                road_summary,
                landmarks,
                roads_available=roads_available,
            )
            risk_data   = f_risk.result()
            impact_data = f_impact.result()
            evac_data   = f_evac.result()

        overview = run_summary_agent(
            risk_data, impact_data, evac_data, report_context=fire_context
        )
    except Exception as e:
        import logging; logging.getLogger(__name__).error("AI agent failed: %s", e)
        return jsonify({"error": "AI report generation failed. Check server logs."}), 502

    _save_ai_report(
        ai_dir, risk_data, impact_data, evac_data, overview, metadata
    )

    return jsonify(_report_response(
        overview,
        risk_data,
        impact_data,
        evac_data,
        metadata,
        has_crowd=_has_crowd_cache(ai_dir),
    )), 200


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
    fire_context, population, road_summary, landmarks, roads_available = _report_evidence(
        event, ts
    )
    if not fire_context:
        return jsonify({"error": "fire context not available — run prediction first"}), 422

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
    expected_metadata, metadata = _expected_report_metadata(
        fire_context,
        population,
        road_summary,
        report_kind="crowd",
        crowd_reports=report_dicts,
    )

    if not force:
        cached = _load_crowd_report(ai_dir, expected_metadata)
        if cached:
            return jsonify(cached), 200

    from agents import run_risk_agent, run_impact_agent, run_evacuation_agent, run_summary_agent, run_crowd_analysis
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            f_risk   = pool.submit(run_risk_agent, fire_context)
            f_impact = pool.submit(run_impact_agent, fire_context, population)
            f_evac   = pool.submit(
                run_evacuation_agent,
                fire_context,
                road_summary,
                landmarks,
                roads_available=roads_available,
            )
            f_crowd  = pool.submit(run_crowd_analysis, report_dicts)
            risk_data   = f_risk.result()
            impact_data = f_impact.result()
            evac_data   = f_evac.result()
            crowd_data  = f_crowd.result()

        overview = run_summary_agent(
            risk_data,
            impact_data,
            evac_data,
            crowd_analysis=crowd_data,
            report_context=fire_context,
        )
    except Exception as e:
        import logging; logging.getLogger(__name__).error("AI agent failed: %s", e)
        return jsonify({"error": "AI report generation failed. Check server logs."}), 502

    _save_ai_report(
        ai_dir,
        risk_data,
        impact_data,
        evac_data,
        overview,
        metadata,
        crowd=crowd_data,
        crowd_run=True,
    )

    return jsonify(_report_response(
        overview,
        risk_data,
        impact_data,
        evac_data,
        metadata,
        has_crowd=True,
        crowd=crowd_data,
    )), 200
