import json
from pathlib import Path

import geopandas as gpd
from flask import Blueprint, jsonify, request
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from db.models import FireEvent
from utils.auth_middleware import admin_required, token_required

# In-memory shared replay clock: {event_id: {ms, pushed_at, speed}}
_replay_times: dict[int, dict] = {}

events_bp = Blueprint('events', __name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "events"


def _event_dir(event) -> Path:
    return DATA_DIR / f"{event.year}_{event.id:04d}"


def _serialize(e):
    from pipeline.event_config import get_event_config

    bounds = to_shape(e.bbox).bounds  # (minx, miny, maxx, maxy)
    config = get_event_config(e)
    return {
        'id':          e.id,
        'name':        e.name,
        'year':        e.year,
        'start_date':  e.start_date.isoformat() if e.start_date else None,
        'end_date':    e.end_date.isoformat() if e.end_date else None,
        'description': e.description,
        'bbox':        list(bounds),  # [minLon, minLat, maxLon, maxLat]
        'analysis_mode': config.analysis_mode,
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


@events_bp.route('/<int:event_id>/thermal/history', methods=['GET'])
@token_required
def get_thermal_history(event_id: int):
    """Return availability and date coverage for normalized FIRMS history."""
    event = FireEvent.query.get(event_id)
    if not event:
        return jsonify({'error': 'event not found'}), 404

    from pipeline.event_config import THERMAL_MONITORING_MODE, get_event_config
    from pipeline.thermal import load_history_metadata

    config = get_event_config(event)
    if config.analysis_mode != THERMAL_MONITORING_MODE:
        return jsonify({
            'error': 'thermal history is not configured for this event',
        }), 409

    metadata = load_history_metadata(event)
    if metadata is None:
        history_dates = config.thermal_history_dates
        metadata = {
            'event_id': event.id,
            'data_available': False,
            'observation_count': 0,
            'first_observed_at': None,
            'last_observed_at': None,
            'requested_start_date': history_dates[0].isoformat() if history_dates else None,
            'requested_end_date': history_dates[1].isoformat() if history_dates else None,
        }
    return jsonify(metadata), 200


def _thermal_event_or_error(event_id: int):
    """Resolve a thermal event for context endpoints."""
    from pipeline.event_config import THERMAL_MONITORING_MODE, get_event_config

    event = FireEvent.query.get(event_id)
    if not event:
        return None, (jsonify({'error': 'event not found'}), 404)
    if get_event_config(event).analysis_mode != THERMAL_MONITORING_MODE:
        return None, (jsonify({
            'error': 'thermal context is not configured for this event',
        }), 409)
    return event, None


@events_bp.route('/<int:event_id>/thermal/context', methods=['GET'])
@token_required
def get_thermal_context(event_id: int):
    """Return source coverage and aggregate enrichment counts."""
    event, error = _thermal_event_or_error(event_id)
    if error:
        return error

    from pipeline.thermal import load_context_metadata

    metadata = load_context_metadata(event)
    if metadata is None:
        return jsonify({
            'event_id': event.id,
            'data_available': False,
            'observation_count': 0,
            'classification_available': False,
        }), 200
    return jsonify({**metadata, 'data_available': True}), 200


def _event_geojson(event, relative_path: str):
    path = _event_dir(event) / relative_path
    if not path.exists():
        return jsonify({'type': 'FeatureCollection', 'features': []}), 200
    return jsonify(json.loads(path.read_text(encoding='utf-8'))), 200


@events_bp.route('/<int:event_id>/thermal/detections', methods=['GET'])
@token_required
def get_enriched_thermal_detections(event_id: int):
    """Return FIRMS observations with descriptive industrial/land-cover context."""
    event, error = _thermal_event_or_error(event_id)
    if error:
        return error
    return _event_geojson(event, 'data_processed/thermal/firms_enriched.geojson')


@events_bp.route('/<int:event_id>/layers/midc', methods=['GET'])
@token_required
def get_midc_context(event_id: int):
    event, error = _thermal_event_or_error(event_id)
    if error:
        return error
    return _event_geojson(event, 'data_processed/industrial/midc_boundary.geojson')


@events_bp.route('/<int:event_id>/layers/industrial-areas', methods=['GET'])
@token_required
def get_industrial_areas(event_id: int):
    event, error = _thermal_event_or_error(event_id)
    if error:
        return error
    return _event_geojson(event, 'data_processed/industrial/industrial_areas.geojson')


@events_bp.route('/<int:event_id>/layers/industrial-facilities', methods=['GET'])
@token_required
def get_industrial_facilities(event_id: int):
    event, error = _thermal_event_or_error(event_id)
    if error:
        return error
    return _event_geojson(event, 'data_processed/industrial/facilities.geojson')


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
@admin_required
def set_replay_time(event_id: int):
    """Admin only — set the shared virtual time for this event."""
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

    return jsonify({'ok': True}), 200
