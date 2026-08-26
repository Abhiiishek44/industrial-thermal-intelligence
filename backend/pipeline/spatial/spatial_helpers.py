"""
pipeline/spatial/spatial_helpers.py
-------------------------------------
Population counts and pure geo helpers for spatial analysis.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import geopandas as gpd

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"


def population_counts(bbox, perimeter_geom, risk: dict, fire_year: int | None) -> dict:
    pop_path = _DATA_DIR / "static" / "population.gpkg"
    if not pop_path.exists():
        log.warning("[spatial] population.gpkg not found — population counts unavailable")
        return {f: 0 for f in ["affected_population", "at_risk_3h", "at_risk_6h", "at_risk_12h"]}

    census_year = _nearest_census_year(fire_year)
    da = gpd.read_file(pop_path, bbox=bbox, layer="dissemination_areas")
    da = da[da["census_year"] == census_year].to_crs("EPSG:4326")

    def pop_in(zone_geom, exclude_geom=None):
        if zone_geom is None:
            return 0
        mask = da.intersects(zone_geom)
        if exclude_geom is not None:
            mask &= ~da.intersects(exclude_geom)
        return int(da.loc[mask, "population"].sum())

    r3, r6, r12 = risk.get(3), risk.get(6), risk.get(12)

    from shapely.ops import unary_union as _union

    def _union_geoms(*geoms):
        valid = [g for g in geoms if g is not None]
        return _union(valid) if valid else None

    return {
        "affected_population": pop_in(perimeter_geom),
        "at_risk_3h":          pop_in(r3,  perimeter_geom),
        "at_risk_6h":          pop_in(r6,  _union_geoms(perimeter_geom, r3)),
        "at_risk_12h":         pop_in(r12, _union_geoms(perimeter_geom, r3, r6)),
    }


def _nearest_census_year(fire_year: int | None) -> int:
    for cy in [2021, 2016, 2011]:
        if fire_year is None or fire_year > cy:
            return cy
    return 2011


def load_geom(path: Path):
    """Load first geometry from a GeoJSON as a single shapely geometry."""
    if not path.exists():
        return None
    gdf = gpd.read_file(path)
    if gdf.empty:
        return None
    from shapely.ops import unary_union
    return unary_union(gdf.geometry)


def load_landmarks(path: Path) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return []


def event_bbox(event) -> tuple[float, float, float, float]:
    from shapely import wkb
    geom = wkb.loads(bytes(event.bbox.data))
    return geom.bounds


def haversine_km(lon1, lat1, lon2, lat2) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def bearing_label(lon1, lat1, lon2, lat2) -> str:
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[round((math.degrees(math.atan2(x, y)) + 360) % 360 / 45) % 8]


def describe_point(lon: float, lat: float, landmarks: list) -> str:
    if not landmarks:
        return f"{lat:.3f}N {abs(lon):.3f}W"
    best = min(landmarks, key=lambda p: haversine_km(lon, lat, p["lon"], p["lat"]))
    dist = haversine_km(lon, lat, best["lon"], best["lat"])
    bear = bearing_label(best["lon"], best["lat"], lon, lat)
    return f"near {best['name']}" if dist < 5 else f"{bear} of {best['name']}, {dist:.0f} km"
