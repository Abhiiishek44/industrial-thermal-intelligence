"""
pipeline/predict/prediction.py
-------------------------------
Stage 2 (ML): Run ML prediction for one timestep → write GeoJSON files.

Outputs (under out_dir = prediction/ML/):
    risk_zones_3h.geojson
    risk_zones_6h.geojson
    risk_zones_12h.geojson
    fire_context.json

Perimeter and hotspots are written by Stage 1 (_run_perimeter_stage)
to perimeter/perimeter.geojson and hotspot/hotspots.geojson.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from pipeline.predict.risk_zones import build_risk_geojson, write_geojson

log = logging.getLogger(__name__)

_HORIZONS = [3, 6, 12]


def run_prediction(
    ts_id:      int,
    overpass_time: pd.Timestamp,
    study,
    fire_state,
    predictor,
    threshold:  float,
    out_dir:    Path,
    pred_cache = None,
) -> dict:
    """Build risk zones, perimeter and hotspots GeoJSONs for one timestep.

    Returns:
        intermediates dict (fire context) from the 6h horizon run,
        or empty dict if prediction produced no features.
    """
    import wildfire_hotspot_prediction as whp

    out_dir.mkdir(parents=True, exist_ok=True)
    t1 = pd.Timestamp(overpass_time)
    intermediates = {}

    # ── Risk zones (3h, 6h, 12h) ─────────────────────────────────────────────
    for h in _HORIZONS:
        out_path = out_dir / f"risk_zones_{h}h.geojson"
        if out_path.exists():
            log.info("[prediction] ts=%d risk_zones_%dh already exists — skip", ts_id, h)
            continue

        result_df, ctx = whp.run_prediction_pipeline(
            study, t1=t1, delta_t_h=float(h),
            predictor=predictor, threshold=threshold, pred_cache=pred_cache,
        )
        if h == 6:
            intermediates = ctx   # capture from the primary horizon

        if result_df.empty:
            log.warning("[prediction] ts=%d no features for delta_t=%dh", ts_id, h)
            write_geojson(out_path, [])
            continue

        geojson = build_risk_geojson(result_df, high_thresh=threshold, horizon=f"{h}h")
        out_path.write_text(json.dumps(geojson, allow_nan=False), encoding="utf-8")
        log.info("[prediction] ts=%d risk_zones_%dh → %d features",
                 ts_id, h, len(geojson["features"]))

    # ── fire_context.json ─────────────────────────────────────────────────────
    ctx_path = out_dir / "fire_context.json"
    if not ctx_path.exists() and intermediates:
        ctx = {k: v for k, v in intermediates.items() if k != "wind_forecast"}
        ctx_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        log.info("[prediction] ts=%d fire_context.json written", ts_id)

    return intermediates


def _export_perimeter(fire_state, t1: pd.Timestamp, out_path: Path) -> None:
    import pyproj
    from shapely.ops import transform as shp_transform

    geom = fire_state.boundary_after.get(t1)

    # Fallback: exact key lookup can fail due to timezone / precision mismatch
    # after DB round-trip. Find the nearest key within a 2-hour window.
    if geom is None:
        candidate_keys = [k for k, v in fire_state.boundary_after.items() if v is not None]
        if candidate_keys:
            nearest = min(candidate_keys,
                          key=lambda k: abs((pd.Timestamp(k) - t1).total_seconds()))
            if abs((pd.Timestamp(nearest) - t1).total_seconds()) <= 7200:
                geom = fire_state.boundary_after[nearest]
                log.debug("[prediction] perimeter: t1=%s → nearest key %s", t1, nearest)

    if geom is None or geom.is_empty:
        write_geojson(out_path, [])
        return

    transformer = pyproj.Transformer.from_crs("EPSG:3978", "EPSG:4326", always_xy=True)
    wgs84       = shp_transform(transformer.transform, geom)
    write_geojson(out_path, [{
        "type": "Feature",
        "geometry": wgs84.__geo_interface__,
        "properties": {
            "t1":          t1.isoformat(),
            "area_km2":    round(geom.area / 1e6, 3),
            "perimeter_m": round(geom.length, 1),
        },
    }])


def _export_hotspots(study, t1: pd.Timestamp, out_path: Path) -> None:
    import pyproj

    hs_df = pd.read_parquet(study.data_processed_dir / "firms" / "hotspots.parquet")
    hs_df["overpass_time"] = pd.to_datetime(hs_df["overpass_time"])
    subset = hs_df[hs_df["overpass_time"] == t1].copy()

    # Filter out low-confidence detections (VIIRS 'l' = <30% confidence)
    # and zero-FRP points (not genuine fire radiative power)
    if "confidence" in subset.columns:
        subset = subset[subset["confidence"].astype(str).str.lower() != "l"]
    if "frp" in subset.columns:
        subset = subset[subset["frp"].fillna(0) > 0]

    transformer = pyproj.Transformer.from_crs("EPSG:3978", "EPSG:4326", always_xy=True)
    features = []
    for _, row in subset.iterrows():
        lon, lat = transformer.transform(row["x_proj"], row["y_proj"])
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "frp":        float(row.get("frp", 0)),
                "confidence": str(row.get("confidence", "")),
            },
        })
    write_geojson(out_path, features)
