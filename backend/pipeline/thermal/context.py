"""Event-scoped industrial and land-cover context for thermal observations.

Providers normalize external sources into stable files beneath an event's
``data_processed`` directory.  Feature enrichment is deliberately descriptive:
it does not assign source classes, persistence labels, or ML predictions.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
from rasterio.merge import merge
from shapely.geometry import Point, Polygon, box, shape
from shapely.ops import unary_union

from pipeline.event_config import THERMAL_MONITORING_MODE, get_event_config

log = logging.getLogger(__name__)

MIDC_BOUNDARY_URL = (
    "https://gis.midcindia.org/server/rest/services/CitizenPortal/"
    "MIDC_PUBLIC_MAIN_MAP_SERVICE/MapServer/6/query"
)
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
WORLDCOVER_URL = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)

WORLDCOVER_CLASSES = {
    10: ("tree_cover", "vegetation"),
    20: ("shrubland", "vegetation"),
    30: ("grassland", "vegetation"),
    40: ("cropland", "agricultural"),
    50: ("built_up", "built_up"),
    60: ("bare_sparse_vegetation", "bare"),
    70: ("snow_and_ice", "other"),
    80: ("permanent_water", "water"),
    90: ("herbaceous_wetland", "vegetation"),
    95: ("mangroves", "vegetation"),
    100: ("moss_and_lichen", "vegetation"),
}


@dataclass(frozen=True)
class ThermalContextPaths:
    industrial_dir: Path
    midc_path: Path
    industrial_areas_path: Path
    facilities_path: Path
    industrial_metadata_path: Path
    landcover_dir: Path
    landcover_path: Path
    landcover_metadata_path: Path
    thermal_dir: Path
    enriched_path: Path
    enriched_geojson_path: Path
    enrichment_metadata_path: Path

    @classmethod
    def from_project_dir(cls, project_dir) -> "ThermalContextPaths":
        processed = Path(project_dir) / "data_processed"
        industrial = processed / "industrial"
        landcover = processed / "landcover"
        thermal = processed / "thermal"
        return cls(
            industrial_dir=industrial,
            midc_path=industrial / "midc_boundary.geojson",
            industrial_areas_path=industrial / "industrial_areas.geojson",
            facilities_path=industrial / "facilities.geojson",
            industrial_metadata_path=industrial / "metadata.json",
            landcover_dir=landcover,
            landcover_path=landcover / "worldcover_2021.tif",
            landcover_metadata_path=landcover / "metadata.json",
            thermal_dir=thermal,
            enriched_path=thermal / "firms_enriched.parquet",
            enriched_geojson_path=thermal / "firms_enriched.geojson",
            enrichment_metadata_path=thermal / "enrichment_metadata.json",
        )


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(temporary, index=False)
    temporary.replace(path)


def _write_geojson(frame: gpd.GeoDataFrame, path: Path) -> None:
    serializable = frame.copy().to_crs("EPSG:4326")
    for column in serializable.columns:
        if column == serializable.geometry.name:
            continue
        if pd.api.types.is_datetime64_any_dtype(serializable[column]):
            serializable[column] = serializable[column].map(
                lambda value: value.isoformat() if pd.notna(value) else None,
            )
    _atomic_write_text(path, serializable.to_json(drop_id=True, na="null"))


def _empty_geo_frame(columns: list[str]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {column: pd.Series(dtype="object") for column in columns},
        geometry=gpd.GeoSeries([], dtype="geometry"),
        crs="EPSG:4326",
    )


def _fetch_json(
    method: str,
    url: str,
    *,
    session=requests,
    timeout: int = 90,
    **kwargs,
) -> dict:
    response = getattr(session, method)(url, timeout=timeout, **kwargs)
    response.raise_for_status()
    return response.json()


def collect_midc_boundaries(event, study, *, session=requests) -> gpd.GeoDataFrame:
    """Cache authoritative MIDC boundary polygons selected by profile filter."""
    config = get_event_config(event)
    paths = ThermalContextPaths.from_project_dir(study.project_dir)
    if paths.midc_path.exists():
        return gpd.read_file(paths.midc_path)
    if not config.industrial_boundary_filter:
        return _empty_geo_frame(["name", "source", "source_id"])

    escaped = config.industrial_boundary_filter.replace("'", "''")
    payload = _fetch_json(
        "get",
        MIDC_BOUNDARY_URL,
        session=session,
        params={
            "where": f"IA_NAME LIKE '%{escaped}%' OR IA_NAME LIKE '%{escaped.upper()}%'",
            "outFields": "IA_NAME,IA_CODE,IA_CATEGORY,MIDC_AREA,UpdatedOn",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "geojson",
        },
    )
    rows = []
    for feature in payload.get("features", []):
        geometry = feature.get("geometry")
        if not geometry:
            continue
        properties = feature.get("properties", {})
        rows.append({
            "name": properties.get("IA_NAME") or "MIDC industrial area",
            "source": "MIDC Enterprise GIS",
            "source_id": str(properties.get("IA_CODE") or ""),
            "area_category": properties.get("IA_CATEGORY"),
            "reported_area": properties.get("MIDC_AREA"),
            "source_updated_at": properties.get("UpdatedOn"),
            "geometry": shape(geometry),
        })
    result = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326") if rows else (
        _empty_geo_frame(["name", "source", "source_id"])
    )
    _write_geojson(result, paths.midc_path)
    return result


def _overpass_request(query: str, *, session=requests) -> dict:
    last_error: Exception | None = None
    for url in OVERPASS_URLS:
        try:
            return _fetch_json(
                "post",
                url,
                session=session,
                data=query.encode("utf-8"),
                headers={
                    "User-Agent": "wildfire-decision-support/1.0",
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        except Exception as exc:
            last_error = exc
            log.warning("[thermal-context] Overpass request failed via %s: %s", url, exc)
    raise RuntimeError(f"all Overpass endpoints failed: {last_error}")


def _closed_polygon(coordinates) -> Polygon | None:
    points = [
        (node["lon"], node["lat"])
        for node in coordinates or []
        if "lon" in node and "lat" in node
    ]
    if len(points) < 4 or points[0] != points[-1]:
        return None
    polygon = Polygon(points)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    return polygon if not polygon.is_empty else None


def _industrial_area_geometry(element):
    direct = _closed_polygon(element.get("geometry"))
    if direct is not None:
        return direct
    polygons = []
    for member in element.get("members", []):
        if member.get("role") not in ("", "outer"):
            continue
        polygon = _closed_polygon(member.get("geometry"))
        if polygon is not None:
            polygons.append(polygon)
    return unary_union(polygons) if polygons else None


def _facility_type(tags: dict) -> str:
    power = tags.get("power")
    if power:
        return f"power_{power}"
    if tags.get("man_made") == "works":
        return "works"
    if tags.get("industrial"):
        return str(tags["industrial"])
    return "industrial_facility"


def collect_osm_industrial_context(event, study, *, session=requests) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Cache OSM industrial land-use polygons and relevant infrastructure."""
    paths = ThermalContextPaths.from_project_dir(study.project_dir)
    if paths.industrial_areas_path.exists() and paths.facilities_path.exists():
        return gpd.read_file(paths.industrial_areas_path), gpd.read_file(paths.facilities_path)

    min_lon, min_lat, max_lon, max_lat = get_event_config(event).bbox
    bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    areas_query = (
        "[out:json][timeout:60];("
        f'way["landuse"="industrial"]({bbox});'
        f'relation["landuse"="industrial"]({bbox});'
        ");out tags center geom;"
    )
    facilities_query = (
        "[out:json][timeout:60];("
        f'nwr["industrial"]({bbox});'
        f'nwr["man_made"="works"]({bbox});'
        f'nwr["power"~"^(plant|generator|substation)$"]({bbox});'
        ");out tags center;"
    )
    area_payload = _overpass_request(areas_query, session=session)
    facility_payload = _overpass_request(facilities_query, session=session)

    area_rows = []
    for element in area_payload.get("elements", []):
        geometry = _industrial_area_geometry(element)
        if geometry is None:
            continue
        tags = element.get("tags", {})
        area_rows.append({
            "name": tags.get("name") or "Unnamed industrial area",
            "industry_type": tags.get("industrial") or "industrial_area",
            "osm_type": element.get("type"),
            "osm_id": str(element.get("id", "")),
            "source": "OpenStreetMap",
            "geometry": geometry,
        })
    areas = gpd.GeoDataFrame(area_rows, geometry="geometry", crs="EPSG:4326") if area_rows else (
        _empty_geo_frame(["name", "industry_type", "osm_type", "osm_id", "source"])
    )

    facility_rows = []
    for element in facility_payload.get("elements", []):
        tags = element.get("tags", {})
        center = element if element.get("type") == "node" else element.get("center", {})
        if "lon" not in center or "lat" not in center:
            continue
        facility_rows.append({
            "name": tags.get("name") or tags.get("operator") or "Unnamed industrial facility",
            "industry_type": _facility_type(tags),
            "industrial_tag": tags.get("industrial"),
            "power_source": tags.get("plant:source") or tags.get("generator:source"),
            "operator": tags.get("operator"),
            "osm_type": element.get("type"),
            "osm_id": str(element.get("id", "")),
            "source": "OpenStreetMap",
            "geometry": Point(float(center["lon"]), float(center["lat"])),
        })
    facilities = (
        gpd.GeoDataFrame(facility_rows, geometry="geometry", crs="EPSG:4326")
        if facility_rows else _empty_geo_frame([
            "name", "industry_type", "industrial_tag", "power_source", "operator",
            "osm_type", "osm_id", "source",
        ])
    )
    _write_geojson(areas, paths.industrial_areas_path)
    _write_geojson(facilities, paths.facilities_path)
    return areas, facilities


def _worldcover_tile_ids(bbox: tuple[float, float, float, float]) -> list[str]:
    min_lon, min_lat, max_lon, max_lat = bbox
    longitude_starts = range(math.floor(min_lon / 3) * 3, math.floor(max_lon / 3) * 3 + 1, 3)
    latitude_starts = range(math.floor(min_lat / 3) * 3, math.floor(max_lat / 3) * 3 + 1, 3)
    result = []
    for latitude in latitude_starts:
        for longitude in longitude_starts:
            lat_label = f"{'N' if latitude >= 0 else 'S'}{abs(latitude):02d}"
            lon_label = f"{'E' if longitude >= 0 else 'W'}{abs(longitude):03d}"
            result.append(lat_label + lon_label)
    return result


def collect_worldcover(event, study) -> Path:
    """Window the official ESA WorldCover COG(s) to the configured event AOI."""
    config = get_event_config(event)
    paths = ThermalContextPaths.from_project_dir(study.project_dir)
    if paths.landcover_path.exists():
        return paths.landcover_path

    sources = []
    try:
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
            for tile_id in _worldcover_tile_ids(config.bbox):
                sources.append(rasterio.open(WORLDCOVER_URL.format(tile=tile_id)))
            array, transform = merge(sources, bounds=config.bbox)
            profile = sources[0].profile.copy()
            profile.update({
                "height": array.shape[1],
                "width": array.shape[2],
                "transform": transform,
                "count": 1,
                "driver": "GTiff",
                "compress": "deflate",
                "tiled": True,
            })
            paths.landcover_dir.mkdir(parents=True, exist_ok=True)
            temporary = paths.landcover_path.with_suffix(".tif.tmp")
            with rasterio.open(temporary, "w", **profile) as destination:
                destination.write(array[:1])
            temporary.replace(paths.landcover_path)
    finally:
        for source in sources:
            source.close()
    return paths.landcover_path


def _landcover_features(raster_path: Path, points: gpd.GeoDataFrame) -> list[dict]:
    output = []
    with rasterio.open(raster_path) as source:
        for geometry in points.geometry:
            longitude, latitude = geometry.x, geometry.y
            try:
                value = int(next(source.sample([(longitude, latitude)]))[0])
            except Exception:
                value = 0
            class_name, group = WORLDCOVER_CLASSES.get(value, ("unknown", "unknown"))

            latitude_delta = 500.0 / 111_320.0
            longitude_delta = 500.0 / (111_320.0 * max(math.cos(math.radians(latitude)), 0.1))
            window = rasterio.windows.from_bounds(
                longitude - longitude_delta,
                latitude - latitude_delta,
                longitude + longitude_delta,
                latitude + latitude_delta,
                source.transform,
            ).round_offsets().round_lengths()
            values = source.read(1, window=window, boundless=True, fill_value=0)
            rows, columns = np.indices(values.shape)
            xs, ys = rasterio.transform.xy(source.window_transform(window), rows, columns)
            xs = np.asarray(xs).reshape(values.shape)
            ys = np.asarray(ys).reshape(values.shape)
            distances = np.sqrt(
                ((xs - longitude) * 111_320.0 * math.cos(math.radians(latitude))) ** 2
                + ((ys - latitude) * 111_320.0) ** 2
            )
            circle_values = values[distances <= 500.0]
            valid = circle_values[circle_values != 0]
            denominator = len(valid)
            forest_codes = np.isin(valid, [10, 95])
            cropland_codes = valid == 40
            builtup_codes = valid == 50
            output.append({
                "landcover_code": value if value else None,
                "landcover_class": class_name,
                "landcover_group": group,
                "is_built_up": value == 50,
                "is_forest": value in (10, 95),
                "is_cropland": value == 40,
                "forest_fraction_500m": float(forest_codes.sum() / denominator) if denominator else None,
                "cropland_fraction_500m": float(cropland_codes.sum() / denominator) if denominator else None,
                "builtup_fraction_500m": float(builtup_codes.sum() / denominator) if denominator else None,
            })
    return output


def collect_industrial_context(event, study) -> dict:
    """Run configured boundary/OSM providers and persist normalized metadata."""
    config = get_event_config(event)
    paths = ThermalContextPaths.from_project_dir(study.project_dir)
    boundary_providers: dict[str, Callable] = {
        "midc_arcgis": collect_midc_boundaries,
    }
    context_providers: dict[str, Callable] = {
        "osm_overpass": collect_osm_industrial_context,
    }
    boundary_provider = boundary_providers.get(config.industrial_boundary_provider)
    context_provider = context_providers.get(config.industrial_context_provider)
    boundaries = boundary_provider(event, study) if boundary_provider else (
        _empty_geo_frame(["name", "source", "source_id"])
    )
    areas, facilities = context_provider(event, study) if context_provider else (
        _empty_geo_frame(["name", "industry_type", "source"]),
        _empty_geo_frame(["name", "industry_type", "source"]),
    )
    metadata = {
        "event_id": event.id,
        "boundary_provider": config.industrial_boundary_provider,
        "industrial_context_provider": config.industrial_context_provider,
        "boundary_source_url": MIDC_BOUNDARY_URL.rsplit("/query", 1)[0],
        "boundary_source_note": "MIDC Enterprise GIS reference boundary; not a legal survey.",
        "osm_source_url": "https://www.openstreetmap.org/",
        "osm_attribution": "OpenStreetMap contributors",
        "midc_boundary_count": len(boundaries),
        "industrial_area_count": len(areas),
        "industrial_facility_count": len(facilities),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_text(paths.industrial_metadata_path, json.dumps(metadata, indent=2))
    return metadata


def collect_landcover(event, study) -> dict:
    """Run the configured global land-cover provider and cache its metadata."""
    config = get_event_config(event)
    paths = ThermalContextPaths.from_project_dir(study.project_dir)
    providers: dict[str, Callable] = {"esa_worldcover_2021": collect_worldcover}
    provider = providers.get(config.landcover_provider)
    raster_path = provider(event, study) if provider else None
    metadata = {
        "event_id": event.id,
        "provider": config.landcover_provider,
        "data_available": bool(raster_path and Path(raster_path).exists()),
        "product": "ESA WorldCover 2021 v200" if raster_path else None,
        "source_url": "https://esa-worldcover.org/" if raster_path else None,
        "license": "CC BY 4.0" if raster_path else None,
        "resolution_m": 10 if raster_path else None,
        "classes": {str(code): values[0] for code, values in WORLDCOVER_CLASSES.items()},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_text(paths.landcover_metadata_path, json.dumps(metadata, indent=2))
    return metadata


def enrich_thermal_history(event, study) -> dict:
    """Attach industrial proximity/containment and WorldCover features to FIRMS."""
    config = get_event_config(event)
    paths = ThermalContextPaths.from_project_dir(study.project_dir)
    history_path = paths.thermal_dir / "firms_history.parquet"
    if not history_path.exists():
        raise FileNotFoundError(f"normalized FIRMS history not found: {history_path}")

    history = pd.read_parquet(history_path)
    points = gpd.GeoDataFrame(
        history.copy(),
        geometry=gpd.points_from_xy(history["longitude"], history["latitude"]),
        crs="EPSG:4326",
    )
    boundaries = gpd.read_file(paths.midc_path) if paths.midc_path.exists() else _empty_geo_frame([])
    areas = gpd.read_file(paths.industrial_areas_path) if paths.industrial_areas_path.exists() else _empty_geo_frame([])
    facilities = gpd.read_file(paths.facilities_path) if paths.facilities_path.exists() else _empty_geo_frame([])

    metric_points = points.to_crs("EPSG:32643")
    metric_boundaries = boundaries.to_crs("EPSG:32643") if not boundaries.empty else boundaries
    metric_areas = areas.to_crs("EPSG:32643") if not areas.empty else areas
    metric_facilities = facilities.to_crs("EPSG:32643") if not facilities.empty else facilities
    boundary_union = unary_union(metric_boundaries.geometry) if not metric_boundaries.empty else None
    area_union = unary_union(metric_areas.geometry) if not metric_areas.empty else None

    context_rows = []
    for point in metric_points.geometry:
        inside_midc = bool(boundary_union is not None and boundary_union.covers(point))
        inside_industrial = bool(area_union is not None and area_union.covers(point))
        distance_midc = float(point.distance(boundary_union)) if boundary_union is not None else None
        if metric_facilities.empty:
            nearest_distance = None
            nearest_name = None
            nearest_type = None
            count_500m = 0
            count_1km = 0
        else:
            distances = metric_facilities.geometry.distance(point)
            nearest_index = distances.idxmin()
            nearest_distance = float(distances.loc[nearest_index])
            nearest = metric_facilities.loc[nearest_index]
            nearest_name = nearest.get("name")
            nearest_type = nearest.get("industry_type")
            count_500m = int((distances <= 500.0).sum())
            count_1km = int((distances <= 1000.0).sum())
        context_rows.append({
            "inside_midc": inside_midc,
            "inside_industrial_polygon": inside_industrial,
            "distance_to_midc_m": distance_midc,
            "distance_to_nearest_industry_m": nearest_distance,
            "nearest_industry_type": nearest_type,
            "nearest_industry_name": nearest_name,
            "industrial_feature_count_500m": count_500m,
            "industrial_feature_count_1km": count_1km,
            "near_industrial_facility": bool(
                nearest_distance is not None
                and nearest_distance <= config.industrial_near_distance_m
            ),
        })

    enriched = pd.concat(
        [history.reset_index(drop=True), pd.DataFrame(context_rows)], axis=1,
    )
    if paths.landcover_path.exists():
        landcover_rows = _landcover_features(paths.landcover_path, points)
    else:
        landcover_rows = [{
            "landcover_code": None,
            "landcover_class": "unknown",
            "landcover_group": "unknown",
            "is_built_up": False,
            "is_forest": False,
            "is_cropland": False,
            "forest_fraction_500m": None,
            "cropland_fraction_500m": None,
            "builtup_fraction_500m": None,
        } for _ in range(len(enriched))]
    enriched = pd.concat([enriched, pd.DataFrame(landcover_rows)], axis=1)
    enriched["industrial_context_available"] = (
        enriched["distance_to_midc_m"].notna()
        & enriched["distance_to_nearest_industry_m"].notna()
        & enriched["landcover_class"].ne("unknown")
    )

    _atomic_write_parquet(enriched, paths.enriched_path)
    enriched_geo = gpd.GeoDataFrame(
        enriched.copy(),
        geometry=gpd.points_from_xy(enriched["longitude"], enriched["latitude"]),
        crs="EPSG:4326",
    )
    _write_geojson(enriched_geo, paths.enriched_geojson_path)

    group_counts = enriched["landcover_group"].value_counts(dropna=False).to_dict()
    metadata = {
        "event_id": event.id,
        "observation_count": len(enriched),
        "inside_midc_count": int(enriched["inside_midc"].sum()),
        "inside_industrial_area_count": int(enriched["inside_industrial_polygon"].sum()),
        "near_industrial_facility_count": int(enriched["near_industrial_facility"].sum()),
        "industrial_context_available_count": int(enriched["industrial_context_available"].sum()),
        "near_distance_threshold_m": config.industrial_near_distance_m,
        "landcover_group_counts": {str(key): int(value) for key, value in group_counts.items()},
        "classification_available": False,
        "classification_note": "Context enrichment only; no classifier or persistence label was applied.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_text(paths.enrichment_metadata_path, json.dumps(metadata, indent=2))
    return metadata


def ensure_thermal_context(event, study) -> dict | None:
    """Build all configured context caches and enrich normalized FIRMS history."""
    config = get_event_config(event)
    if config.analysis_mode != THERMAL_MONITORING_MODE:
        return None
    collect_industrial_context(event, study)
    collect_landcover(event, study)
    return enrich_thermal_history(event, study)


def load_context_metadata(event) -> dict | None:
    project_dir = Path(__file__).resolve().parents[3] / "data" / "events" / (
        f"{event.year}_{event.id:04d}"
    )
    paths = ThermalContextPaths.from_project_dir(project_dir)
    if not paths.enrichment_metadata_path.exists():
        return None
    metadata = json.loads(paths.enrichment_metadata_path.read_text(encoding="utf-8"))
    if paths.industrial_metadata_path.exists():
        metadata["industrial_sources"] = json.loads(
            paths.industrial_metadata_path.read_text(encoding="utf-8"),
        )
    if paths.landcover_metadata_path.exists():
        metadata["landcover_source"] = json.loads(
            paths.landcover_metadata_path.read_text(encoding="utf-8"),
        )
    return metadata
