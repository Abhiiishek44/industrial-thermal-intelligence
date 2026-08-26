"""
pipeline/env.py
---------------
Environment preparation per FireEvent:
  - Shared: ML models (Zenodo), static GeoPackages (Zenodo)
  - Per-event: ERA5 weather, FIRMS hotspots, fire_state.pkl

All steps are idempotent (skip-if-exists). Safe to re-run.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_DATA_DIR   = Path(__file__).resolve().parents[2] / "data"
_MODELS_DIR = _DATA_DIR / "static" / "models"

_STATIC_FILES = {
    "population.gpkg":                         "https://zenodo.org/records/19434352/files/population.gpkg?download=1",
    "roads_canada.gpkg":                       "https://zenodo.org/records/19436338/files/roads_canada.gpkg?download=1",
    "actual_perimeter/actual_perimeter.gpkg":  "https://zenodo.org/records/19502692/files/actual_perimeter.gpkg?download=1",
}


def prepare_all_events(app) -> None:
    """Download shared assets + build per-event data for all FireEvents."""
    print("[env] Checking shared assets ...")
    _ensure_models()
    _download_static_gpkg()

    with app.app_context():
        from db.models import FireEvent
        events = FireEvent.query.all()
        if not events:
            log.warning("[env] No FireEvents in DB")
            return
        for event in events:
            print(f"[env] --- {event.name} ---")
            try:
                _prepare_event(event)
            except Exception as e:
                import traceback
                print(f"[env] ERROR: event {event.id} ({event.name}) failed: {e}")
                traceback.print_exc()


def _make_study(event):
    import wildfire_hotspot_prediction as whp
    from shapely import wkb

    geom = wkb.loads(bytes(event.bbox.data))
    lon_min, lat_min, lon_max, lat_max = geom.bounds
    project_dir = _DATA_DIR / "events" / f"{event.year}_{event.id:04d}"

    study = whp.Study(
        name        = event.name,
        bbox        = (lon_min, lat_min, lon_max, lat_max),
        start_date  = event.start_date.strftime("%Y-%m-%d"),
        end_date    = event.end_date.strftime("%Y-%m-%d"),
        project_dir = project_dir,
    )
    study.makedirs()

    # Remove unused library-generated dirs that we don't use in this system
    # (models/ and predictions/ are managed by data/static/models/ instead)
    for unused in (study.models_dir, study.predictions_dir, study.data_render_dir):
        try:
            unused.rmdir()   # only removes if empty
        except OSError:
            pass

    return study


def _prepare_event(event) -> None:
    import wildfire_hotspot_prediction as whp
    from wildfire_hotspot_prediction.training.fire_state import build_fire_state, save_fire_state

    study = _make_study(event)

    print("[env] Fetching landmarks ...")
    _fetch_landmarks(event, study)

    print("[env] Pre-building roads cache ...")
    _prebuild_roads(event, study)

    print("[env] Checking ERA5 ...")
    whp.ensure_era5_coverage(study)

    if not (study.landcover_raw_dir / "fuel_type.tif").exists():
        print("[env] Downloading fuel type map ...")
        whp.collect_environment(study, sources=["landcover"])
    if not (study.landcover_dir / "fuel_type.tif").exists():
        print("[env] Preprocessing fuel type map ...")
        whp.preprocess_environment(study, sources=["landcover"])

    terrain_raw_dir = study.project_dir / "data_raw" / "terrain"
    if not (terrain_raw_dir / "dtm.tif").exists():
        print("[env] Downloading terrain (DEM, slope, aspect) ...")
        whp.collect_environment(study, sources=["terrain"])
    _patch_terrain_crs(terrain_raw_dir)
    if not (study.project_dir / "data_processed" / "terrain" / "dtm.tif").exists():
        print("[env] Preprocessing terrain ...")
        whp.preprocess_environment(study, sources=["terrain"])

    fwi_path = study.weather_dir / "ffmc_daily.parquet"
    if not fwi_path.exists():
        print("[env] Building FWI indices ...")
        whp.build_fire_weather_index(study)
    else:
        print("[env] FWI indices — already exists, skip")

    grid_path = study.data_processed_dir / "grid_static.parquet"
    if not grid_path.exists():
        print("[env] Building static grid ...")
        whp.build_grid(study)
    else:
        print("[env] Static grid — already exists, skip")

    fire_state_path = study.training_dir / "fire_state.pkl"
    if fire_state_path.exists():
        print("[env] fire_state.pkl — already exists, skip")
        return

    print("[env] Collecting FIRMS hotspots ...")
    whp.collect_hotspots(study)

    print("[env] Preprocessing hotspots ...")
    hotspot_data = whp.preprocess_hotspots(study)

    print("[env] Building fire state ...")
    fire_state = build_fire_state(hotspot_data)
    save_fire_state(fire_state, fire_state_path)
    print(f"[env] fire_state.pkl written → {fire_state_path}")


def _patch_terrain_crs(terrain_raw_dir: Path) -> None:
    """Rewrite terrain TIFs with EPSG:3978 CRS using WKT (no proj.db lookup needed).

    MRDEM downloads use NAD83(CSRS)/Canada Atlas Lambert which some PROJ
    installations read as EngineeringCRS (unknown datum), blocking reprojection.
    NAD83(CSRS) and NAD83 differ by < 1 m — negligible at our 500 m grid.
    """
    import rasterio
    from rasterio.crs import CRS

    # WKT for EPSG:3978 — avoids CRS.from_epsg() which needs proj.db
    _WKT_3978 = (
        'PROJCS["NAD83 / Canada Atlas Lambert",'
        'GEOGCS["NAD83",DATUM["North_American_Datum_1983",'
        'SPHEROID["GRS 1980",6378137,298.257222101]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],'
        'PROJECTION["Lambert_Conformal_Conic_2SP"],'
        'PARAMETER["standard_parallel_1",49],'
        'PARAMETER["standard_parallel_2",77],'
        'PARAMETER["latitude_of_origin",49],'
        'PARAMETER["central_meridian",-95],'
        'PARAMETER["false_easting",0],'
        'PARAMETER["false_northing",0],'
        'UNIT["metre",1]]'
    )
    target_crs = CRS.from_wkt(_WKT_3978)

    for fname in ("dtm.tif", "slope.tif", "aspect.tif"):
        path = terrain_raw_dir / fname
        if not path.exists():
            continue
        try:
            with rasterio.open(path) as src:
                try:
                    if src.crs and src.crs.to_epsg() == 3978:
                        continue  # already correct
                except Exception:
                    pass  # CRS unreadable — patch it anyway
                data = src.read()
                meta = src.meta.copy()
            meta["crs"] = target_crs
            tmp = path.with_suffix(".patched.tif")
            with rasterio.open(tmp, "w", **meta) as dst:
                dst.write(data)
            tmp.replace(path)
            print(f"[env] terrain CRS patched → {fname}")
        except Exception as e:
            log.warning("[env] could not patch terrain CRS for %s: %s", fname, e)


def _fetch_landmarks(event, study) -> None:
    """Fetch named places near the event bbox and save to landmarks.json.

    Tries Overpass API first; falls back to community.gpkg centroids.
    Skips if landmarks.json already exists.
    """
    import json, time
    import requests
    from shapely import wkb

    out_path = study.project_dir / "landmarks.json"
    if out_path.exists():
        print("[env] landmarks.json — already exists, skip")
        return

    geom = wkb.loads(bytes(event.bbox.data))
    lon_min, lat_min, lon_max, lat_max = geom.bounds

    landmarks = _overpass_places(lat_min, lon_min, lat_max, lon_max)
    if not landmarks:
        log.warning("[env] Overpass unavailable — falling back to Nominatim")
        landmarks = _nominatim_places(lat_min, lon_min, lat_max, lon_max)

    out_path.write_text(json.dumps(landmarks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[env] landmarks.json — {len(landmarks)} places")


def _overpass_places(lat_min, lon_min, lat_max, lon_max) -> list[dict]:
    import requests
    PLACE_TYPES = "city|town|village|hamlet|locality|suburb"
    query = (
        f"[out:json][timeout:20];"
        f"node[\"place\"~\"^({PLACE_TYPES})$\"]"
        f"({lat_min},{lon_min},{lat_max},{lon_max});"
        f"out body;"
    )
    for url in ("https://overpass-api.de/api/interpreter",
                "https://overpass.openstreetmap.ru/api/interpreter"):
        try:
            r = requests.get(url, params={"data": query}, timeout=25)
            if r.status_code != 200:
                continue
            nodes = r.json().get("elements", [])
            RANK = {"city": 0, "town": 1, "village": 2, "hamlet": 3,
                    "suburb": 4, "locality": 5}
            result = [
                {"name": n["tags"].get("name", ""),
                 "lon":  n["lon"],
                 "lat":  n["lat"],
                 "type": n["tags"].get("place", "")}
                for n in nodes if n.get("tags", {}).get("name")
            ]
            result.sort(key=lambda x: RANK.get(x["type"], 99))
            return result
        except Exception:
            continue
    return []


def _nominatim_places(lat_min, lon_min, lat_max, lon_max) -> list[dict]:
    import requests, time

    # Derive a central query point and search radius
    center_lat = (lat_min + lat_max) / 2
    center_lon = (lon_min + lon_max) / 2
    viewbox = f"{lon_min},{lat_max},{lon_max},{lat_min}"   # left,top,right,bottom

    PLACE_TYPES = ["city", "town", "village", "hamlet", "locality"]
    headers = {"User-Agent": "wildfire-decision-support/1.0"}
    results = []
    seen = set()

    for place_type in PLACE_TYPES:
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": place_type, "format": "json", "limit": 20,
                        "viewbox": viewbox, "bounded": 1, "countrycodes": "ca"},
                headers=headers, timeout=10,
            )
            for d in r.json():
                name = d.get("display_name", "").split(",")[0].strip()
                if name and name not in seen:
                    seen.add(name)
                    results.append({
                        "name": name,
                        "lon":  float(d["lon"]),
                        "lat":  float(d["lat"]),
                        "type": place_type,
                    })
            time.sleep(1)
        except Exception:
            continue

    return results


_ROAD_TYPES = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
}


def _prebuild_roads(event, study) -> None:
    """Filter roads_canada.gpkg to event bbox once per event.

    Saves data_processed/roads/roads_raw.gpkg with columns:
      road_name, highway, geometry
    Skips if the file already exists.
    """
    import geopandas as gpd
    from shapely import wkb

    out_path = study.data_processed_dir / "roads" / "roads_clipped.gpkg"
    if out_path.exists():
        print("[env] roads_clipped.gpkg — already exists, skip")
        return

    roads_src = _DATA_DIR / "static" / "roads_canada.gpkg"
    if not roads_src.exists():
        print("[env] WARN: roads_canada.gpkg not found — skipping roads cache")
        return

    geom = wkb.loads(bytes(event.bbox.data))
    bbox = geom.bounds  # (lon_min, lat_min, lon_max, lat_max)

    roads = gpd.read_file(roads_src, bbox=bbox)
    roads = roads[roads["highway"].isin(_ROAD_TYPES)].copy()
    roads["name"] = roads["name"].fillna("").str.strip()
    roads["road_name"] = roads["name"].where(roads["name"] != "", roads["highway"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    roads[["road_name", "highway", "geometry"]].to_file(out_path, driver="GPKG")
    print(f"[env] roads_clipped.gpkg — {len(roads)} segments → {out_path}")


def _ensure_models() -> None:
    import wildfire_hotspot_prediction as whp
    whp.ensure_models(models_dir=_MODELS_DIR)


def _download_static_gpkg() -> None:
    import requests

    static_dir = _DATA_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    for filename, url in _STATIC_FILES.items():
        dest = static_dir / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            print(f"[env] {filename} — already exists, skip")
            continue
        print(f"[env] Downloading {filename} ...")
        try:
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
            print(f"[env] {filename} — {dest.stat().st_size / 1e6:.1f} MB")
        except Exception as e:
            log.error("[env] failed to download %s: %s", filename, e)
