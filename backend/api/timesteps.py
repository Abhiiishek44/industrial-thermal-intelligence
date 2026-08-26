"""
api/timesteps.py
----------------
Core blueprint, shared helpers, control endpoints, and chat.

Routes:
    GET  /events/<id>/timesteps
    GET  /events/<id>/timesteps/<ts_id>/status
    POST /events/<id>/timesteps/<ts_id>/run-prediction   body: {force?: bool}
    POST /events/<id>/chat

Prediction/weather/spatial/report routes are registered by importing
the sub-modules at the bottom of this file.
"""

from __future__ import annotations

import json
from pathlib import Path

from datetime import date

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context
from utils.auth_middleware import token_required, admin_required

timesteps_bp = Blueprint("timesteps", __name__)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# Chat quota: 5 messages per GitHub user per UTC day. Counts persist in the
# users table so quota survives restarts and is per-user (not per-session).
_CHAT_LIMIT = 5


# ── Shared path helpers ───────────────────────────────────────────────────────

def _ts_base(event_id: int, year: int, slot_time) -> Path:
    import pandas as pd
    ts_str = pd.Timestamp(slot_time).strftime("%Y-%m-%dT%H%M")
    return _DATA_DIR / "events" / f"{year}_{event_id:04d}" / "timesteps" / ts_str


def _pred_dir(event_id: int, year: int, slot_time) -> Path:
    """Returns prediction/ML/ — where ML risk-zone outputs live."""
    return _ts_base(event_id, year, slot_time) / "prediction" / "ML"


def _perim_dir(event_id: int, year: int, slot_time) -> Path:
    return _ts_base(event_id, year, slot_time) / "perimeter"


def _hotspot_dir(event_id: int, year: int, slot_time) -> Path:
    return _ts_base(event_id, year, slot_time) / "hotspot"


def _actual_perim_dir(event_id: int, year: int, slot_time) -> Path:
    return _ts_base(event_id, year, slot_time) / "actual_perimeter"


_SENTINEL = {
    # status_dir suffix → file that proves the stage completed without STATUS.json
    "ML":               "fire_context.json",
    "wind_driven":      "STATUS.json",          # no sentinel yet; always needs STATUS.json
    "spatial_analysis": "ML/roads.geojson",
}

def _read_status(status_dir: Path) -> str:
    """Read status, checking in-memory running set first, then STATUS.json, then sentinels."""
    from pipeline.check.builder_slots import _read_status as _slots_read_status, _write_status as _slots_write_status
    # Check in-memory running first
    from pipeline.check.builder_slots import _running, _running_lock
    with _running_lock:
        if str(status_dir) in _running:
            return "running"

    path = status_dir / "STATUS.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("status", "pending")
        except Exception:
            return "pending"

    # STATUS.json missing — check sentinel file to handle pre-STATUS.json runs
    sentinel_name = _SENTINEL.get(status_dir.name)
    if sentinel_name and (status_dir / sentinel_name).exists():
        _write_status(status_dir, "done")
        return "done"

    return "pending"


def _write_status(status_dir: Path, status: str) -> None:
    from pipeline.check.builder_slots import _write_status as _slots_write_status
    _slots_write_status(status_dir, status)


def _get_event_and_ts(event_id: int, ts_id: int):
    from db.models import FireEvent, EventTimestep
    event = FireEvent.query.get(event_id)
    if not event:
        return None, (jsonify({"error": "event not found"}), 404)
    ts = EventTimestep.query.filter_by(id=ts_id, event_id=event_id).first()
    if not ts:
        return None, (jsonify({"error": "timestep not found"}), 404)
    return (event, ts), None


def _read_geojson(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"type": "FeatureCollection", "features": []}


def _read_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


# ── List timesteps ────────────────────────────────────────────────────────────

@timesteps_bp.route("/events/<int:event_id>/timesteps", methods=["GET"])
@token_required
def list_timesteps(event_id: int):
    from db.models import FireEvent, EventTimestep
    event = FireEvent.query.get(event_id)
    if not event:
        return jsonify({"error": "event not found"}), 404
    rows = (
        EventTimestep.query
        .filter_by(event_id=event_id)
        .order_by(EventTimestep.slot_time)
        .all()
    )
    result = []
    for ts in rows:
        base     = _ts_base(event.id, event.year, ts.slot_time)
        ml_st    = _read_status(base / "prediction" / "ML")
        wd_st    = _read_status(base / "prediction" / "wind_driven")
        sp_st    = _read_status(base / "spatial_analysis")
        result.append({
            "id":                      ts.id,
            "slot_time":               ts.slot_time.isoformat(),
            "nearest_t1":              ts.nearest_t1.isoformat(),
            "gap_hours":               ts.gap_hours,
            "data_gap_warn":           ts.data_gap_warn,
            "prediction_status":       ml_st,
            "spatial_analysis_status": sp_st,
            "wind_driven_status":      wd_st,
        })
    return jsonify(result), 200


# ── Status + run-prediction ───────────────────────────────────────────────────

def _reset_ts_if_files_missing(event, ts) -> bool:
    """Reset ML status to 'pending' if STATUS.json says done but perimeter.geojson is gone."""
    base     = _ts_base(event.id, event.year, ts.slot_time)
    ml_dir   = base / "prediction" / "ML"
    ml_st    = _read_status(ml_dir)
    if ml_st not in ("done", "failed"):
        return False
    sentinel = ml_dir / "fire_context.json"
    if sentinel.exists():
        return False
    _write_status(ml_dir, "pending")
    _write_status(base / "spatial_analysis", "pending")
    return True


@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/status", methods=["GET"])
@token_required
def get_ts_status(event_id: int, ts_id: int):
    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result
    _reset_ts_if_files_missing(event, ts)
    base = _ts_base(event.id, event.year, ts.slot_time)
    return jsonify({
        "prediction_status":        _read_status(base / "prediction" / "ML"),
        "spatial_analysis_status":  _read_status(base / "spatial_analysis"),
        "wind_driven_status":       _read_status(base / "prediction" / "wind_driven"),
        "crowd_prediction_status":  _read_status(base / "prediction" / "ML_crowd"),
        "spatial_crowd_status":     _read_status(base / "spatial_analysis_crowd"),
    })


@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/run-prediction", methods=["POST"])
@admin_required
def run_prediction(event_id: int, ts_id: int):
    """Trigger prediction pipeline.

    Body (optional JSON):
        { "force": true }          — wipe existing outputs and re-run even if already done.
        { "crowd": true }          — run crowd-augmented prediction (ML_crowd/).
        { "crowd": true, "force": true } — wipe crowd outputs and re-run.
    """
    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result

    body  = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    crowd = bool(body.get("crowd"))

    from utils.background import run_in_background
    app = current_app._get_current_object()

    # ── Crowd prediction branch ───────────────────────────────────────────────
    if crowd:
        base     = _ts_base(event.id, event.year, ts.slot_time)
        crow_dir = base / "prediction" / "ML_crowd"
        sp_crowd = base / "spatial_analysis_crowd"
        crow_st  = _read_status(crow_dir)

        if force and crow_st == "done":
            import shutil, os, stat
            def _on_rm_error(func, path, exc_info):
                try:
                    os.chmod(path, stat.S_IWRITE)
                    func(path)
                except Exception:
                    pass
            for d in (crow_dir, sp_crowd):
                if d.exists():
                    try:
                        shutil.rmtree(d, onerror=_on_rm_error)
                    except Exception:
                        pass
            crow_st = "pending"

        if crow_st == "running":
            return jsonify({"status": "running"})

        _write_status(crow_dir, "running")
        from pipeline.check.builder import build_single_timestep_ondemand_crowd
        run_in_background(build_single_timestep_ondemand_crowd, app, ts_id)
        return jsonify({"status": "running"})

    # ── Standard prediction branch ────────────────────────────────────────────
    base   = _ts_base(event.id, event.year, ts.slot_time)
    ml_dir = base / "prediction" / "ML"
    ml_st  = _read_status(ml_dir)

    if force and ml_st == "done":
        _wipe_prediction_outputs(event, ts)
        ml_st = "pending"

    # Auto-heal: STATUS says done but files missing
    _reset_ts_if_files_missing(event, ts)
    ml_st = _read_status(ml_dir)

    if ml_st in ("done", "running"):
        return jsonify({"status": ml_st})

    _write_status(ml_dir, "running")

    from pipeline.check.builder import build_single_timestep_ondemand
    run_in_background(build_single_timestep_ondemand, app, ts_id)
    return jsonify({"status": "running"})


def _wipe_prediction_outputs(event, ts) -> None:
    """Delete prediction + spatial_analysis dirs and reset STATUS.json to pending.

    Uses an onerror handler to handle Windows-locked files (OneDrive, Explorer).
    Falls back to deleting individual files if rmtree still fails.
    """
    import os
    import shutil
    import stat

    def _on_rm_error(func, path, exc_info):
        """Make read-only files writable then retry (Windows WinError 5 fix)."""
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass  # best-effort; don't crash the endpoint

    base = _ts_base(event.id, event.year, ts.slot_time)

    for sub in ("prediction", "spatial_analysis"):
        d = base / sub
        if d.exists():
            try:
                shutil.rmtree(d, onerror=_on_rm_error)
            except Exception:
                for f in d.rglob("*"):
                    if f.is_file():
                        try:
                            os.chmod(f, stat.S_IWRITE)
                            f.unlink()
                        except Exception:
                            pass

    _write_status(base / "prediction" / "ML", "pending")
    _write_status(base / "prediction" / "wind_driven", "pending")
    _write_status(base / "spatial_analysis", "pending")


# ── Build-all (SSE) ───────────────────────────────────────────────────────────

@timesteps_bp.route("/events/<int:event_id>/build-all", methods=["POST"])
@admin_required
def build_all_predictions(event_id: int):
    """SSE: run full prediction pipeline for all pending timesteps in an event.

    Yields newline-delimited JSON events:
        { total, done, current, status }   status = loading | running | done | error
    """
    import json as _json
    import logging as _logging
    import pandas as _pd
    from db.models import FireEvent, EventTimestep

    _log = _logging.getLogger(__name__)

    event = FireEvent.query.get(event_id)
    if not event:
        return jsonify({"error": "event not found"}), 404

    def _generate():
        from pipeline.check.builder import (
            _get_event_assets, _load_predictor, _load_threshold, _build_timestep,
        )

        timesteps = (
            EventTimestep.query
            .filter_by(event_id=event_id)
            .order_by(EventTimestep.slot_time)
            .all()
        )

        pending = [
            ts for ts in timesteps
            if _read_status(_ts_base(event.id, event.year, ts.slot_time) / "prediction" / "ML")
               not in ("done", "running")
        ]

        total = len(pending)
        yield f"data: {_json.dumps({'total': total, 'done': 0, 'status': 'loading'})}\n\n"

        try:
            assets    = _get_event_assets(event)
            predictor = _load_predictor()
            threshold = _load_threshold()
        except Exception as exc:
            _log.error("[build-all] setup failed: %s", exc)
            yield f"data: {_json.dumps({'error': str(exc), 'status': 'error'})}\n\n"
            return

        for i, ts in enumerate(pending):
            ts_label = _pd.Timestamp(ts.slot_time).strftime("%Y-%m-%d %H:%M")
            yield f"data: {_json.dumps({'total': total, 'done': i, 'current': ts_label, 'status': 'running'})}\n\n"
            try:
                _build_timestep(event, ts, assets, predictor, threshold)
            except Exception as exc:
                _log.error("[build-all] ts %d failed: %s", ts.id, exc)

        yield f"data: {_json.dumps({'total': total, 'done': total, 'status': 'done'})}\n\n"

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Chat ──────────────────────────────────────────────────────────────────────

@timesteps_bp.route("/events/<int:event_id>/chat", methods=["POST"])
@token_required
def chat(event_id: int):
    body    = request.get_json(force=True)
    message = body.get("message", "").strip()
    ts_id   = body.get("timestep_id")
    history = body.get("history", [])

    if not message:
        return jsonify({"error": "message required"}), 400

    payload  = request.current_user or {}
    user_id  = payload.get("user_id")
    is_admin = payload.get("is_admin", False)
    if not is_admin and user_id is not None:
        from db.connection import db
        from db.models import User
        u = User.query.get(user_id)
        if u is None:
            return jsonify({"error": "User not found."}), 401
        today = date.today()
        if u.chat_count_date != today:
            u.chat_count_date = today
            u.chat_count      = 0
        if u.chat_count >= _CHAT_LIMIT:
            return jsonify({"error": f"Daily chat limit reached ({_CHAT_LIMIT} per day). Try again tomorrow."}), 429
        u.chat_count += 1
        db.session.commit()

    summary      = ""
    road_summary = []
    if ts_id:
        result, _ = _get_event_and_ts(event_id, ts_id)
        if result:
            event_obj, ts_obj = result
            import pandas as _pd
            ts_str  = _pd.Timestamp(ts_obj.slot_time).strftime("%Y-%m-%dT%H%M")
            ai_dir  = (_DATA_DIR / "events" / f"{event_obj.year}_{event_obj.id:04d}"
                       / "timesteps" / ts_str / "AI_report")
            # Build rich context for the chat agent from structured AI_report files
            summary_data = _read_json(ai_dir / "summary.json") or {}
            parts = []
            if summary_data.get("risk_level"):
                parts.append("RISK LEVEL: " + summary_data["risk_level"])
            if summary_data.get("key_points"):
                parts.append("KEY POINTS:\n" + "\n".join("- " + p for p in summary_data["key_points"]))
            if summary_data.get("situation"):
                parts.append("SITUATION:\n" + summary_data["situation"])
            if summary_data.get("key_risks"):
                parts.append("KEY RISKS:\n" + summary_data["key_risks"])
            if summary_data.get("immediate_actions"):
                parts.append("IMMEDIATE ACTIONS:\n" + summary_data["immediate_actions"])
            for fname, label in (("risk", "RISK ANALYSIS"), ("impact", "IMPACT ANALYSIS"),
                                  ("evacuation", "EVACUATION ANALYSIS"), ("crowd", "CROWD INTELLIGENCE")):
                d = _read_json(ai_dir / f"{fname}.json")
                if d:
                    parts.append(f"{label}:\n{__import__('json').dumps(d, ensure_ascii=False)}")
            summary = "\n\n".join(parts)
            # Road summary from spatial analysis roads.geojson
            from api.ts_data_routes import _spatial_dir, _build_road_summary
            roads_path    = _spatial_dir(event_obj.id, event_obj.year, ts_obj.slot_time) / "ML" / "roads.geojson"
            roads_geojson = _read_json(roads_path) or {}
            road_summary  = _build_road_summary(roads_geojson)

    from agents.chat_agent import run_chat_agent
    return Response(
        stream_with_context(run_chat_agent(summary=summary, road_summary=road_summary,
                                           message=message, history=history)),
        mimetype="text/plain",
    )


# ── Register sub-module routes on this blueprint ──────────────────────────────
import api.ts_prediction_routes  # noqa: E402, F401
import api.ts_data_routes        # noqa: E402, F401
