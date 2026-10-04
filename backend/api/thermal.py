"""Risk-first APIs backed by prepared thermal intelligence artifacts."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from services.thermal_intelligence import thermal_intelligence


thermal_bp = Blueprint("thermal", __name__)


def _bounded_int(name: str, default: int, *, minimum: int = 0, maximum: int = 2000) -> int:
    try:
        return max(minimum, min(maximum, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


def _optional_year():
    value = request.args.get("year")
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _optional_bool(name: str):
    value = request.args.get(name)
    if value is None or value == "":
        return None
    if value.lower() in {"1", "true", "yes"}:
        return True
    if value.lower() in {"0", "false", "no"}:
        return False
    return None


def _risk_levels(default: str | None = None):
    value = request.args.get("risk_level", default)
    return [item.strip() for item in value.split(",") if item.strip()] if value else None


def _filters(default_risk: str | None = None):
    return {
        "risk_levels": _risk_levels(default_risk),
        "confidence": request.args.get("confidence"),
        "region": request.args.get("region"),
        "year": _optional_year(),
        "iforest_agreement": _optional_bool("iforest_agreement"),
        "data_mode": request.args.get("data_mode"),
    }


@thermal_bp.errorhandler(FileNotFoundError)
def _artifact_missing(error):
    return jsonify({"error": str(error), "data_available": False}), 503


@thermal_bp.errorhandler(ValueError)
def _artifact_invalid(error):
    return jsonify({"error": str(error), "data_available": False}), 503


@thermal_bp.get("/stats")
def get_stats():
    return jsonify(thermal_intelligence.stats(
        region=request.args.get("region"), year=_optional_year(),
        data_mode=request.args.get("data_mode"),
    ))


@thermal_bp.get("/events")
def get_events():
    payload = thermal_intelligence.list_events(
        limit=_bounded_int("limit", 100),
        offset=_bounded_int("offset", 0, maximum=1_000_000),
        **_filters(),
    )
    return jsonify(payload)


@thermal_bp.get("/events/<source_event_id>")
def get_event(source_event_id: str):
    event = thermal_intelligence.event_detail(source_event_id)
    if event is None:
        return jsonify({"error": "thermal event not found"}), 404
    return jsonify(event)


@thermal_bp.get("/map")
def get_map_events():
    # Risk-first demo default: do not ship thousands of historical low events.
    payload = thermal_intelligence.map_events(
        limit=_bounded_int("limit", 1000),
        **_filters(default_risk="high,critical"),
    )
    return jsonify(payload)


@thermal_bp.get("/sources")
def get_sources():
    payload = thermal_intelligence.list_sources(
        region=request.args.get("region"),
        limit=_bounded_int("limit", 500),
        offset=_bounded_int("offset", 0, maximum=1_000_000),
    )
    return jsonify(payload)
