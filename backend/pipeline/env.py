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
    log.info("[env] environment preparation started")
    log.info("[env] checking shared assets")
    _ensure_models()
    _download_static_gpkg()

    with app.app_context():
        from db.models import FireEvent
        events = FireEvent.query.all()
        if not events:
            log.warning("[env] No FireEvents in DB")
            return
        for event in events:
            log.info("[env] event %d (%s) preparation started", event.id, event.name)
            try:
                _prepare_event(event)
                log.info("[env] event %d environment preparation complete", event.id)
            except Exception as exc:
                log.exception(
                    "[env] event %d (%s) environment preparation failed: %s",
                    event.id,
                    event.name,
                    exc,
                )

    log.info("[env] environment preparation finished")


def _create_event_timesteps(event) -> None:
    """Build DB slots as soon as an event's real observations are available."""
    if event.end_date is None:
        log.info("[builder] event %d is realtime; skipping replay timesteps", event.id)
        return

    try:
        from pipeline.check.builder import (
            _build_timestep,
            _get_build_runtime,
            build_event_timesteps,
        )
        from pipeline.event_config import uses_wildfire_model

        timesteps = build_event_timesteps(event)
        log.info(
            "[builder] event %d automatic timestep generation finished: %d available",
            event.id,
            len(timesteps),
        )
        if not uses_wildfire_model(event):
            assets, predictor, threshold = _get_build_runtime(event)
            for timestep in timesteps:
                _build_timestep(event, timestep, assets, predictor, threshold)
            log.info(
                "[builder] event %d monitoring outputs pre-built for %d observation(s)",
                event.id,
                len(timesteps),
            )
    except Exception as exc:
        log.exception(
            "[builder] event %d automatic timestep generation failed: %s",
            event.id,
            exc,
        )


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
    from pipeline.event_config import uses_wildfire_model

    study = _make_study(event)

    print("[env] Fetching landmarks ...")
    _fetch_landmarks(event, study)

    print("[env] Pre-building roads cache ...")
    _prebuild_roads(event, study)

    print("[env] Pre-building population cache ...")
    _prebuild_population(event, study)

    print("[env] Checking ERA5 ...")
    whp.ensure_era5_coverage(study)

    hotspots_path = study.data_processed_dir / "firms" / "hotspots.parquet"
    hotspot_data = None
    if hotspots_path.exists():
        log.info("[env] event %d processed FIRMS hotspots already exist: %s", event.id, hotspots_path)
    else:
        log.info("[env] event %d collecting FIRMS hotspots", event.id)
        whp.collect_hotspots(study)

        log.info("[env] event %d preprocessing FIRMS hotspots", event.id)
        hotspot_data = whp.preprocess_hotspots(study)

    if uses_wildfire_model(event):
        from wildfire_hotspot_prediction.training.fire_state import build_fire_state, save_fire_state

        fire_state_path = study.training_dir / "fire_state.pkl"
        if fire_state_path.exists():
            log.info("[env] event %d fire_state.pkl already exists: %s", event.id, fire_state_path)
        else:
            if hotspot_data is None:
                hotspot_data = whp.preprocess_hotspots(study)
            log.info("[env] event %d generating fire_state.pkl", event.id)
            fire_state = build_fire_state(hotspot_data)
            save_fire_state(fire_state, fire_state_path)
            log.info(
                "[env] event %d fire_state.pkl generated with %d overpasses: %s",
                event.id,
                len(fire_state.steps),
                fire_state_path,
            )

    if not uses_wildfire_model(event):
        from pipeline.thermal import ensure_thermal_context, ensure_thermal_history

        log.info("[env] event %d preparing historical thermal observations", event.id)
        try:
            ensure_thermal_history(event, study)
        except Exception as exc:
            # Historical context is additive. Keep the compact real-observation
            # replay usable when an archive request or normalization fails.
            log.exception(
                "[env] event %d historical thermal preparation failed: %s",
                event.id,
                exc,
            )

        log.info("[env] event %d preparing industrial and land-cover context", event.id)
        try:
            ensure_thermal_context(event, study)
        except Exception as exc:
            log.exception(
                "[env] event %d thermal context preparation failed: %s",
                event.id,
                exc,
            )

    # Slot persistence depends only on real observations. Do it immediately,
    # so optional fuel/terrain preparation cannot leave the API empty.
    _create_event_timesteps(event)

    if not uses_wildfire_model(event):
        log.info(
            "[env] event %d is thermal monitoring; wildfire model assets are not prepared",
            event.id,
        )
        return

    if not (study.landcover_raw_dir / "fuel_type.tif").exists():
        print("[env] Downloading fuel type map ...")
        whp.collect_environment(study, sources=["landcover"])
    if not (study.landcover_dir / "fuel_type.tif").exists():
        print("[env] Preprocessing fuel type map ...")
        whp.preprocess_environment(study, sources=["landcover"])

    terrain_raw_dir = study.project_dir / "data_raw" / "terrain"
    terrain_processed_dir = study.project_dir / "data_processed" / "terrain"
    terrain_outputs = tuple(
        terrain_processed_dir / filename
        for filename in ("dtm.tif", "slope.tif", "aspect.tif")
    )
    if not all(path.exists() for path in terrain_outputs):
        if not (terrain_raw_dir / "dtm.tif").exists():
            print("[env] Downloading terrain (DEM, slope, aspect) ...")
            whp.collect_environment(study, sources=["terrain"])
        _patch_terrain_crs(terrain_raw_dir)
        print("[env] Preprocessing terrain ...")
        whp.preprocess_environment(study, sources=["terrain"])
    else:
        print("[env] Processed terrain — already exists, skip")

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
        from pipeline.event_config import get_event_config
        log.warning("[env] Overpass unavailable — falling back to Nominatim")
        landmarks = _nominatim_places(
            lat_min, lon_min, lat_max, lon_max,
            country_code=get_event_config(event).country_code,
        )

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


def _nominatim_places(lat_min, lon_min, lat_max, lon_max,
                      country_code: str = "") -> list[dict]:
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
            params = {"q": place_type, "format": "json", "limit": 20,
                      "viewbox": viewbox, "bounded": 1}
            if country_code:
                params["countrycodes"] = country_code
            r = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params=params,
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
    """Build the configured road provider into the event-scoped road contract.

    Saves data_processed/roads/roads_clipped.gpkg with columns:
      road_name, highway, geometry
    Skips if the file already exists.
    """
    import geopandas as gpd
    from pipeline.event_config import get_event_config

    out_path = study.data_processed_dir / "roads" / "roads_clipped.gpkg"
    if out_path.exists():
        print("[env] roads_clipped.gpkg — already exists, skip")
        return

    config = get_event_config(event)
    provider = {
        "canada_static": _roads_from_canada_static,
        "osm": _roads_from_osm,
    }.get(config.roads_provider)
    if provider is None:
        log.warning("[env] event %d has no roads provider", event.id)
        return

    roads = provider(event)
    if roads is None or roads.empty:
        log.warning(
            "[env] event %d roads provider %s returned no roads",
            event.id,
            config.roads_provider,
        )
        return

    roads = roads.to_crs("EPSG:4326")
    roads = roads[roads["highway"].isin(_ROAD_TYPES)].copy()
    if roads.empty:
        log.warning("[env] event %d has no supported major roads in its AOI", event.id)
        return
    roads["name"] = roads["name"].fillna("").astype(str).str.strip()
    roads["road_name"] = roads["name"].where(roads["name"] != "", roads["highway"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    roads[["road_name", "highway", "geometry"]].to_file(out_path, driver="GPKG")
    print(f"[env] roads_clipped.gpkg — {len(roads)} segments → {out_path}")


def _roads_from_canada_static(event):
    import geopandas as gpd
    from pipeline.spatial.spatial_helpers import event_bbox

    roads_src = _DATA_DIR / "static" / "roads_canada.gpkg"
    if not roads_src.exists():
        print("[env] WARN: roads_canada.gpkg not found — skipping roads cache")
        return None

    return gpd.read_file(roads_src, bbox=event_bbox(event))


def _roads_from_osm(event):
    """Read major OSM ways from Overpass and return the normalized source rows."""
    import geopandas as gpd
    import requests
    from shapely.geometry import LineString
    from pipeline.spatial.spatial_helpers import event_bbox

    lon_min, lat_min, lon_max, lat_max = event_bbox(event)
    road_pattern = "(motorway|trunk|primary|secondary)(_link)?"
    query = (
        "[out:json][timeout:45];"
        f'way["highway"~"^{road_pattern}$"]'
        f"({lat_min},{lon_min},{lat_max},{lon_max});"
        "out tags geom;"
    )

    for url in (
        "https://overpass-api.de/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
    ):
        try:
            response = requests.post(
                url,
                data=query.encode("utf-8"),
                headers={
                    "User-Agent": "wildfire-decision-support/1.0",
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=60,
            )
            if response.status_code != 200:
                log.warning("[env] OSM roads request returned HTTP %d via %s", response.status_code, url)
                continue
            rows = []
            for way in response.json().get("elements", []):
                coordinates = [
                    (node["lon"], node["lat"])
                    for node in way.get("geometry", [])
                    if "lon" in node and "lat" in node
                ]
                if len(coordinates) < 2:
                    continue
                tags = way.get("tags", {})
                rows.append({
                    "name": tags.get("name") or tags.get("ref") or "",
                    "highway": tags.get("highway", ""),
                    "geometry": LineString(coordinates),
                })
            if rows:
                return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
        except Exception as exc:
            log.warning("[env] OSM roads request failed via %s: %s", url, exc)
    return None


def _prebuild_population(event, study) -> None:
    """Normalize configured population data into an event-scoped GeoPackage."""
    import geopandas as gpd
    from pipeline.event_config import get_event_config
    from pipeline.spatial.spatial_helpers import event_bbox

    out_path = study.data_processed_dir / "spatial" / "population.gpkg"
    if out_path.exists():
        print("[env] population.gpkg — already exists, skip")
        return

    config = get_event_config(event)
    if config.population_provider == "none":
        log.info("[env] event %d has no configured population provider", event.id)
        return
    if config.population_provider != "canada_census":
        log.warning(
            "[env] event %d has unsupported population provider %s",
            event.id,
            config.population_provider,
        )
        return

    source = _DATA_DIR / "static" / "population.gpkg"
    if not source.exists():
        log.warning("[env] population source not found: %s", source)
        return
    population = gpd.read_file(
        source,
        bbox=event_bbox(event),
        layer="dissemination_areas",
    )
    if population.empty:
        log.warning("[env] event %d population provider returned no features", event.id)
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    population.to_file(out_path, layer="population", driver="GPKG")
    print(f"[env] population.gpkg — {len(population)} areas → {out_path}")


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
