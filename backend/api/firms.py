"""
api/firms.py
------------
Real-time NASA FIRMS hotspot endpoint.

Route:
    GET /api/firms/realtime          → past 24 h hotspots (Canada bbox)
    GET /api/firms/realtime?hours=48 → past 48 h  (max 5 days = 120 h)

Response: GeoJSON FeatureCollection
Cache: in-memory, refreshed every CACHE_TTL_S seconds (default 3600 = 1 h)
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Blueprint, jsonify, request
from utils.auth_middleware import token_required

firms_bp = Blueprint("firms", __name__)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

_FIRMS_BASE       = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
_FIRMS_STATUS_URL = "https://firms.modaps.eosdis.nasa.gov/mapserver/mapkey_status/?MAP_KEY={key}"
_SOURCES     = [
    "VIIRS_SNPP_NRT",    # Suomi-NPP
    "VIIRS_NOAA20_NRT",  # NOAA-20
    "VIIRS_NOAA21_NRT",  # NOAA-21
    "MODIS_NRT",         # Terra/Aqua (1 km, broader coverage)
]
_CANADA_BBOX  = "-141.0,41.7,-52.6,83.1"  # minLon,minLat,maxLon,maxLat
_DEFAULT_DAYS = 5
_CACHE_TTL_S  = 3600   # NASA FIRMS NRT refreshes ~hourly; match that cadence

_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"

# ── File-based cache ──────────────────────────────────────────────────────────

def _cache_path(key: str) -> Path:
    return _CACHE_DIR / f"{key}.json"


def _cached(key: str):
    path = _cache_path(key)
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > _CACHE_TTL_S:
        log.info("[firms] cache stale (%s) — will refetch", path.name)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _store(key: str, data: dict):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(key).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── FIRMS fetch ───────────────────────────────────────────────────────────────

def _fetch_source(api_key: str, source: str, day_range: int) -> dict:
    """Fetch one satellite source, using file cache. Returns GeoJSON FeatureCollection."""
    cache_key = f"firms_{source}_{day_range}"
    cached = _cached(cache_key)
    if cached:
        log.debug("[firms] cache hit %s day_range=%d", source, day_range)
        return cached

    url = f"{_FIRMS_BASE}/{api_key}/{source}/{_CANADA_BBOX}/{day_range}"
    log.info("[firms] fetching %s (day_range=%d)", source, day_range)
    try:
        r = requests.get(url, timeout=30)
        if not r.ok:
            log.error("[firms] %s HTTP %d: %s", source, r.status_code, r.text[:300])
            r.raise_for_status()
    except Exception as e:
        log.error("[firms] %s request failed: %s", source, e)
        return _empty_fc()

    result = _csv_to_geojson(r.text)
    result["source"] = source
    log.info("[firms] %s — %d features", source, result["count"])
    _store(cache_key, result)
    return result


def _check_key_status(api_key: str) -> bool:
    """Return True if the key is valid and has transactions remaining."""
    url = _FIRMS_STATUS_URL.format(key=api_key)
    try:
        r = requests.get(url, timeout=10)
        if not r.ok:
            log.error("[firms] key status check failed HTTP %d: %s", r.status_code, r.text[:200])
            return False
        data = r.json()
        used  = data.get("current_transactions", 0)
        limit = data.get("transaction_limit", 0)
        log.info("[firms] key status: %d / %d transactions used", used, limit)
        if limit > 0 and used >= limit:
            log.error("[firms] transaction limit reached (%d / %d)", used, limit)
            return False
        return True
    except Exception as e:
        log.error("[firms] key status check error: %s", e)
        return False


def _fetch_firms(day_range: int) -> dict:
    api_key = os.environ.get("FIRMS_API_KEY", "")
    if not api_key:
        log.error("[firms] FIRMS_API_KEY not set")
        return _empty_fc()

    if not _check_key_status(api_key):
        return _empty_fc()

    all_features = []
    for source in _SOURCES:
        fc = _fetch_source(api_key, source, day_range)
        all_features.extend(fc.get("features", []))

    return {
        "type":       "FeatureCollection",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count":      len(all_features),
        "features":   all_features,
    }


def _csv_to_geojson(text: str) -> dict:
    features = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        try:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
        except (KeyError, ValueError):
            continue

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "frp":        _safe_float(row.get("frp")),
                "confidence": row.get("confidence", ""),
                "bright_ti4": _safe_float(row.get("bright_ti4")),
                "acq_date":   row.get("acq_date", ""),
                "acq_time":   row.get("acq_time", ""),
                "satellite":  row.get("satellite", ""),
                "daynight":   row.get("daynight", ""),
            },
        })

    return {
        "type":       "FeatureCollection",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count":      len(features),
        "features":   features,
    }


def _safe_float(val):
    try:
        return round(float(val), 2)
    except (TypeError, ValueError):
        return None


def _empty_fc() -> dict:
    return {
        "type":       "FeatureCollection",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count":      0,
        "features":   [],
    }


# ── Route ─────────────────────────────────────────────────────────────────────

@firms_bp.route("/realtime", methods=["GET"])
@token_required
def get_realtime():
    """Serve FIRMS hotspots from JSON cache (fetch from NASA if cache missing).

    Query params:
        days (int, 1–5, default 5): lookback window in days
    """
    try:
        days = max(1, min(5, int(request.args.get("days", _DEFAULT_DAYS))))
    except ValueError:
        days = _DEFAULT_DAYS

    return jsonify(_fetch_firms(days)), 200


@firms_bp.route("/refresh", methods=["POST"])
@token_required
def refresh():
    """Force re-fetch from NASA FIRMS and overwrite cache files.

    Deletes existing cache JSONs for all sources, then fetches fresh data.
    """
    try:
        days = max(1, min(5, int(request.args.get("days", _DEFAULT_DAYS))))
    except ValueError:
        days = _DEFAULT_DAYS

    for source in _SOURCES:
        p = _cache_path(f"firms_{source}_{days}")
        if p.exists():
            p.unlink()
            log.info("[firms] cache cleared: %s", p.name)

    result = _fetch_firms(days)
    return jsonify({"refreshed": True, "count": result.get("count", 0)}), 200
