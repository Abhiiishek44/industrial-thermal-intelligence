"""
pipeline/predict/risk_zones.py
------------------------------
Convert ML prediction output (500m grid cells) → risk zone GeoJSON polygons.

Only the 'high' risk level (prob >= youden_threshold) is exported.
Boundary smoothing is applied via morphological closing in EPSG:3978
before reprojection to WGS84.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# Morphological closing parameters (metres, in EPSG:3978)
_SMOOTH_OUT = 300.0   # expand to fill gaps between adjacent cells
_SMOOTH_IN  = 300.0   # contract back — net area preserved, edges rounded


def load_youden_threshold(
    models_dir: Path,
    model_name: str = "lr_steps",
    scale: float = 2.5,
) -> float:
    """Load Youden's J threshold from model_full_thresholds.json.

    Args:
        scale: Multiplier applied to the stored threshold before use.
               Values > 1.0 raise the decision boundary (fewer predictions).
               Capped at 0.95.
    """
    path = models_dir / "model_full_thresholds.json"
    raw = json.loads(path.read_text()).get(model_name, 0.5) if path.exists() else 0.5
    if not path.exists():
        log.warning("[risk_zones] thresholds.json not found — using 0.5")
    thr = min(raw * scale, 0.95)
    log.info("[risk_zones] threshold: %.4f × %.1f → %.4f", raw, scale, thr)
    return thr


def build_risk_geojson(df: pd.DataFrame, high_thresh: float, horizon: str) -> dict:
    """Convert 500m grid cell predictions to a smoothed high-risk polygon.

    Args:
        df:          DataFrame with b_x, b_y, prob columns (EPSG:3978).
        high_thresh: Youden's J threshold for 'high' risk.
        horizon:     Prediction horizon label, e.g. "3h", "6h", "12h".

    Returns:
        GeoJSON FeatureCollection with one feature (high risk zone only).
    """
    import pyproj
    from shapely.geometry import box
    from shapely.ops import unary_union
    from shapely.ops import transform as shp_transform

    half = 250.0

    high_df = df[df["prob"] >= high_thresh]
    if high_df.empty:
        return {"type": "FeatureCollection", "features": []}

    # Build and merge 500m grid boxes in projected CRS
    polys  = [box(r.b_x - half, r.b_y - half, r.b_x + half, r.b_y + half)
              for r in high_df.itertuples()]
    merged = unary_union(polys)

    # Smooth: morphological closing (buffer out then in) in EPSG:3978
    merged = merged.buffer(_SMOOTH_OUT).buffer(-_SMOOTH_IN)

    # Reproject to WGS84
    transformer = pyproj.Transformer.from_crs("EPSG:3978", "EPSG:4326", always_xy=True)
    merged_wgs = shp_transform(transformer.transform, merged)

    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": merged_wgs.__geo_interface__,
            "properties": {
                "horizon":    horizon,
                "risk_level": "high",
                "cell_count": len(high_df),
                "prob_mean":  round(float(high_df["prob"].mean()), 4),
                "prob_max":   round(float(high_df["prob"].max()),  4),
            },
        }],
    }


def write_geojson(path: Path, features: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, allow_nan=False),
        encoding="utf-8",
    )
