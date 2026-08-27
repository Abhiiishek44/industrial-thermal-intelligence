import json
from datetime import datetime, timedelta, timezone
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

    config = get_event_config(e)
    bounds = config.view_bbox or to_shape(e.bbox).bounds
    data_ready = True
    if config.analysis_mode == "thermal_monitoring":
        data_ready = (
            _event_dir(e) / "data_processed/thermal/classification_metadata.json"
        ).exists()
    return {
        'id':          e.id,
        'name':        e.name,
        'year':        e.year,
        'start_date':  e.start_date.isoformat() if e.start_date else None,
        'end_date':    e.end_date.isoformat() if e.end_date else None,
        'description': e.description,
        'bbox':        list(bounds),  # [minLon, minLat, maxLon, maxLat]
        'analysis_mode': config.analysis_mode,
        'region_id': config.region_id,
        'default_view_days': config.default_view_days,
        'monitoring_focus': config.monitoring_focus,
        'state': config.state,
        'data_ready': data_ready,
    }


@events_bp.route('/', methods=['GET'])
def list_events():
    from pipeline.event_config import is_public_event

    events = [
        event for event in FireEvent.query.order_by(FireEvent.year.desc()).all()
        if is_public_event(event)
    ]
    return jsonify([_serialize(e) for e in events]), 200


def _india_thermal_overview(events) -> dict:
    """Merge each public India region's prepared source layer for the home map."""
    from pipeline.event_config import is_public_event

    features = []
    per_region = []
    class_counts = {'industrial': 0, 'natural': 0, 'unknown': 0}
    focus_counts = {'industrial': 0, 'forest': 0}
    observation_count = 0

    for event in events:
        if not is_public_event(event):
            continue
        serialized = _serialize(event)
        path = _event_dir(event) / 'data_processed/thermal/classified_sources.geojson'
        region_counts = {'industrial': 0, 'natural': 0, 'unknown': 0}
        region_observations = 0
        region_features = []
        if path.exists():
            payload = json.loads(path.read_text(encoding='utf-8'))
            region_features = _filter_geojson_bbox(payload, serialized['bbox']).get('features', [])

        for feature in region_features:
            properties = dict(feature.get('properties') or {})
            source_class = str(properties.get('source_class') or 'unknown').lower()
            if source_class not in class_counts:
                source_class = 'unknown'
            detections = int(properties.get('raw_observation_count') or 0)
            class_counts[source_class] += 1
            region_counts[source_class] += 1
            observation_count += detections
            region_observations += detections
            properties.update({
                'event_id': event.id,
                'region_id': serialized['region_id'],
                'region_name': event.name,
                'state': serialized['state'],
                'monitoring_focus': serialized['monitoring_focus'],
            })
            features.append({**feature, 'properties': properties})

        focus = serialized['monitoring_focus'] or 'industrial'
        focus_counts[focus] = focus_counts.get(focus, 0) + 1
        per_region.append({
            'event_id': event.id,
            'region_id': serialized['region_id'],
            'name': event.name,
            'state': serialized['state'],
            'monitoring_focus': focus,
            'source_count': len(region_features),
            'observation_count': region_observations,
            'class_counts': region_counts,
            'data_ready': serialized['data_ready'],
        })

    return {
        'type': 'FeatureCollection',
        'features': features,
        'metadata': {
            'scope': 'India',
            'region_count': len(per_region),
            'source_count': len(features),
            'observation_count': observation_count,
            'class_counts': class_counts,
            'focus_counts': focus_counts,
            'regions': per_region,
        },
    }


@events_bp.route('/thermal/overview', methods=['GET'])
def get_india_thermal_overview():
    """Return one combined industrial-and-forest monitoring layer for India."""
    events = FireEvent.query.order_by(FireEvent.id.asc()).all()
    return jsonify(_india_thermal_overview(events)), 200


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
    from pipeline.event_config import get_event_config
    from shapely.geometry import box

    config = get_event_config(event)
    shape = box(*(config.view_bbox or to_shape(event.bbox).bounds))
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


def _parse_utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _filter_thermal_features(
    payload: dict,
    days: int,
    end: datetime,
    *,
    timestamp_property: str = 'observed_at',
) -> tuple[list, datetime]:
    """Select observations in an inclusive N-day activity window."""
    start = end - timedelta(days=days - 1)
    selected = []
    for feature in payload.get('features', []):
        observed_at = (feature.get('properties') or {}).get(timestamp_property)
        if not observed_at:
            continue
        try:
            observed = _parse_utc_timestamp(str(observed_at))
        except ValueError:
            continue
        if start <= observed <= end:
            selected.append(feature)
    return selected, start


def _filter_geojson_bbox(payload: dict, bbox) -> dict:
    """Keep point features inside a dashboard's focused monitoring extent."""
    if not bbox:
        return payload
    min_lon, min_lat, max_lon, max_lat = bbox
    selected = []
    for feature in payload.get('features', []):
        geometry = feature.get('geometry') or {}
        coordinates = geometry.get('coordinates') or []
        if geometry.get('type') != 'Point' or len(coordinates) < 2:
            continue
        lon, lat = coordinates[:2]
        if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat:
            selected.append(feature)
    return {**payload, 'features': selected}


def _windowed_persistent_sources(event, days: int, end: datetime):
    """Build causal persistent sources from detections available by ``end``."""
    import pandas as pd
    from pipeline.thermal import build_persistent_sources, get_persistence_settings

    detections_path = (
        _event_dir(event) / 'data_processed/thermal/detections_aggregated.parquet'
    )
    if not detections_path.exists():
        return pd.DataFrame(), end - timedelta(days=days - 1)
    start = end - timedelta(days=days - 1)
    detections = pd.read_parquet(detections_path)
    from pipeline.event_config import get_event_config

    display_bbox = get_event_config(event).view_bbox
    if display_bbox and {'longitude', 'latitude'}.issubset(detections.columns):
        min_lon, min_lat, max_lon, max_lat = display_bbox
        detections = detections[
            detections['longitude'].between(min_lon, max_lon)
            & detections['latitude'].between(min_lat, max_lat)
        ].copy()
    observed_at = pd.to_datetime(detections['observed_at'], utc=True)
    window = detections[(observed_at >= start) & (observed_at <= end)].copy()
    settings = get_persistence_settings()
    clusters = build_persistent_sources(
        window,
        radius_m=settings['cluster_radius_m'],
        min_observations=settings['cluster_min_observations'],
    )
    return clusters, start


@events_bp.route('/<int:event_id>/thermal/detections', methods=['GET'])
@token_required
def get_enriched_thermal_detections(event_id: int):
    """Return enriched FIRMS observations, optionally filtered to a time window."""
    event, error = _thermal_event_or_error(event_id)
    if error:
        return error

    thermal_dir = _event_dir(event) / 'data_processed/thermal'
    aggregated_path = thermal_dir / 'detections_aggregated.geojson'
    path = aggregated_path if aggregated_path.exists() else thermal_dir / 'firms_enriched.geojson'
    if not path.exists():
        return jsonify({'type': 'FeatureCollection', 'features': []}), 200
    payload = json.loads(path.read_text(encoding='utf-8'))
    from pipeline.event_config import get_event_config

    payload = _filter_geojson_bbox(payload, get_event_config(event).view_bbox)

    days_value = request.args.get('days')
    end_value = request.args.get('end')
    if days_value is None and end_value is None:
        return jsonify(payload), 200
    try:
        days = int(days_value or '5')
        if days not in (5, 30):
            raise ValueError
        end = _parse_utc_timestamp(end_value) if end_value else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return jsonify({
            'error': 'days must be 5 or 30 and end must be an ISO-8601 timestamp',
        }), 400

    selected, start = _filter_thermal_features(payload, days, end)

    return jsonify({
        'type': 'FeatureCollection',
        'features': selected,
        'metadata': {
            'view': f'{days}d',
            'days': days,
            'start': start.isoformat(),
            'end': end.isoformat(),
            'detection_count': len(selected),
            'raw_observation_count': sum(
                int((feature.get('properties') or {}).get('raw_observation_count') or 1)
                for feature in selected
            ),
            'representation': 'multi_sensor_aggregated' if path == aggregated_path else 'raw_observations',
        },
    }), 200


@events_bp.route('/<int:event_id>/thermal/persistent', methods=['GET'])
@events_bp.route('/<int:event_id>/thermal/clusters', methods=['GET'])
@token_required
def get_persistent_thermal_sources(event_id: int):
    """Return recurring spatial clusters active in the requested window."""
    event, error = _thermal_event_or_error(event_id)
    if error:
        return error
    try:
        days = int(request.args.get('days', '30'))
        if days not in (5, 30):
            raise ValueError
        end_value = request.args.get('end')
        end = _parse_utc_timestamp(end_value) if end_value else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return jsonify({
            'error': 'days must be 5 or 30 and end must be an ISO-8601 timestamp',
        }), 400

    from pipeline.thermal import thermal_frame_to_geojson

    clusters, start = _windowed_persistent_sources(event, days, end)
    payload = thermal_frame_to_geojson(clusters)
    selected = payload['features']
    level_counts = {}
    for feature in selected:
        level = str((feature.get('properties') or {}).get('persistence_level') or 'UNKNOWN')
        level_counts[level] = level_counts.get(level, 0) + 1
    return jsonify({
        'type': 'FeatureCollection',
        'features': selected,
        'metadata': {
            'view': 'persistent',
            'days': days,
            'start': start.isoformat(),
            'end': end.isoformat(),
            'persistent_source_count': len(selected),
            'persistence_level_counts': level_counts,
        },
    }), 200


@events_bp.route('/<int:event_id>/thermal/classifications', methods=['GET'])
@events_bp.route('/<int:event_id>/thermal/classified-sources', methods=['GET'])
@token_required
def get_thermal_classifications(event_id: int):
    """Return causal explainable classifications for persistent sources."""
    event, error = _thermal_event_or_error(event_id)
    if error:
        return error
    try:
        days = int(request.args.get('days', '30'))
        if days not in (5, 30):
            raise ValueError
        end_value = request.args.get('end')
        end = _parse_utc_timestamp(end_value) if end_value else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return jsonify({
            'error': 'days must be 5 or 30 and end must be an ISO-8601 timestamp',
        }), 400

    from pipeline.thermal import (
        classification_metadata,
        classify_persistent_sources,
        thermal_frame_to_geojson,
    )

    clusters, start = _windowed_persistent_sources(event, days, end)
    classified = classify_persistent_sources(clusters)
    payload = thermal_frame_to_geojson(classified)
    metadata = classification_metadata(event.id, classified)
    return jsonify({
        **payload,
        'metadata': {
            **metadata,
            'view': 'classification',
            'days': days,
            'start': start.isoformat(),
            'end': end.isoformat(),
        },
    }), 200


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
