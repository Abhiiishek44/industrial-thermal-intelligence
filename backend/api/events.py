import json
from pathlib import Path

import geopandas as gpd
from flask import Blueprint, jsonify, request
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from db.models import FireEvent
from utils.auth_middleware import token_required

# In-memory shared replay clock: {event_id: {ms, pushed_at, speed}}
_replay_times: dict[int, dict] = {}

events_bp = Blueprint('events', __name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "events"


def _event_dir(event) -> Path:
    return DATA_DIR / f"{event.year}_{event.id:04d}"


def _serialize(e):
    bounds = to_shape(e.bbox).bounds  # (minx, miny, maxx, maxy)
    return {
        'id':          e.id,
        'name':        e.name,
        'year':        e.year,
        'start_date':  e.start_date.isoformat() if e.start_date else None,
        'end_date':    e.end_date.isoformat() if e.end_date else None,
        'description': e.description,
        'bbox':        list(bounds),  # [minLon, minLat, maxLon, maxLat]
    }


@events_bp.route('/', methods=['GET'])
def list_events():
    events = FireEvent.query.order_by(FireEvent.year.desc()).all()
    return jsonify([_serialize(e) for e in events]), 200


@events_bp.route('/<int:event_id>', methods=['GET'])
def get_event(event_id: int):
    event = FireEvent.query.get(event_id)
    if not event:
        return jsonify({'error': 'event not found'}), 404
    return jsonify(_serialize(event)), 200


@events_bp.route('/<int:event_id>/layers/aoi', methods=['GET'])
@token_required
def get_aoi(event_id: int):
    event = FireEvent.query.get(event_id)
    if not event:
        return jsonify({'error': 'event not found'}), 404
    shape = to_shape(event.bbox)
    fc = {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'geometry': mapping(shape), 'properties': {'name': event.name}}
    ]}
    return jsonify(fc), 200


@events_bp.route('/<int:event_id>/layers/community', methods=['GET'])
@token_required
def get_community(event_id: int):
    event = FireEvent.query.get(event_id)
    if not event:
        return jsonify({'error': 'event not found'}), 404
    lm_path = _event_dir(event) / 'landmarks.json'
    if not lm_path.exists():
        return jsonify({'type': 'FeatureCollection', 'features': []}), 200
    landmarks = json.loads(lm_path.read_text(encoding='utf-8'))
    features = [
        {'type': 'Feature',
         'geometry': {'type': 'Point', 'coordinates': [lm['lon'], lm['lat']]},
         'properties': {'name': lm['name'], 'type': lm.get('type', '')}}
        for lm in landmarks
    ]
    return jsonify({'type': 'FeatureCollection', 'features': features}), 200


@events_bp.route('/<int:event_id>/layers/roads', methods=['GET'])
@token_required
def get_static_roads(event_id: int):
    event = FireEvent.query.get(event_id)
    if not event:
        return jsonify({'error': 'event not found'}), 404
    roads_path = _event_dir(event) / 'data_processed' / 'roads' / 'roads_clipped.gpkg'
    if not roads_path.exists():
        return jsonify({'type': 'FeatureCollection', 'features': []}), 200
    gdf = gpd.read_file(roads_path)
    return jsonify(json.loads(gdf.to_json())), 200


# ── Shared replay clock ──────────────────────────────────────────────────────

@events_bp.route('/<int:event_id>/replay-time', methods=['GET'])
@token_required
def get_replay_time(event_id: int):
    """Return the current shared virtual time (ms since epoch) for this event.

    In-memory cache is checked first (avoids a DB hit on every 10-second poll).
    Falls back to FireEvent.replay_ms so the value survives server restarts.
    """
    entry = _replay_times.get(event_id)
    if entry is None:
        from db.models import FireEvent
        event = FireEvent.query.get(event_id)
        if event and event.replay_ms is not None:
            import time as _time
            entry = {'ms': event.replay_ms, 'pushed_at': _time.time() * 1000, 'speed': 1}
            _replay_times[event_id] = entry
    return jsonify(entry or {}), 200


@events_bp.route('/<int:event_id>/replay-time', methods=['POST'])
@token_required
def set_replay_time(event_id: int):
    """Admin only — set the shared virtual time for this event."""
    import jwt as _jwt
    import os
    auth = request.headers.get('Authorization', '')
    try:
        payload = _jwt.decode(auth.split(' ')[1],
                              os.getenv('SECRET_KEY', 'wildfire-secret-key-change-in-production'),
                              algorithms=['HS256'])
        if not payload.get('is_admin'):
            return jsonify({'error': 'admin only'}), 403
    except Exception:
        return jsonify({'error': 'unauthorized'}), 401

    import time as _time
    data = request.get_json(force=True) or {}
    ms = data.get('ms')
    if not isinstance(ms, (int, float)):
        return jsonify({'error': 'ms required'}), 400
    _replay_times[event_id] = {
        'ms':        int(ms),
        'pushed_at': _time.time() * 1000,
        'speed':     data.get('speed', 1),
    }

    # Persist to DB so the value survives server restarts
    from db.connection import db
    from db.models import FireEvent
    event = FireEvent.query.get(event_id)
    if event:
        event.replay_ms = int(ms)
        db.session.commit()

    # Trigger prediction builds for the current timestep and the next 2 upcoming
    # ones so they're ready before the clock reaches them.
    _trigger_upcoming_predictions(event_id, int(ms))

    return jsonify({'ok': True}), 200


def _trigger_upcoming_predictions(event_id: int, current_ms: int, lookahead: int = 2) -> None:
    """Fire-and-forget: start prediction builds for timesteps at/near current_ms.

    Finds the closest timestep to current_ms plus the next `lookahead` slots,
    and calls run-prediction on each if they are still pending.
    """
    try:
        import pandas as pd
        from flask import current_app
        from db.models import EventTimestep, FireEvent
        from pipeline.check.builder import build_single_timestep_ondemand
        from utils.background import run_in_background
        from api.timesteps import _ts_base, _read_status, _write_status

        event = FireEvent.query.get(event_id)
        if not event:
            return

        current_ts = pd.Timestamp(current_ms, unit='ms')
        rows = (
            EventTimestep.query
            .filter_by(event_id=event_id)
            .order_by(EventTimestep.slot_time)
            .all()
        )
        if not rows:
            return

        # Find the index of the slot closest to current_ms
        closest_idx = min(
            range(len(rows)),
            key=lambda i: abs((pd.Timestamp(rows[i].slot_time) - current_ts).total_seconds())
        )

        app = current_app._get_current_object()
        for i in range(closest_idx, min(closest_idx + lookahead + 1, len(rows))):
            ts = rows[i]
            ml_dir = _ts_base(event.id, event.year, ts.slot_time) / "prediction" / "ML"
            from pipeline.check.builder_slots import _read_status as _bs_read, _write_status as _bs_write
            st = _bs_read(ml_dir)
            if st == "pending":
                _bs_write(ml_dir, "running")
                run_in_background(build_single_timestep_ondemand, app, ts.id)
    except Exception:
        pass  # never crash the endpoint
