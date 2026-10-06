"""Rolling-window APIs for durable near-real-time FIRMS detections and events."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timezone

from flask import Blueprint, jsonify, request

from db.connection import db
from db.models import FireAlert, FireDetection, FirmsSyncState, MonitoringFireEvent
from services.firms_ingestion_service import (
    ALLOWED_WINDOW_HOURS,
    get_ingestion_settings,
    rolling_cutoff,
    utc_now,
)


fires_bp = Blueprint("fires", __name__)


def _hours() -> int:
    try:
        hours = int(request.args.get("hours", "24"))
    except (TypeError, ValueError):
        hours = 24
    if hours not in ALLOWED_WINDOW_HOURS:
        raise ValueError(f"hours must be one of {ALLOWED_WINDOW_HOURS}")
    return hours


def _region_event_id() -> int | None:
    raw = request.args.get("region_event_id")
    if raw in (None, ""):
        return None
    try:
        region_event_id = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("region_event_id must be a positive integer") from error
    if region_event_id <= 0:
        raise ValueError("region_event_id must be a positive integer")
    return region_event_id


def _iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _detection_payload(item: FireDetection) -> dict:
    return {
        "id": item.id,
        "detection_key": item.detection_key,
        "fire_event_id": item.fire_event_id,
        "monitoring_region_id": item.monitoring_region_id,
        "latitude": item.latitude,
        "longitude": item.longitude,
        "acquisition_time": _iso(item.acquisition_time),
        "received_at": _iso(item.received_at),
        "data_latency_seconds": item.data_latency_seconds,
        "satellite": item.satellite,
        "instrument": item.instrument,
        "source_product": item.source_product,
        "brightness": item.brightness,
        "bright_ti4": item.bright_ti4,
        "bright_ti5": item.bright_ti5,
        "frp": item.frp,
        "firms_confidence": item.firms_confidence,
        "day_night": item.day_night,
        "land_cover": item.land_cover,
        "landcover_group": item.landcover_group,
        "inside_industrial_polygon": item.inside_industrial_polygon,
        "near_industrial_facility": item.near_industrial_facility,
        "predicted_class": item.predicted_class,
        "model_confidence": item.model_confidence,
        "model_version": item.model_version,
        "alert_status": item.alert_status,
    }


def _event_payload(
    item: MonitoringFireEvent,
    detections: list[FireDetection] | None = None,
) -> dict:
    detections = detections or []
    landcover_counts = Counter(
        detection.landcover_group or "unknown" for detection in detections
    )
    payload = {
        "id": item.id,
        "event_key": item.event_key,
        "monitoring_region_id": item.monitoring_region_id,
        "centroid_latitude": item.centroid_latitude,
        "centroid_longitude": item.centroid_longitude,
        "first_detected_at": _iso(item.first_detected_at),
        "last_detected_at": _iso(item.last_detected_at),
        "status": item.status,
        "detection_count": item.detection_count,
        "max_frp": item.max_frp,
        "average_frp": item.average_frp,
        "frp": item.max_frp,
        "mean_frp": item.average_frp,
        "predicted_class": item.predicted_class,
        "source_class": item.predicted_class,
        "model_confidence": item.confidence,
        "alert_status": item.alert_status,
        "alert_sent_at": _iso(item.alert_sent_at),
        "inside_industrial_area_count": sum(
            detection.inside_industrial_polygon is True for detection in detections
        ),
        "near_industrial_facility_count": sum(
            detection.near_industrial_facility is True for detection in detections
        ),
        "landcover_group_counts": dict(landcover_counts),
    }
    return payload


@fires_bp.errorhandler(ValueError)
def _invalid_query(error):
    return jsonify({"error": str(error)}), 400


@fires_bp.get("/recent")
def recent_detections():
    hours = _hours()
    region_event_id = _region_event_id()
    cutoff = rolling_cutoff(hours)
    query = FireDetection.query.filter(
        FireDetection.acquisition_time >= cutoff
    )
    if region_event_id is not None:
        query = query.filter(FireDetection.monitoring_region_id == region_event_id)
    rows = query.order_by(FireDetection.acquisition_time.desc()).all()
    return jsonify({
        "hours": hours,
        "region_event_id": region_event_id,
        "cutoff": _iso(cutoff),
        "count": len(rows),
        "detections": [_detection_payload(row) for row in rows],
    })


@fires_bp.get("/detections/<int:detection_id>")
def detection_detail(detection_id: int):
    row = db.session.get(FireDetection, detection_id)
    if row is None:
        return jsonify({"error": "fire detection not found"}), 404
    return jsonify(_detection_payload(row))


@fires_bp.get("/events")
def recent_events():
    hours = _hours()
    region_event_id = _region_event_id()
    cutoff = rolling_cutoff(hours)
    query = MonitoringFireEvent.query.filter(
        MonitoringFireEvent.last_detected_at >= cutoff
    )
    if region_event_id is not None:
        query = query.filter(
            MonitoringFireEvent.monitoring_region_id == region_event_id
        )
    rows = query.order_by(MonitoringFireEvent.last_detected_at.desc()).all()
    detections_by_event: dict[int, list[FireDetection]] = defaultdict(list)
    if rows:
        event_ids = [row.id for row in rows]
        detections = FireDetection.query.filter(
            FireDetection.fire_event_id.in_(event_ids),
            FireDetection.acquisition_time >= cutoff,
        ).all()
        for detection in detections:
            detections_by_event[detection.fire_event_id].append(detection)
    features = [{
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [row.centroid_longitude, row.centroid_latitude],
        },
        "properties": _event_payload(row, detections_by_event[row.id]),
    } for row in rows]
    return jsonify({
        "type": "FeatureCollection",
        "hours": hours,
        "region_event_id": region_event_id,
        "cutoff": _iso(cutoff),
        "count": len(features),
        "features": features,
    })


@fires_bp.get("/events/<int:event_id>")
def event_detail(event_id: int):
    event = db.session.get(MonitoringFireEvent, event_id)
    if event is None:
        return jsonify({"error": "fire event not found"}), 404
    detections = FireDetection.query.filter_by(
        fire_event_id=event.id
    ).order_by(FireDetection.acquisition_time.desc()).all()
    payload = _event_payload(event, detections)
    payload["detections"] = [_detection_payload(row) for row in detections]
    payload["alerts"] = [{
        "id": alert.id,
        "alert_type": alert.alert_type,
        "severity": alert.severity,
        "message": alert.message,
        "created_at": _iso(alert.created_at),
    } for alert in FireAlert.query.filter_by(fire_event_id=event.id).all()]
    return jsonify(payload)


@fires_bp.get("/status")
def status():
    settings = get_ingestion_settings()
    sync = db.session.get(FirmsSyncState, 1)
    last_observed = db.session.query(db.func.max(FireDetection.acquisition_time)).scalar()
    return jsonify({
        "status": sync.status if sync else "never",
        "last_successful_sync": _iso(sync.last_successful_sync) if sync else None,
        "last_attempt_at": _iso(sync.last_attempt_at) if sync else None,
        "last_observed_at": _iso(last_observed),
        "poll_interval_minutes": settings["poll_interval_minutes"],
        "model_version": settings["model_version"],
        "sources": list(settings["sources"]),
        "server_time": _iso(utc_now()),
        "records_received": sync.records_received if sync else 0,
        "new_records": sync.new_records if sync else 0,
        "duplicates": sync.duplicates if sync else 0,
        "prediction_failures": sync.prediction_failures if sync else 0,
        "error": sync.error if sync else None,
    })
