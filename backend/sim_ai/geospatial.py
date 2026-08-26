"""
sim_ai/geospatial.py
---------------------
Extract GIS context for the simulator from prediction output files.

Public API:
    extract_gis_context(event, ts_row) -> GisContext
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@dataclass
class GisContext:
    perimeter_pts: list[list[float]] = field(default_factory=list)   # [[lat, lon], ...]
    road_pts:      list[list[float]] = field(default_factory=list)   # [[lat, lon], ...]
    landmark_pts:  list[dict]        = field(default_factory=list)   # [{name, lat, lon, type}, ...]
    slot_time:     str | None        = None


def extract_gis_context(event, ts_row) -> GisContext:
    """Load perimeter + road + landmark coordinates from prediction files.

    Args:
        event:   FireEvent ORM row
        ts_row:  EventTimestep ORM row

    Returns:
        GisContext with sampled lat/lon lists and slot_time ISO string.
    """
    if ts_row is None:
        return GisContext()

    slot_time_str = pd.Timestamp(ts_row.slot_time).isoformat()
    ts_str   = pd.Timestamp(ts_row.slot_time).strftime("%Y-%m-%dT%H%M")
    event_dir = _DATA_DIR / "events" / f"{event.year}_{event.id:04d}"
    base_dir  = event_dir / "timesteps" / ts_str

    return GisContext(
        perimeter_pts = _pts_from_geojson(base_dir / "perimeter" / "perimeter.geojson"),
        road_pts      = _pts_from_geojson(base_dir / "spatial_analysis" / "ML" / "roads.geojson"),
        landmark_pts  = _load_landmarks(event_dir / "landmarks.json"),
        slot_time     = slot_time_str,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_landmarks(path: Path) -> list[dict]:
    """Return [{name, lat, lon, type}, ...] from landmarks.json."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        result = []
        for item in raw:
            if "lat" in item and "lon" in item:
                result.append({
                    "name": item.get("name", ""),
                    "lat":  item["lat"],
                    "lon":  item["lon"],
                    "type": item.get("type", ""),
                })
        return result
    except Exception:
        return []


def _pts_from_geojson(path: Path) -> list[list[float]]:
    """Return [[lat, lon], ...] from any GeoJSON feature collection."""
    if not path.exists():
        return []
    try:
        fc   = json.loads(path.read_text(encoding="utf-8"))
        pts: list[list[float]] = []
        for f in fc.get("features", []):
            _collect(f.get("geometry") or {}, pts)
        return pts
    except Exception:
        return []


def _collect(geom: dict, pts: list[list[float]]) -> None:
    gtype  = geom.get("type", "")
    coords = geom.get("coordinates", [])
    if gtype == "Point":
        pts.append([coords[1], coords[0]])
    elif gtype in ("LineString", "MultiPoint"):
        pts.extend([c[1], c[0]] for c in coords)
    elif gtype == "Polygon":
        pts.extend([c[1], c[0]] for c in coords[0])
    elif gtype == "MultiLineString":
        for ring in coords:
            pts.extend([c[1], c[0]] for c in ring)
    elif gtype == "MultiPolygon":
        for poly in coords:
            pts.extend([c[1], c[0]] for c in poly[0])
    elif gtype == "GeometryCollection":
        for g in geom.get("geometries", []):
            _collect(g, pts)
