"""
api/ts_prediction_routes.py
-----------------------------
Prediction-layer endpoints (all read-only, return pre-built GeoJSON).

Routes (on timesteps_bp):
    GET  /events/<id>/timesteps/<ts_id>/perimeter
    GET  /events/<id>/timesteps/<ts_id>/hotspots
    GET  /events/<id>/timesteps/<ts_id>/risk-zones
    GET  /events/<id>/timesteps/<ts_id>/risk-zones-wind
    GET  /events/<id>/timesteps/<ts_id>/actual-perimeter
    GET  /events/<id>/timesteps/<ts_id>/fire-context
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from flask import Response, jsonify, request
from utils.auth_middleware import token_required

from api.timesteps import (
    timesteps_bp, _get_event_and_ts,
    _pred_dir, _perim_dir, _hotspot_dir, _actual_perim_dir,
    _read_geojson, _read_json,
)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/perimeter", methods=["GET"])
@token_required
def get_perimeter(event_id: int, ts_id: int):
    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result
    perim_dir = _perim_dir(event.id, event.year, ts.slot_time)
    crowd = request.args.get("crowd", "false").lower() == "true"
    if crowd:
        crowd_path = perim_dir / "perimeter_crowd.geojson"
        if crowd_path.exists():
            return jsonify(_read_geojson(crowd_path)), 200
    return jsonify(_read_geojson(perim_dir / "perimeter.geojson")), 200


@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/hotspots", methods=["GET"])
@token_required
def get_hotspots(event_id: int, ts_id: int):
    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result
    crowd = request.args.get("crowd") == "true"
    fname = "hotspots_crowd.geojson" if crowd else "hotspots.geojson"
    return jsonify(_read_geojson(_hotspot_dir(event.id, event.year, ts.slot_time) / fname)), 200


@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/risk-zones", methods=["GET"])
@token_required
def get_risk_zones(event_id: int, ts_id: int):
    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result
    crowd = request.args.get("crowd") == "true"
    if crowd:
        from api.timesteps import _ts_base
        pred = _ts_base(event.id, event.year, ts.slot_time) / "prediction" / "ML_crowd"
    else:
        pred = _pred_dir(event.id, event.year, ts.slot_time)
    features = []
    for h in ("3h", "6h", "12h"):
        fc = _read_geojson(pred / f"risk_zones_{h}.geojson")
        features.extend(fc.get("features", []))
    return jsonify({"type": "FeatureCollection", "features": features}), 200


@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/risk-zones-wind", methods=["GET"])
@token_required
def get_risk_zones_wind(event_id: int, ts_id: int):
    """Wind-driven risk zones rooted at T1 hotspot boundary (WHP-inspired)."""
    import shapely.geometry as sg
    import shapely.ops as so
    import shapely.affinity as sa
    from pyproj import Transformer

    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result

    pred     = _pred_dir(event.id, event.year, ts.slot_time)
    hs_dir   = _hotspot_dir(event.id, event.year, ts.slot_time)
    perim_d  = _perim_dir(event.id, event.year, ts.slot_time)
    ctx      = _read_json(pred / "fire_context.json")
    if not ctx:
        return jsonify({"type": "FeatureCollection", "features": []}), 200

    # Wind: prefer wind_forecast max, fall back to weather_t1
    wind_forecast = ctx.get("wind_forecast") or []
    if not wind_forecast:
        wx         = ctx.get("weather_t1") or {}
        wind_speed = wx.get("wind_speed_kmh") or 0
        wind_dir   = wx.get("wind_dir") or 0
    else:
        best       = max(wind_forecast, key=lambda f: f.get("max_wind_speed_kmh") or f.get("wind_speed_kmh") or 0)
        wind_speed = best.get("max_wind_speed_kmh") or best.get("wind_speed_kmh") or 0
        wind_dir   = best.get("wind_dir") or 0

    if wind_speed == 0:
        return jsonify({"type": "FeatureCollection", "features": []}), 200

    # Seed shape from T1 hotspot convex hull; fall back to perimeter centroid
    hotspots  = _read_geojson(hs_dir / "hotspots.geojson")
    pts_wgs84 = []
    for f in (hotspots.get("features") or []):
        try:
            pts_wgs84.append(sg.shape(f["geometry"]))
        except Exception:
            pass
    if not pts_wgs84:
        perim = _read_geojson(perim_d / "perimeter.geojson")
        for f in (perim.get("features") or []):
            try:
                pts_wgs84.append(sg.shape(f["geometry"]).centroid)
            except Exception:
                pass
    if not pts_wgs84:
        return jsonify({"type": "FeatureCollection", "features": []}), 200

    # Project to EPSG:3978 (metres)
    wkt_3978 = (
        'PROJCS["NAD83 / Canada Atlas Lambert",'
        'GEOGCS["NAD83",DATUM["North_American_Datum_1983",'
        'SPHEROID["GRS 1980",6378137,298.257222101]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],'
        'PROJECTION["Lambert_Conformal_Conic"],'
        'PARAMETER["latitude_of_origin",49],PARAMETER["central_meridian",-95],'
        'PARAMETER["standard_parallel_1",49],PARAMETER["standard_parallel_2",77],'
        'PARAMETER["false_easting",0],PARAMETER["false_northing",0],UNIT["metre",1]]'
    )
    to_proj  = Transformer.from_crs("EPSG:4326", wkt_3978, always_xy=True)
    to_wgs84 = Transformer.from_crs(wkt_3978, "EPSG:4326", always_xy=True)

    VIIRS_BUF_M = 500.0
    pts_proj    = [sg.Point(*to_proj.transform(p.x, p.y)) for p in pts_wgs84]

    if len(pts_proj) >= 3:
        seed = so.unary_union([p.buffer(VIIRS_BUF_M) for p in pts_proj]).convex_hull
    elif len(pts_proj) == 2:
        seed = so.unary_union([p.buffer(VIIRS_BUF_M) for p in pts_proj])
    else:
        seed = pts_proj[0].buffer(VIIRS_BUF_M * 2)

    angle_rad = math.radians(90.0 - wind_dir)
    dx_unit   = math.cos(angle_rad)
    dy_unit   = math.sin(angle_rad)

    features = []
    for label, h, color, fill_opacity in [("3h", 3, "#ffcc00", 0.25), ("6h", 6, "#ff8800", 0.20), ("12h", 12, "#ff3300", 0.15)]:
        spread_m  = wind_speed * (h / 24.0) * 1000.0
        cross_m   = spread_m * 0.4
        shifted   = sa.translate(seed, xoff=spread_m * dx_unit, yoff=spread_m * dy_unit)
        zone      = so.unary_union([seed, shifted.buffer(cross_m)]).convex_hull
        coords    = list(zone.exterior.coords)
        xs, ys    = [p[0] for p in coords], [p[1] for p in coords]
        lons, lats = to_wgs84.transform(xs, ys)
        features.append({
            "type": "Feature",
            "properties": {"horizon": label, "color": color, "fill_opacity": fill_opacity,
                           "spread_km": round(spread_m / 1000.0, 1),
                           "wind_speed_kmh": wind_speed, "wind_dir": wind_dir},
            "geometry": {"type": "Polygon", "coordinates": [list(zip(lons, lats))]},
        })

    return jsonify({"type": "FeatureCollection", "features": features}), 200


@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/actual-perimeter", methods=["GET"])
@token_required
def get_actual_perimeter(event_id: int, ts_id: int):
    """Return ROS-weighted actual perimeters for +0h/+3h/+6h/+12h (pre-built at startup)."""
    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result

    ap_dir   = _actual_perim_dir(event.id, event.year, ts.slot_time)
    colors   = {"0h": "#ffffff", "3h": "#a0c4ff", "6h": "#74b9ff", "12h": "#0984e3"}
    features = []

    for h in (0, 3, 6, 12):
        path = ap_dir / f"{h}h.geojson"
        if not path.exists():
            continue
        try:
            fc = json.loads(path.read_text(encoding="utf-8"))
            for f in fc.get("features", []):
                f.setdefault("properties", {})["color"] = colors.get(f"{h}h", "#888")
                features.append(f)
        except Exception:
            pass

    return jsonify({"type": "FeatureCollection", "features": features}), 200


@timesteps_bp.route("/events/<int:event_id>/timesteps/<int:ts_id>/fire-context", methods=["GET"])
@token_required
def get_fire_context(event_id: int, ts_id: int):
    result, err = _get_event_and_ts(event_id, ts_id)
    if err:
        return err
    event, ts = result
    path = _pred_dir(event.id, event.year, ts.slot_time) / "fire_context.json"
    if not path.exists():
        return jsonify({}), 200
    text = re.sub(r'\bNaN\b', 'null', path.read_text(encoding="utf-8"))
    return Response(text, mimetype="application/json"), 200
