"""
pipeline/spatial/spatial.py
----------------------------
Stage 2: Spatial analysis per timestep.

Inputs  (files):  prediction/perimeter.geojson, risk_zones_{3,6,12}h.geojson
                  data/events/<year>_<id>/landmarks.json
                  timesteps/<ts>/hotspot/hotspots.geojson
Inputs  (static): data/static/roads_canada.gpkg, data/static/population.gpkg

Outputs (files):  spatial_analysis/roads.geojson
                  spatial_analysis/population.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString
from shapely.ops import unary_union

from pipeline.spatial.spatial_helpers import (
    population_counts, load_geom, load_landmarks, event_bbox,
    haversine_km, describe_point,
)

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"

_ROAD_TYPES = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
}
_HW_RANK = {
    "motorway": 0, "trunk": 1, "primary": 2, "secondary": 3,
    "motorway_link": 4, "trunk_link": 5, "primary_link": 6, "secondary_link": 7,
}
_STATUS_RANK = {
    "burning": 0, "burned": 1,
    "at_risk_3h": 2, "at_risk_6h": 3, "at_risk_12h": 4, "clear": 5,
}
_HOTSPOT_BUFFER_DEG = 0.005   # ~500 m
_MERGE_GAP_KM       = 2.0


# ── Entry point ───────────────────────────────────────────────────────────────

def run_spatial_analysis(event_id: int, ts_id: int, out_dir: Path,
                         pred_dir: Path | None = None,
                         hotspot_path: Path | None = None) -> None:
    """Run spatial analysis for one timestep.

    Outputs (under out_dir = spatial_analysis/ or spatial_analysis_crowd/):
        ML/roads.geojson         — road network with fire status and sections
        ML/population.json       — population counts from ML risk zones
        wind_driven/roads.geojson  — (placeholder, standard mode only)
        wind_driven/population.json

    pred_dir:     override the ML prediction input dir
                  (defaults to out_dir.parent / "prediction" / "ML")
    hotspot_path: override the hotspot GeoJSON path
                  (defaults to out_dir.parent / "hotspot" / "hotspots.geojson")
    """
    from db.models import FireEvent
    event        = FireEvent.query.get(event_id)
    ev_dir       = _DATA_DIR / "events" / f"{event.year}_{event_id:04d}"
    ml_dir       = pred_dir if pred_dir is not None else out_dir.parent / "prediction" / "ML"
    perim_dir    = out_dir.parent / "perimeter"
    hotspot_path = hotspot_path if hotspot_path is not None else out_dir.parent / "hotspot" / "hotspots.geojson"
    out_dir.mkdir(parents=True, exist_ok=True)

    perimeter_geom = load_geom(perim_dir / "perimeter.geojson")
    risk = {
        3:  load_geom(ml_dir / "risk_zones_3h.geojson"),
        6:  load_geom(ml_dir / "risk_zones_6h.geojson"),
        12: load_geom(ml_dir / "risk_zones_12h.geojson"),
    }
    landmarks = load_landmarks(ev_dir / "landmarks.json")
    bbox      = event_bbox(event)

    roads_gdf = _build_roads(
        bbox, perimeter_geom, risk, landmarks,
        ev_dir=ev_dir, hotspot_path=hotspot_path,
    )
    ml_counts = population_counts(bbox, perimeter_geom, risk, event.year)
    log.info("[spatial] ts=%d: ML affected_population=%d", ts_id, ml_counts.get("affected_population", 0))

    # ── ML outputs ────────────────────────────────────────────────────────────
    ml_dir_out = out_dir / "ML"
    ml_dir_out.mkdir(exist_ok=True)

    # population first — so it's written even if roads serialization fails
    (ml_dir_out / "population.json").write_text(
        json.dumps(ml_counts, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # sections is a Python list — serialize to JSON string for fiona/pyogrio compat
    roads_out = roads_gdf.copy()
    roads_out["sections"] = roads_out["sections"].apply(json.dumps)
    roads_out.to_file(ml_dir_out / "roads.geojson", driver="GeoJSON")
    log.info("[spatial] ts=%d: ML/roads.geojson written (%d features)", ts_id, len(roads_out))

    # ── wind_driven outputs (placeholder, standard mode only) ─────────────────
    if pred_dir is not None:
        return  # crowd mode — no wind_driven placeholder needed
    wd_dir_out = out_dir / "wind_driven"
    wd_dir_out.mkdir(exist_ok=True)
    if not (wd_dir_out / "roads.geojson").exists():
        gpd.GeoDataFrame(
            columns=["road_name", "highway", "status", "sections", "geometry"],
            geometry="geometry", crs="EPSG:4326",
        ).to_file(wd_dir_out / "roads.geojson", driver="GeoJSON")
    if not (wd_dir_out / "population.json").exists():
        wd_counts = {
            "affected_population": ml_counts.get("affected_population", 0),
            "at_risk_3h": None, "at_risk_6h": None, "at_risk_12h": None,
        }
        (wd_dir_out / "population.json").write_text(
            json.dumps(wd_counts, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ── Road analysis ─────────────────────────────────────────────────────────────

def _build_roads(bbox, perimeter_geom, risk: dict, landmarks: list,
                 ev_dir: Path | None = None,
                 hotspot_path: Path | None = None) -> gpd.GeoDataFrame:
    _EMPTY = gpd.GeoDataFrame(
        columns=["road_name", "highway", "status", "sections", "geometry"],
        geometry="geometry", crs="EPSG:4326",
    )

    cached = (ev_dir / "data_processed" / "roads" / "roads_clipped.gpkg") if ev_dir else None
    if cached and cached.exists():
        roads = gpd.read_file(cached)
        if roads.crs is None:
            roads = roads.set_crs("EPSG:4326")
        roads = roads.to_crs("EPSG:4326")
    else:
        roads_path = _DATA_DIR / "static" / "roads_canada.gpkg"
        if not roads_path.exists():
            log.warning("[spatial] roads_canada.gpkg not found — skipping road analysis")
            return _EMPTY
        roads = gpd.read_file(roads_path, bbox=bbox)
        roads = roads[roads["highway"].isin(_ROAD_TYPES)].copy()
        roads["name"]      = roads["name"].fillna("").str.strip()
        roads["road_name"] = roads["name"].where(roads["name"] != "", roads["highway"])

    # Load hotspots → buffered union
    hotspot_union = None
    if hotspot_path and hotspot_path.exists():
        hs = gpd.read_file(hotspot_path).to_crs("EPSG:4326")
        if not hs.empty:
            hotspot_union = hs.geometry.buffer(_HOTSPOT_BUFFER_DEG).unary_union

    risk_geoms = {h: g for h, g in risk.items() if g is not None}

    # Status masks — priority: burning > burned > 3h > 6h > 12h > clear
    burning    = roads.intersects(hotspot_union) if hotspot_union is not None \
                 else pd.Series(False, index=roads.index)
    burned     = (roads.intersects(perimeter_geom) & ~burning) if perimeter_geom is not None \
                 else pd.Series(False, index=roads.index)
    at_risk_3  = (roads.intersects(risk_geoms[3])  & ~burning & ~burned) \
                 if 3  in risk_geoms else pd.Series(False, index=roads.index)
    at_risk_6  = (roads.intersects(risk_geoms[6])  & ~burning & ~burned & ~at_risk_3) \
                 if 6  in risk_geoms else pd.Series(False, index=roads.index)
    at_risk_12 = (roads.intersects(risk_geoms[12]) & ~burning & ~burned & ~at_risk_3 & ~at_risk_6) \
                 if 12 in risk_geoms else pd.Series(False, index=roads.index)

    roads["status"] = "clear"
    roads.loc[at_risk_12, "status"] = "at_risk_12h"
    roads.loc[at_risk_6,  "status"] = "at_risk_6h"
    roads.loc[at_risk_3,  "status"] = "at_risk_3h"
    roads.loc[burned,     "status"] = "burned"
    roads.loc[burning,    "status"] = "burning"

    exclusive_zones = _build_exclusive_zones(hotspot_union, perimeter_geom, risk_geoms)

    agg = (
        roads.groupby(["road_name", "status"], as_index=False)
             .agg(highway=("highway", lambda s: min(s, key=lambda h: _HW_RANK.get(h, 99))))
    )
    dissolved = roads.dissolve(by=["road_name", "status"])[["geometry"]].reset_index()
    dissolved = dissolved.merge(agg, on=["road_name", "status"])

    def get_sections(row):
        if row["status"] == "clear":
            return []
        zone = exclusive_zones.get(row["status"])
        if zone is None:
            return []
        return _compute_sections(row["geometry"], zone, landmarks)

    dissolved["sections"] = dissolved.apply(get_sections, axis=1)

    return gpd.GeoDataFrame(
        dissolved[["road_name", "highway", "status", "sections", "geometry"]],
        geometry="geometry", crs="EPSG:4326",
    )


def _build_exclusive_zones(hotspot_union, perimeter_geom, risk_geoms: dict) -> dict:
    """Build non-overlapping zone geometries for section clipping.

    Priority order (each zone excludes all higher-priority zones):
        burning > burned > at_risk_3h > at_risk_6h > at_risk_12h
    """
    zones   = {}
    exclude = None

    if hotspot_union is not None:
        zones["burning"] = hotspot_union
        exclude = hotspot_union

    if perimeter_geom is not None:
        zones["burned"] = perimeter_geom.difference(exclude) if exclude is not None else perimeter_geom
        exclude = unary_union([g for g in [exclude, perimeter_geom] if g is not None])

    for h, label in [(3, "at_risk_3h"), (6, "at_risk_6h"), (12, "at_risk_12h")]:
        if h not in risk_geoms:
            continue
        z = risk_geoms[h]
        zones[label] = z.difference(exclude) if exclude is not None else z
        exclude = unary_union([g for g in [exclude, z] if g is not None])

    return zones


# ── Section helpers ───────────────────────────────────────────────────────────

def _compute_sections(road_geom, zone_geom, landmarks: list) -> list[dict]:
    """Return [{section_id, from, to}, ...] for contiguous road segments inside zone_geom."""
    try:
        clipped = road_geom.intersection(zone_geom)
    except Exception:
        return []
    if clipped.is_empty:
        return []

    if clipped.geom_type == "LineString":
        lines = [clipped]
    elif clipped.geom_type == "MultiLineString":
        lines = list(clipped.geoms)
    elif clipped.geom_type == "GeometryCollection":
        expanded = []
        for g in clipped.geoms:
            if g.geom_type == "LineString":
                expanded.append(g)
            elif g.geom_type == "MultiLineString":
                expanded.extend(g.geoms)
        lines = expanded
    else:
        return []

    lines = [l for l in lines if len(list(l.coords)) >= 2]
    if not lines:
        return []

    lines = _merge_close_sections(lines, _MERGE_GAP_KM)

    result = []
    for i, line in enumerate(lines, 1):
        coords = list(line.coords)
        result.append({
            "section_id": i,
            "from": describe_point(coords[0][0],  coords[0][1],  landmarks),
            "to":   describe_point(coords[-1][0], coords[-1][1], landmarks),
        })
    return result


def _merge_close_sections(lines: list, merge_gap_km: float) -> list:
    """Merge adjacent LineString segments whose gap is within merge_gap_km."""
    lines = sorted(lines, key=lambda l: list(l.coords)[0])
    merged, current = [], lines[0]
    for nxt in lines[1:]:
        end   = list(current.coords)[-1]
        start = list(nxt.coords)[0]
        if haversine_km(end[0], end[1], start[0], start[1]) <= merge_gap_km:
            current = LineString(list(current.coords) + list(nxt.coords))
        else:
            merged.append(current)
            current = nxt
    merged.append(current)
    return merged


def _road_summary(roads_gdf: gpd.GeoDataFrame) -> list[dict]:
    MAJOR = {"motorway", "trunk", "primary", "secondary"}
    major = roads_gdf[roads_gdf["highway"].isin(MAJOR)].copy()
    summary = []
    for road, grp in major.groupby("road_name"):
        if road in _ROAD_TYPES:
            continue
        worst = min(grp["status"], key=lambda s: _STATUS_RANK.get(s, 99))
        if worst == "clear":
            continue
        row = grp[grp["status"] == worst].iloc[0]
        summary.append({
            "road":         road,
            "highway":      row["highway"],
            "worst_status": worst,
            "sections":     row["sections"] if isinstance(row["sections"], list) else [],
        })
    summary.sort(key=lambda x: _STATUS_RANK.get(x["worst_status"], 99))
    return summary
