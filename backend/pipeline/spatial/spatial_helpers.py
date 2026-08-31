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

WILDFIRE_EXPOSURE_FIELDS = (
    "affected_population", "at_risk_3h", "at_risk_6h", "at_risk_12h",
)
THERMAL_EXPOSURE_FIELDS = ("within_1km", "within_3km", "within_5km")


def unavailable_population(analysis_mode: str, reason: str) -> dict:
    """Return an explicit missing-data contract; missing never means zero."""
    thermal = analysis_mode == "thermal_monitoring"
    fields = THERMAL_EXPOSURE_FIELDS if thermal else WILDFIRE_EXPOSURE_FIELDS
    return {
        **{field: None for field in fields},
        "data_available": False,
        "exposure_mode": "proximity_buffers" if thermal else "forecast_zones",
        "reason": reason,
        "source": None,
    }


def _population_in_raster(pop_path: Path, zone_geom, exclude_geom=None) -> int:
    if zone_geom is None or zone_geom.is_empty:
        return 0

    import rasterio
    from pyproj import Transformer
    from rasterio.mask import mask
    from shapely.geometry import mapping
    from shapely.ops import transform

    zone = zone_geom.difference(exclude_geom) if exclude_geom is not None else zone_geom
    if zone.is_empty:
        return 0
    with rasterio.open(pop_path) as dataset:
        if dataset.crs and str(dataset.crs) != "EPSG:4326":
            transformer = Transformer.from_crs("EPSG:4326", dataset.crs, always_xy=True)
            zone = transform(transformer.transform, zone)
        try:
            values, _ = mask(dataset, [mapping(zone)], crop=True, filled=False)
        except ValueError:
            return 0
        band = values[0]
        return max(0, int(round(float(band.sum())))) if band.count() else 0


def _buffer_wgs84(geom, distance_m: float):
    if geom is None or geom.is_empty:
        return None
    projected = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs("EPSG:3857")
    return projected.buffer(distance_m).to_crs("EPSG:4326").iloc[0]


def population_counts(
    pop_path: Path,
    perimeter_geom,
    risk: dict,
    fire_year: int | None,
    *,
    analysis_mode: str = "wildfire_prediction",
    hotspot_geom=None,
) -> dict:
    if not pop_path.exists():
        log.info("[spatial] event population cache not configured")
        return unavailable_population(
            analysis_mode,
            "Population dataset is not configured for this region.",
        )

    if pop_path.suffix.lower() in {".tif", ".tiff"}:
        resolution_m = 1_000 if "1km" in pop_path.name.lower() else 100
        metadata_path = pop_path.with_name("population_metadata.json")
        if metadata_path.exists():
            try:
                resolution_m = int(json.loads(metadata_path.read_text()).get("resolution_m", resolution_m))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        source = {
            "provider": "WorldPop",
            "dataset_year": 2026,
            "resolution_m": resolution_m,
            "units": "estimated people",
        }
        if analysis_mode == "thermal_monitoring":
            if hotspot_geom is None or hotspot_geom.is_empty:
                return unavailable_population(
                    analysis_mode,
                    "No thermal detection geometry is available for exposure analysis.",
                )
            b1 = _buffer_wgs84(hotspot_geom, 1_000)
            b3 = _buffer_wgs84(hotspot_geom, 3_000)
            b5 = _buffer_wgs84(hotspot_geom, 5_000)
            return {
                "within_1km": _population_in_raster(pop_path, b1),
                "within_3km": _population_in_raster(pop_path, b3),
                "within_5km": _population_in_raster(pop_path, b5),
                "data_available": True,
                "exposure_mode": "proximity_buffers",
                "reason": None,
                "source": source,
            }

        r3, r6, r12 = risk.get(3), risk.get(6), risk.get(12)
        from shapely.ops import unary_union as _union

        def union_geoms(*geoms):
            valid = [geom for geom in geoms if geom is not None]
            return _union(valid) if valid else None

        return {
            "affected_population": _population_in_raster(pop_path, perimeter_geom),
            "at_risk_3h": _population_in_raster(pop_path, r3, perimeter_geom),
            "at_risk_6h": _population_in_raster(
                pop_path, r6, union_geoms(perimeter_geom, r3)
            ),
            "at_risk_12h": _population_in_raster(
                pop_path, r12, union_geoms(perimeter_geom, r3, r6)
            ),
            "data_available": True,
            "exposure_mode": "forecast_zones",
            "reason": None,
            "source": source,
        }

    census_year = _nearest_census_year(fire_year)
    da = gpd.read_file(pop_path, layer="population")
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
        "data_available":       True,
        "exposure_mode":        "forecast_zones",
        "reason":               None,
        "source": {
            "provider": "Statistics Canada census",
            "dataset_year": census_year,
            "units": "people",
        },
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
        lat_dir = "N" if lat >= 0 else "S"
        lon_dir = "E" if lon >= 0 else "W"
        return f"{abs(lat):.3f}{lat_dir} {abs(lon):.3f}{lon_dir}"
    best = min(landmarks, key=lambda p: haversine_km(lon, lat, p["lon"], p["lat"]))
    dist = haversine_km(lon, lat, best["lon"], best["lat"])
    bear = bearing_label(best["lon"], best["lat"], lon, lat)
    return f"near {best['name']}" if dist < 5 else f"{bear} of {best['name']}, {dist:.0f} km"
