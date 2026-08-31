"""
pipeline/check/builder_stages.py
----------------------------------
Per-stage runners: prediction, weather, spatial.
Called by builder.py orchestrator.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


def _run_prediction_stage(event, ts, study, fire_state, predictor, threshold, pred_cache, pbar=None) -> None:
    from pipeline.predict import run_prediction
    from pipeline.check.builder_slots import _write_status
    from tqdm import tqdm
    import traceback

    ts_base  = _timestep_dir(event.id, event.year, ts.slot_time)
    ml_dir   = ts_base / "prediction" / "ML"
    wd_dir   = ts_base / "prediction" / "wind_driven"

    print(f"[prediction_stage] ts={ts.id} marking running…")
    _write_status(ml_dir, "running")
    _write_status(wd_dir, "pending")
    try:
        t1 = pd.Timestamp(ts.nearest_t1)
        if t1.tzinfo is not None:
            t1 = t1.tz_localize(None)
        print(f"[prediction_stage] ts={ts.id} t1={t1} calling run_prediction…")
        run_prediction(
            ts_id         = ts.id,
            overpass_time = t1,
            study         = study,
            fire_state    = fire_state,
            predictor     = predictor,
            threshold     = threshold,
            out_dir       = ml_dir,
            pred_cache    = pred_cache,
        )
        print(f"[prediction_stage] ts={ts.id} run_prediction returned — writing done")
        _write_status(ml_dir, "done")
    except Exception as e:
        print(f"[prediction_stage] ts={ts.id} FAILED: {e}")
        traceback.print_exc()
        tqdm.write(f"[builder] prediction failed for ts {ts.id}: {e}")
        _write_status(ml_dir, "failed")


def _json_scalar(value):
    """Convert pandas/numpy scalar values into JSON-safe Python values."""
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value.item() if hasattr(value, "item") else value


def _export_thermal_observations(study, t1: pd.Timestamp, out_path: Path):
    """Export enriched FIRMS detections for one monitoring observation time."""
    from pipeline.predict.risk_zones import write_geojson

    enriched_path = study.data_processed_dir / "thermal" / "firms_enriched.parquet"
    history_path = study.data_processed_dir / "thermal" / "firms_history.parquet"
    source_path = enriched_path if enriched_path.exists() else history_path
    if not source_path.exists():
        raise FileNotFoundError(f"thermal FIRMS observations not found: {source_path}")

    observations = pd.read_parquet(source_path)
    observations["observed_at"] = pd.to_datetime(observations["observed_at"], utc=True)
    target = pd.Timestamp(t1)
    target = target.tz_localize("UTC") if target.tzinfo is None else target.tz_convert("UTC")
    subset = observations[observations["observed_at"].eq(target)].copy()

    property_columns = (
        "observed_at", "acq_date", "acq_time", "satellite", "instrument",
        "source_product", "confidence", "daynight", "frp", "bright_ti4",
        "bright_ti5", "inside_midc", "inside_industrial_polygon",
        "near_industrial_facility", "distance_to_midc_m",
        "distance_to_nearest_industry_m", "nearest_industry_type",
        "nearest_industry_name", "landcover_class", "landcover_group",
        "is_built_up", "is_forest", "is_cropland",
    )
    features = []
    for _, row in subset.iterrows():
        properties = {
            column: _json_scalar(row.get(column))
            for column in property_columns
            if column in subset.columns
        }
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [float(row["longitude"]), float(row["latitude"])],
            },
            "properties": properties,
        })
    write_geojson(out_path, features)
    return subset


def _run_thermal_monitoring_stage(event, ts, study) -> None:
    """Export observed thermal data without invoking the wildfire classifier."""
    from pipeline.check.builder_slots import _write_status
    from pipeline.predict.risk_zones import write_geojson
    import traceback

    ts_base = _timestep_dir(event.id, event.year, ts.slot_time)
    ml_dir = ts_base / "prediction" / "ML"
    wd_dir = ts_base / "prediction" / "wind_driven"
    hs_path = ts_base / "hotspot" / "hotspots.geojson"
    perimeter_path = ts_base / "perimeter" / "perimeter.geojson"

    _write_status(ml_dir, "running")
    _write_status(wd_dir, "pending")
    try:
        t1 = pd.Timestamp(ts.nearest_t1)
        if t1.tzinfo is not None:
            t1 = t1.tz_localize(None)

        hs_path.parent.mkdir(parents=True, exist_ok=True)
        observations = _export_thermal_observations(study, t1, hs_path)

        perimeter_path.parent.mkdir(parents=True, exist_ok=True)
        if not perimeter_path.exists():
            write_geojson(perimeter_path, [])

        ml_dir.mkdir(parents=True, exist_ok=True)
        for horizon in (3, 6, 12):
            risk_path = ml_dir / f"risk_zones_{horizon}h.geojson"
            if not risk_path.exists():
                write_geojson(risk_path, [])

        frp = pd.to_numeric(
            observations.get("frp", pd.Series(index=observations.index, dtype=float)),
            errors="coerce",
        )
        brightness = pd.to_numeric(
            observations.get("bright_ti4", pd.Series(index=observations.index, dtype=float)),
            errors="coerce",
        )
        count_true = lambda column: int(observations.get(column, pd.Series(dtype=bool)).fillna(False).astype(bool).sum())
        value_counts = lambda column: {
            str(key): int(value)
            for key, value in observations.get(column, pd.Series(dtype="string")).dropna().value_counts().items()
        }
        from pipeline.event_config import get_event_config

        config = get_event_config(event)
        thermal_dir = Path(study.data_processed_dir) / "thermal"
        persistence_metadata = {}
        classification_metadata = {}
        for filename, target in (
            ("persistence_metadata.json", persistence_metadata),
            ("classification_metadata.json", classification_metadata),
        ):
            metadata_path = thermal_dir / filename
            if metadata_path.exists():
                try:
                    target.update(json.loads(metadata_path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    log.warning("[thermal] unable to read %s", metadata_path)
        context = {
            "analysis_mode": "thermal_monitoring",
            "region": {
                "event_id": event.id,
                "region_id": config.region_id,
                "name": config.name,
                "state": config.state,
                "country_code": config.country_code,
                "monitoring_focus": config.monitoring_focus,
                "bbox": list(config.view_bbox or config.bbox),
            },
            "classification": {
                "status": "not_configured",
                "message": "Wildfire LR prediction is not applied to industrial thermal detections.",
            },
            "fire": {
                "burned_area_km2": None,
                "new_area_km2": None,
                "growth_rate_km2h": None,
                "n_hotspots": len(observations),
                "frp_sum": round(float(frp.fillna(0).sum()), 3),
            },
            "thermal": {
                "detection_count": len(observations),
                "frp_mean_mw": round(float(frp.mean()), 3) if frp.notna().any() else None,
                "frp_max_mw": round(float(frp.max()), 3) if frp.notna().any() else None,
                "brightness_ti4_max_k": round(float(brightness.max()), 2) if brightness.notna().any() else None,
                "inside_midc_count": count_true("inside_midc"),
                "inside_industrial_area_count": count_true("inside_industrial_polygon"),
                "near_industrial_facility_count": count_true("near_industrial_facility"),
                "confidence_counts": value_counts("confidence"),
                "landcover_group_counts": value_counts("landcover_group"),
                "classification_counts": classification_metadata.get("class_counts", {}),
                "emergency_candidate_count": classification_metadata.get("emergency_candidate_count", 0),
                "classification_mean_confidence": classification_metadata.get("mean_confidence"),
                "persistence_level_counts": persistence_metadata.get("persistence_level_counts", {}),
                "persistent_source_count": persistence_metadata.get("persistent_source_count", 0),
                "satellites": sorted(str(value) for value in observations.get(
                    "satellite", pd.Series(dtype="string"),
                ).dropna().unique()),
                "nearest_industries": sorted(str(value) for value in observations.get(
                    "nearest_industry_name", pd.Series(dtype="string"),
                ).dropna().unique()),
            },
            "fwi_t1": {},
            "observation_time": t1.isoformat(),
        }
        (ml_dir / "fire_context.json").write_text(
            json.dumps(context, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _write_status(ml_dir, "done")
        log.info(
            "[thermal] ts=%d exported %d observed hotspot(s); classifier skipped",
            ts.id,
            len(observations),
        )
    except Exception as exc:
        log.error("[thermal] ts=%d failed: %s", ts.id, exc)
        traceback.print_exc()
        _write_status(ml_dir, "failed")


def _run_weather_stage(event, ts, study, pbar=None) -> None:
    from pipeline.weather import build_weather_forecast
    from tqdm import tqdm

    out_dir = _timestep_dir(event.id, event.year, ts.slot_time) / "weather"
    if (out_dir / "forecast.json").exists():
        print(f"[weather_stage] ts={ts.id} already done — skip")
        return
    print(f"[weather_stage] ts={ts.id} building weather forecast…")
    try:
        t1 = pd.Timestamp(ts.nearest_t1)
        if t1.tzinfo is not None:
            t1 = t1.tz_localize(None)
        build_weather_forecast(study=study, t1=t1, out_dir=out_dir)
        print(f"[weather_stage] ts={ts.id} done")
    except Exception as e:
        print(f"[weather_stage] ts={ts.id} FAILED: {e}")
        tqdm.write(f"[builder] weather forecast failed for ts {ts.id}: {e}")


def _run_spatial_stage(event, ts, pbar=None) -> None:
    from pipeline.spatial import run_spatial_analysis
    from pipeline.check.builder_slots import _write_status
    from tqdm import tqdm
    import traceback

    out_dir = _timestep_dir(event.id, event.year, ts.slot_time) / "spatial_analysis"
    print(f"[spatial_stage] ts={ts.id} starting…")
    _write_status(out_dir, "running")
    try:
        run_spatial_analysis(event.id, ts.id, out_dir)
        print(f"[spatial_stage] ts={ts.id} done")
        _write_status(out_dir, "done")
    except Exception as e:
        print(f"[spatial_stage] ts={ts.id} FAILED: {e}")
        traceback.print_exc()
        tqdm.write(f"[builder] spatial analysis failed for ts {ts.id}: {e}")
        _write_status(out_dir, "failed")


def _run_spatial_stage_crowd(event, ts, crow_dir: Path, sp_crowd: Path) -> None:
    """Run spatial analysis using ML_crowd/ risk zones and hotspots_crowd.geojson."""
    from pipeline.spatial import run_spatial_analysis
    from pipeline.check.builder_slots import _write_status
    import traceback

    ts_base      = _timestep_dir(event.id, event.year, ts.slot_time)
    hotspot_path = ts_base / "hotspot" / "hotspots_crowd.geojson"

    print(f"[spatial_crowd] ts={ts.id} starting…")
    _write_status(sp_crowd, "running")
    try:
        run_spatial_analysis(
            event_id     = event.id,
            ts_id        = ts.id,
            out_dir      = sp_crowd,
            pred_dir     = crow_dir,
            hotspot_path = hotspot_path,
        )
        print(f"[spatial_crowd] ts={ts.id} done")
        _write_status(sp_crowd, "done")
    except Exception as e:
        print(f"[spatial_crowd] ts={ts.id} FAILED: {e}")
        traceback.print_exc()
        _write_status(sp_crowd, "failed")


def _merge_fire_context(spatial_out_dir: Path, road_summary: list) -> None:
    """Append road_summary into the sibling prediction/ML/fire_context.json."""
    ctx_path = spatial_out_dir.parent / "prediction" / "ML" / "fire_context.json"
    if not ctx_path.exists():
        return
    try:
        ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
        ctx["road_summary"] = road_summary
        text = json.dumps(ctx, ensure_ascii=False, indent=2)
        text = re.sub(r'\bNaN\b', 'null', text)
        text = re.sub(r'\bInfinity\b', 'null', text)
        ctx_path.write_text(text, encoding="utf-8")
    except Exception as e:
        log.warning("[builder] could not merge road_summary into fire_context: %s", e)


def _load_actual_perimeter_cache(event):
    """Load and AOI-clip actual_perimeter.gpkg once per event. Returns GeoDataFrame (EPSG:3978)."""
    import geopandas as gpd
    import pyproj
    from shapely.geometry import box as _box
    from pipeline.spatial.spatial_helpers import event_bbox

    _DATA_DIR = Path(__file__).resolve().parents[3] / "data"
    gpkg_path = _DATA_DIR / "static" / "actual_perimeter" / "actual_perimeter.gpkg"
    if not gpkg_path.exists():
        return None

    bbox_wgs84 = event_bbox(event)
    _t = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3978", always_xy=True)
    minx, miny = _t.transform(bbox_wgs84[0], bbox_wgs84[1])
    maxx, maxy = _t.transform(bbox_wgs84[2], bbox_wgs84[3])
    aoi = _box(minx, miny, maxx, maxy)

    gdf = gpd.read_file(str(gpkg_path), bbox=(minx, miny, maxx, maxy))
    if gdf.empty:
        return None
    gdf = gdf[gdf.geometry.intersects(aoi)].copy()
    if gdf.empty:
        return None

    date_col = "date"
    if date_col not in gdf.columns:
        date_col = next(
            (c for c in gdf.columns if c.lower() in ("fire_date", "acq_date", "perimeter_date")),
            None,
        )
    if date_col is None:
        return None

    gdf = gdf.rename(columns={date_col: "_date"})
    gdf["_date"] = pd.to_datetime(gdf["_date"]).dt.date
    return gdf


def _load_ros_weights_cache(study) -> dict:
    """Compute ROS cumulative weights from ros_hourly.parquet. Returns {hour: fraction}."""
    ros_path = study.data_processed_dir / "weather" / "ros_hourly.parquet"
    return _compute_ros_weights(ros_path)


def _run_perimeter_stage(event, ts, study, fire_state, ap_cache=None, ros_cache=None) -> None:
    """Stage 1 addition: export perimeter, hotspots, and ROS-weighted actual perimeters.

    Outputs (idempotent — skips if files already exist):
        perimeter/perimeter.geojson
        hotspot/hotspots.geojson
        actual_perimeter/{0h,3h,6h,12h}.geojson
    """
    from pipeline.predict.prediction import _export_perimeter, _export_hotspots
    from tqdm import tqdm

    ts_base = _timestep_dir(event.id, event.year, ts.slot_time)
    t1 = pd.Timestamp(ts.nearest_t1)
    if t1.tzinfo is not None:
        t1 = t1.tz_localize(None)

    # ── Perimeter ────────────────────────────────────────────────────────────────
    perim_dir  = ts_base / "perimeter"
    perim_path = perim_dir / "perimeter.geojson"
    if not perim_path.exists():
        perim_dir.mkdir(parents=True, exist_ok=True)
        print(f"[perimeter_stage] ts={ts.id} exporting perimeter…")
        try:
            _export_perimeter(fire_state, t1, perim_path)
            print(f"[perimeter_stage] ts={ts.id} perimeter done")
        except Exception as e:
            print(f"[perimeter_stage] ts={ts.id} perimeter FAILED: {e}")
            tqdm.write(f"[builder] perimeter export failed for ts {ts.id}: {e}")
    else:
        print(f"[perimeter_stage] ts={ts.id} perimeter already exists — skip")

    # ── Hotspots ─────────────────────────────────────────────────────────────────
    hs_dir  = ts_base / "hotspot"
    hs_path = hs_dir / "hotspots.geojson"
    if not hs_path.exists():
        hs_dir.mkdir(parents=True, exist_ok=True)
        print(f"[perimeter_stage] ts={ts.id} exporting hotspots…")
        try:
            _export_hotspots(study, t1, hs_path)
            print(f"[perimeter_stage] ts={ts.id} hotspots done")
        except Exception as e:
            print(f"[perimeter_stage] ts={ts.id} hotspots FAILED: {e}")
            tqdm.write(f"[builder] hotspot export failed for ts {ts.id}: {e}")
    else:
        print(f"[perimeter_stage] ts={ts.id} hotspots already exist — skip")

    # ── Actual perimeters (ROS-weighted) ─────────────────────────────────────────
    print(f"[perimeter_stage] ts={ts.id} building actual perimeters…")
    _build_actual_perimeters(event, ts, ap_cache=ap_cache, ros_cache=ros_cache)
    print(f"[perimeter_stage] ts={ts.id} actual perimeters done")


def _build_actual_perimeters(event, ts, ap_cache=None, ros_cache=None) -> None:
    """Pre-build ROS-weighted actual perimeter GeoJSONs for +0h/+3h/+6h/+12h.

    Each source polygon in the GPKG is kept as a separate feature.
    Growth model per polygon (all geometry ops in EPSG:3978 metres, output in WGS84):
        growth_i  = polygon_i.difference(yesterday_union)
        base_i    = polygon_i.intersection(yesterday_union)  (or empty if no t-1)
        scaled_i  = base_i.union(scale(growth_i, weight[h]))
        weight[h] = cumROS[slot_hour + h] / totalROS[0..24]   (linear h/24 fallback)
    """
    import shapely.affinity as sa
    from shapely.ops import unary_union, transform as shp_transform
    from shapely.validation import make_valid
    import pyproj
    from tqdm import tqdm

    ts_base = _timestep_dir(event.id, event.year, ts.slot_time)
    out_dir = ts_base / "actual_perimeter"
    horizons = (0, 3, 6, 12)

    if all((out_dir / f"{h}h.geojson").exists() for h in horizons):
        return

    if ap_cache is None:
        return  # no gpkg data available

    slot = pd.Timestamp(ts.slot_time)
    if slot.tzinfo is not None:
        slot = slot.tz_localize(None)
    slot_hour = slot.hour

    gdf = ap_cache  # already AOI-clipped, date col renamed to "_date"

    # Shift back one day: government perimeters are mapped at end-of-day,
    # so "yesterday" is the base and "today" is the growth target.
    # This ensures date_t1 has real data at slot time, making horizons grow.
    date_t0 = (slot - pd.Timedelta(days=1)).date()  # yesterday
    date_t1 = slot.date()                            # today (growth target)

    # Yesterday's individual polygons (each stays separate — base for display)
    sub_t0 = gdf[gdf["_date"] == date_t0]
    if sub_t0.empty:
        return  # no actual data for yesterday — skip

    # Today's union (growth target)
    sub_t1 = gdf[gdf["_date"] == date_t1]
    if sub_t1.empty:
        geom_t1 = None
    else:
        valid_t1 = [make_valid(g) for g in sub_t1.geometry if g is not None and not g.is_empty]
        valid_t1 = [g for g in valid_t1 if not g.is_empty]
        if valid_t1:
            geom_t1 = unary_union(valid_t1)
            if not geom_t1.is_valid:
                geom_t1 = make_valid(geom_t1)
        else:
            geom_t1 = None

    # ── ROS weights ───────────────────────────────────────────────────────────────
    weights = ros_cache if ros_cache is not None else {}  # {hour 0..35: cumulative fraction}

    # ── Reprojection: EPSG:3978 → WGS84 ─────────────────────────────────────────
    transformer = pyproj.Transformer.from_crs("EPSG:3978", "EPSG:4326", always_xy=True)

    def _to_wgs84(geom):
        return shp_transform(transformer.transform, geom)

    # Union today's polygons then decompose into non-overlapping parts
    def _union_and_decompose(sub):
        raw = [make_valid(g) for g in sub.geometry if g is not None and not g.is_empty]
        raw = [g for g in raw if not g.is_empty]
        if not raw:
            return []
        merged = unary_union(raw)
        if not merged.is_valid:
            merged = make_valid(merged)
        # Decompose MultiPolygon / GeometryCollection into individual Polygons
        geom_type = merged.geom_type
        if geom_type == "Polygon":
            return [merged]
        elif geom_type == "MultiPolygon":
            return list(merged.geoms)
        else:
            # GeometryCollection: extract only Polygon parts
            return [g for g in merged.geoms if g.geom_type in ("Polygon", "MultiPolygon")]

    today_parts = _union_and_decompose(sub_t0)
    if not today_parts:
        return

    # Pre-compute per-polygon growth once (reused across horizons)
    # growth_i = (tomorrow_union - geom_i) scaled towards tomorrow
    poly_data = []  # list of (geom_i, growth_i)
    for geom_i in today_parts:
        if geom_t1 is not None:
            try:
                growth_i = make_valid(geom_t1.difference(geom_i))
            except Exception:
                growth_i = None
        else:
            growth_i = None
        poly_data.append((geom_i, growth_i))

    if not poly_data:
        return

    # ── Build + write ─────────────────────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    for delta_h in horizons:
        out_path = out_dir / f"{delta_h}h.geojson"
        if out_path.exists():
            continue
        h_abs = slot_hour + delta_h
        w     = weights.get(h_abs, h_abs / 24.0)

        features = []
        for geom_i, growth_i in poly_data:
            try:
                if growth_i is not None and not growth_i.is_empty and w > 0:
                    # Scale growth outward from T1's centroid so the ring expands
                    # away from the base perimeter (not from the growth ring's own centre)
                    scaled = sa.scale(growth_i, xfact=w, yfact=w, origin=geom_i.centroid)
                    geom_h = make_valid(geom_i.union(scaled))
                else:
                    geom_h = geom_i  # no tomorrow data or weight=0 → show today as-is
            except Exception:
                geom_h = geom_i  # fallback to today's observed polygon

            features.append({
                "type": "Feature",
                "geometry": _to_wgs84(geom_h).__geo_interface__,
                "properties": {
                    "horizon":   f"+{delta_h}h",
                    "slot_hour": slot_hour,
                    "weight":    round(w, 4),
                    "date":      str(date_t0),
                },
            })

        out_path.write_text(
            json.dumps({"type": "FeatureCollection", "features": features},
                       ensure_ascii=False),
            encoding="utf-8",
        )


def _compute_ros_weights(ros_path: Path) -> dict:
    """Compute cumulative ROS fractions keyed by absolute hour (0..35).

    Uses mean diurnal ROS pattern (averaged across all days and grid cells).
    Returns {hour: fraction} where fraction = cumROS[hour] / totalROS[0..24].
    Hours 24-35 wrap to the next day's pattern (hour % 24).
    Falls back to empty dict (caller uses linear h/24 fallback).
    """
    if not ros_path.exists():
        return {}
    try:
        df = pd.read_parquet(ros_path)
        if "valid_time" not in df.columns or "ros" not in df.columns:
            return {}

        df["_hour"] = pd.to_datetime(df["valid_time"]).dt.hour
        # Mean ROS per hour-of-day across all days and grid cells
        mean_ros = df.groupby("_hour")["ros"].mean().reindex(range(24), fill_value=0.0)

        total = mean_ros.sum()   # total ROS in one diurnal cycle
        if total <= 0:
            return {}

        # Build cumulative weights for hours 0..35 (max: slot 23:00 + 12h = hour 35)
        # weight[h] = fraction of daily ROS accumulated AFTER h hours have elapsed
        # weight[0] = 0 (at the slot moment, no hours have elapsed)
        # weight[3] = (ros[0]+ros[1]+ros[2]) / total
        weights = {}
        cum = 0.0
        for h in range(36):
            weights[h] = min(cum / total, 1.0)
            cum += mean_ros[h % 24]
        return weights
    except Exception:
        return {}


def _timestep_dir(event_id: int, year: int, slot_time) -> Path:
    _DATA_DIR = Path(__file__).resolve().parents[3] / "data"
    ts_str = pd.Timestamp(slot_time).strftime("%Y-%m-%dT%H%M")
    return _DATA_DIR / "events" / f"{year}_{event_id:04d}" / "timesteps" / ts_str
