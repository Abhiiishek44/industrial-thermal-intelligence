"""
pipeline/check/builder.py
--------------------------
Event orchestration + on-demand timestep build.

Slot helpers  → builder_slots.py
Stage runners → builder_stages.py
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from pipeline.check.builder_slots import (
    _generate_slots, _upsert_timesteps, _write_status,
)
from pipeline.check.builder_stages import (
    _run_prediction_stage, _run_weather_stage, _run_spatial_stage,
    _run_perimeter_stage,
)

log = logging.getLogger(__name__)

_DATA_DIR   = Path(__file__).resolve().parents[3] / "data"
_MODELS_DIR = _DATA_DIR / "static" / "models"
_N_WORKERS  = 2


def _patch_whp_caches() -> None:
    """Monkey-patch wildfire_hotspot_prediction to cache expensive disk reads.

    build_prediction_features() calls load_fire_state() and _load_selector()
    on every invocation, causing hundreds of redundant disk reads during the
    priority pre-build (93 slots × 3 horizons = 279 calls).  After this patch,
    each unique path is read from disk only once.
    """
    import threading

    try:
        import wildfire_hotspot_prediction.training.fire_state as _fs_mod
        import wildfire_hotspot_prediction.build_prediction_data.feature_builder as _fb_mod

        # ── fire_state cache ──────────────────────────────────────────────────
        _fs_cache: dict = {}
        _fs_lock = threading.Lock()
        _orig_load_fs = _fs_mod.load_fire_state

        def _cached_load_fire_state(path):
            key = str(path)
            with _fs_lock:
                if key not in _fs_cache:
                    _fs_cache[key] = _orig_load_fs(path)
                return _fs_cache[key]

        _fs_mod.load_fire_state = _cached_load_fire_state
        _fb_mod.load_fire_state = _cached_load_fire_state

        # ── selector caches ───────────────────────────────────────────────────
        # _load_selector reads selectors.parquet — cache the result per t1.
        # When selectors.parquet is absent it returns None, and the caller falls
        # through to build_receptor_selector (expensive geometry op). Cache that
        # too so each unique t1_actual is only computed once across all horizons.
        _sel_cache: dict = {}
        _sel_lock = threading.Lock()
        _orig_load_sel = _fb_mod._load_selector

        def _cached_load_selector(train_dir, t1_actual):
            key = (str(train_dir), pd.Timestamp(t1_actual).isoformat())
            with _sel_lock:
                if key not in _sel_cache:
                    _sel_cache[key] = _orig_load_sel(train_dir, t1_actual)
                return _sel_cache[key]

        _fb_mod._load_selector = _cached_load_selector

        # build_receptor_selector is only called when _load_selector returns None.
        # It depends only on (t1_actual, fire_state) — cache by t1_actual isoformat.
        from wildfire_hotspot_prediction.training.receptor_selector import build_receptor_selector as _orig_brs
        _brs_cache: dict = {}
        _brs_lock = threading.Lock()

        def _cached_build_receptor_selector(t1_actual, fire_state):
            key = pd.Timestamp(t1_actual).isoformat()
            with _brs_lock:
                if key not in _brs_cache:
                    _brs_cache[key] = _orig_brs(t1_actual, fire_state)
                return _brs_cache[key]

        _fb_mod.build_receptor_selector = _cached_build_receptor_selector

        log.info("[builder] whp disk-read caches applied (fs + selector + receptor_selector)")
    except Exception as exc:
        log.warning("[builder] whp cache patch failed (non-fatal): %s", exc)


_patch_whp_caches()


# ── Loaders (module-level cache — loaded once per process) ────────────────────

import threading as _threading
_event_cache: dict = {}       # event_id → {study, fire_state, pred_cache, ap_cache, ros_cache}
_event_cache_lock = _threading.Lock()
_predictor_cache: dict = {}   # model_name → WildfirePredictor
_predictor_lock  = _threading.Lock()
_threshold_cache: list = []   # [value]  (list so we can mutate it)


def _load_study(event):
    import wildfire_hotspot_prediction as whp
    from shapely import wkb
    geom = wkb.loads(bytes(event.bbox.data))
    lon_min, lat_min, lon_max, lat_max = geom.bounds
    project_dir = _DATA_DIR / "events" / f"{event.year}_{event.id:04d}"
    return whp.Study(
        name        = event.name,
        bbox        = (lon_min, lat_min, lon_max, lat_max),
        start_date  = event.start_date.strftime("%Y-%m-%d"),
        end_date    = event.end_date.strftime("%Y-%m-%d"),
        project_dir = project_dir,
    )


def _load_fire_state(study):
    from wildfire_hotspot_prediction.training.fire_state import load_fire_state
    path = study.data_processed_dir / "training" / "fire_state.pkl"
    if not path.exists():
        raise FileNotFoundError(f"fire_state.pkl not found: {path}")
    return load_fire_state(path)


def _load_predictor():
    import wildfire_hotspot_prediction as whp
    key = "lr_steps"
    with _predictor_lock:
        if key not in _predictor_cache:
            _predictor_cache[key] = whp.WildfirePredictor(models_dir=_MODELS_DIR, model_name=key)
        return _predictor_cache[key]


def _load_threshold() -> float:
    if not _threshold_cache:
        from pipeline.predict.risk_zones import load_youden_threshold
        _threshold_cache.append(load_youden_threshold(_MODELS_DIR))
    return _threshold_cache[0]


def _get_event_assets(event):
    """Return cached (study, fire_state, pred_cache, ap_cache, ros_cache) for event.
    Loads from disk on first call, returns cached objects thereafter.
    """
    import wildfire_hotspot_prediction as whp
    from pipeline.check.builder_stages import _load_actual_perimeter_cache, _load_ros_weights_cache

    with _event_cache_lock:
        if event.id not in _event_cache:
            study      = _load_study(event)
            fire_state = _load_fire_state(study)
            pred_cache = whp.build_prediction_cache(study)
            ap_cache   = _load_actual_perimeter_cache(event)
            ros_cache  = _load_ros_weights_cache(study)
            _event_cache[event.id] = dict(
                study=study, fire_state=fire_state, pred_cache=pred_cache,
                ap_cache=ap_cache, ros_cache=ros_cache,
            )
            log.info("[builder] event %d assets loaded and cached", event.id)
        return _event_cache[event.id]


# ── Per-event orchestration ───────────────────────────────────────────────────

def build_playback_events() -> None:
    """Pre-compute all replay events. Must run inside Flask app context."""
    from db.models import FireEvent
    events = FireEvent.query.filter(FireEvent.end_date.isnot(None)).all()
    log.info("[builder] %d playback event(s) to process", len(events))
    for event in events:
        try:
            _build_event(event)
        except Exception as e:
            log.error("[builder] event %d (%s) failed: %s", event.id, event.name, e)


def _build_event(event) -> None:
    log.info("[builder] building event %d: %s", event.id, event.name)
    try:
        assets    = _get_event_assets(event)
        predictor = _load_predictor()
        threshold = _load_threshold()
    except Exception as e:
        log.error("[builder] event %d setup failed: %s", event.id, e)
        return

    steps     = sorted(assets["fire_state"].steps)
    slots     = _generate_slots(event)
    timesteps = _upsert_timesteps(event.id, slots, steps)
    print(f"[builder] {event.name}: {len(slots)} slots, {len(steps)} overpasses")

    from tqdm import tqdm
    from flask import current_app
    _app = current_app._get_current_object()

    def _task(ts):
        with _app.app_context():
            _build_timestep(event, ts, assets, predictor, threshold)
        return ts.id

    pool = ThreadPoolExecutor(max_workers=_N_WORKERS)
    try:
        with tqdm(total=len(timesteps), desc=f"  {event.name}", unit="ts", dynamic_ncols=True) as pbar:
            futures = {pool.submit(_task, ts): ts for ts in timesteps}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    ts = futures[fut]
                    log.error("[builder] ts %d failed: %s", ts.id, e)
                pbar.update(1)
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=False)


def _build_timestep(event, ts, assets: dict, predictor, threshold, pbar=None) -> None:
    from pipeline.check.builder_slots import _read_status, _write_status

    study      = assets["study"]
    fire_state = assets["fire_state"]
    pred_cache = assets["pred_cache"]
    ap_cache   = assets["ap_cache"]
    ros_cache  = assets["ros_cache"]

    ts_str  = pd.Timestamp(ts.slot_time).strftime("%Y-%m-%dT%H%M")
    ts_base = _DATA_DIR / "events" / f"{event.year}_{event.id:04d}" / "timesteps" / ts_str
    ml_dir  = ts_base / "prediction" / "ML"
    sp_dir  = ts_base / "spatial_analysis"

    print(f"[build_timestep] ts={ts.id} ({ts_str}) — weather…")
    _run_weather_stage(event, ts, study, pbar)
    print(f"[build_timestep] ts={ts.id} — perimeter…")
    _run_perimeter_stage(event, ts, study, fire_state, ap_cache, ros_cache)

    ml_st = _read_status(ml_dir)
    print(f"[build_timestep] ts={ts.id} — ml_status={ml_st}")
    if ml_st not in ("done", "failed"):
        print(f"[build_timestep] ts={ts.id} — running prediction…")
        _run_prediction_stage(event, ts, study, fire_state, predictor, threshold, pred_cache, pbar)
        print(f"[build_timestep] ts={ts.id} — prediction done, ml_status={_read_status(ml_dir)}")

    if _read_status(ml_dir) == "done" and _read_status(sp_dir) not in ("done", "failed"):
        print(f"[build_timestep] ts={ts.id} — spatial analysis…")
        _run_spatial_stage(event, ts, pbar)
        print(f"[build_timestep] ts={ts.id} — spatial done")


# ── Startup: slots + weather only ─────────────────────────────────────────────

def _build_selectors_parquet(study, fire_state) -> None:
    """Pre-compute receptor selectors for every overpass and save to selectors.parquet.

    build_prediction_features() tries to load selectors.parquet first; if missing it
    calls build_receptor_selector() on every prediction call (very slow).  Building
    the parquet once at startup eliminates that cost entirely.
    """
    import geopandas as gpd
    from shapely.geometry import base as _shp_base
    from wildfire_hotspot_prediction.training.receptor_selector import build_receptor_selector

    out_path = study.data_processed_dir / "training" / "selectors.parquet"
    if out_path.exists():
        return

    steps = sorted(fire_state.steps)
    rows = []
    for t1 in steps:
        geom = build_receptor_selector(t1, fire_state)
        rows.append({"T1": t1, "geometry": geom})

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:3978")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(out_path)
    print(f"[builder] selectors.parquet — {len(rows)} overpasses → {out_path}")


def build_slots_only() -> None:
    """Create EventTimestep DB rows and build selectors.parquet. No weather/perimeter here."""
    from db.models import FireEvent
    events = FireEvent.query.filter(FireEvent.end_date.isnot(None)).all()
    for event in events:
        try:
            study      = _load_study(event)
            fire_state = _load_fire_state(study)
            slots      = _generate_slots(event)
            steps      = sorted(fire_state.steps)
            timesteps  = _upsert_timesteps(event.id, slots, steps)
            print(f"[builder] {event.name}: {len(slots)} slots created")

            _build_selectors_parquet(study, fire_state)
        except Exception as e:
            log.error("[builder] build_slots_only event %d failed: %s", event.id, e)


def build_weather_perimeter() -> None:
    """Build weather + perimeter for all timesteps. Runs concurrently with priority prediction."""
    from db.models import FireEvent
    from pipeline.check.builder_stages import _load_actual_perimeter_cache, _load_ros_weights_cache
    events = FireEvent.query.filter(FireEvent.end_date.isnot(None)).all()
    for event in events:
        try:
            study      = _load_study(event)
            fire_state = _load_fire_state(study)
            from pipeline.check.builder_slots import _generate_slots, _upsert_timesteps
            slots     = _generate_slots(event)
            steps     = sorted(fire_state.steps)
            timesteps = _upsert_timesteps(event.id, slots, steps)
            ap_cache  = _load_actual_perimeter_cache(event)
            ros_cache = _load_ros_weights_cache(study)
            print(f"[builder] {event.name}: building weather + perimeter for {len(timesteps)} slots…")
            for ts in timesteps:
                _run_weather_stage(event, ts, study)
                _run_perimeter_stage(event, ts, study, fire_state, ap_cache, ros_cache)
            print(f"[builder] {event.name}: weather + perimeter ready")
        except Exception as e:
            log.error("[builder] build_weather_perimeter event %d failed: %s", event.id, e)


# ── Priority pre-build: 12:00 / 13:00 / 14:00 per day ────────────────────────

_PRIORITY_HOURS = {12, 13, 14}

def build_priority_slots() -> None:
    """Pre-build ML prediction for 12:00, 13:00, 14:00 of every day in each event."""
    from db.models import FireEvent, EventTimestep
    events = FireEvent.query.filter(FireEvent.end_date.isnot(None)).all()
    for event in events:
        try:
            assets    = _get_event_assets(event)
            predictor = _load_predictor()
            threshold = _load_threshold()
        except Exception as e:
            log.error("[builder] priority build event %d setup failed: %s", event.id, e)
            continue

        all_ts = (
            EventTimestep.query
            .filter_by(event_id=event.id)
            .order_by(EventTimestep.slot_time)
            .all()
        )
        priority_ts = [
            ts for ts in all_ts
            if pd.Timestamp(ts.slot_time).hour in _PRIORITY_HOURS
        ]
        if not priority_ts:
            continue

        print(f"[builder] {event.name}: pre-building {len(priority_ts)} priority slot(s) (12h/13h/14h)…")

        from flask import current_app
        _app = current_app._get_current_object()

        def _task(ts):
            with _app.app_context():
                _build_timestep(event, ts, assets, predictor, threshold)
            return ts.id

        pool = ThreadPoolExecutor(max_workers=_N_WORKERS)
        try:
            futures = {pool.submit(_task, ts): ts for ts in priority_ts}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    ts = futures[fut]
                    log.error("[builder] priority ts %d failed: %s", ts.id, e)
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            pool.shutdown(wait=False)

        print(f"[builder] {event.name}: priority slots done")


# ── On-demand: single timestep ────────────────────────────────────────────────

def build_single_timestep_ondemand(app, ts_id: int) -> None:
    """Run all pipeline stages for one timestep (background thread).
    After ML completes, augments hotspots with crowd-submitted fire reports.
    """
    print(f"[ondemand] ts={ts_id} START")
    with app.app_context():
        from db.connection import db
        from db.models import EventTimestep, FireEvent

        ts = db.session.get(EventTimestep, ts_id)
        if not ts:
            print(f"[ondemand] ts={ts_id} not found in DB — abort")
            return

        event = db.session.get(FireEvent, ts.event_id)
        if not event:
            print(f"[ondemand] ts={ts_id} event not found — abort")
            return

        ts_str  = pd.Timestamp(ts.slot_time).strftime("%Y-%m-%dT%H%M")
        print(f"[ondemand] ts={ts_id} slot={ts_str}")
        ml_dir  = _DATA_DIR / "events" / f"{event.year}_{event.id:04d}" / "timesteps" / ts_str / "prediction" / "ML"
        from pipeline.check.builder_slots import _read_status
        st = _read_status(ml_dir)
        if st == "done":
            print(f"[ondemand] ts={ts_id} already done — skip")
            return

        print(f"[ondemand] ts={ts_id} ml_status={st} — loading assets…")
        try:
            assets    = _get_event_assets(event)
            print(f"[ondemand] ts={ts_id} assets ready")
            predictor = _load_predictor()
            print(f"[ondemand] ts={ts_id} predictor ready")
            threshold = _load_threshold()
            print(f"[ondemand] ts={ts_id} threshold={threshold:.4f}")
        except Exception as e:
            print(f"[ondemand] ts={ts_id} setup FAILED: {e}")
            log.error("[builder] ondemand ts %d setup failed: %s", ts_id, e)
            _write_status(ml_dir, "failed")
            return

        print(f"[ondemand] ts={ts_id} entering _build_timestep…")
        _build_timestep(event, ts, assets, predictor, threshold)
        print(f"[ondemand] ts={ts_id} _build_timestep done — augmenting crowd…")
        _augment_with_crowd(event, ts_id)
        print(f"[ondemand] ts={ts_id} COMPLETE")


# ── On-demand: crowd-augmented prediction ────────────────────────────────────

def build_single_timestep_ondemand_crowd(app, ts_id: int) -> None:
    """Build crowd-augmented prediction for one timestep (background thread).

    Flow:
      1. Ensure standard ML/ prediction is done (run it if not).
      2. Get crowd fire_reports within 24h of the slot.
      3. Extend fire_state.boundary_after[t1] with 500m buffers around crowd points.
      4. Re-run prediction with crowd-modified fire_state → prediction/ML_crowd/.
      5. Write hotspot/hotspots_crowd.geojson (VIIRS + crowd points).
      6. Run spatial analysis against ML_crowd/.
    """
    import copy
    import json as _json
    import shutil

    with app.app_context():
        from db.connection import db
        from db.models import EventTimestep, FireEvent, FieldReport
        from pipeline.check.builder_slots import _read_status, _write_status

        ts = db.session.get(EventTimestep, ts_id)
        if not ts:
            return
        event = db.session.get(FireEvent, ts.event_id)
        if not event:
            return

        ts_str   = pd.Timestamp(ts.slot_time).strftime("%Y-%m-%dT%H%M")
        ts_base  = _DATA_DIR / "events" / f"{event.year}_{event.id:04d}" / "timesteps" / ts_str
        ml_dir   = ts_base / "prediction" / "ML"
        crow_dir = ts_base / "prediction" / "ML_crowd"
        sp_crowd = ts_base / "spatial_analysis_crowd"

        print(f"[crowd] ts={ts_id} START")

        # ── Step 1: ensure standard prediction is done ───────────────────────
        if _read_status(ml_dir) != "done":
            print(f"[crowd] ts={ts_id} standard ML not done — running standard first…")
            try:
                assets    = _get_event_assets(event)
                predictor = _load_predictor()
                threshold = _load_threshold()
            except Exception as e:
                print(f"[crowd] ts={ts_id} asset load failed: {e}")
                _write_status(crow_dir, "failed")
                return
            _build_timestep(event, ts, assets, predictor, threshold)

        if _read_status(ml_dir) != "done":
            print(f"[crowd] ts={ts_id} standard ML failed — cannot build crowd prediction")
            _write_status(crow_dir, "failed")
            return

        # ── Step 2: get crowd fire_reports ───────────────────────────────────
        slot_time    = pd.Timestamp(ts.slot_time)
        window_start = (slot_time - pd.Timedelta(hours=24)).to_pydatetime()
        reports = (
            FieldReport.query
            .filter(
                FieldReport.event_id == event.id,
                FieldReport.post_type == "fire_report",
                FieldReport.created_at >= window_start,
            )
            .all()
        )
        print(f"[crowd] ts={ts_id} {len(reports)} crowd fire_reports in 24h window")

        crow_dir.mkdir(parents=True, exist_ok=True)
        _write_status(crow_dir, "running")

        try:
            # ── Step 3: write hotspots_crowd.geojson ────────────────────────
            hs_path       = ts_base / "hotspot" / "hotspots.geojson"
            hs_crowd_path = ts_base / "hotspot" / "hotspots_crowd.geojson"
            base_fc = _json.loads(hs_path.read_text(encoding="utf-8")) if hs_path.exists() \
                      else {"type": "FeatureCollection", "features": []}
            viirs_frps = [f["properties"]["frp"] for f in base_fc.get("features", [])
                          if f.get("properties", {}).get("frp", 0) > 0]
            avg_frp = round(sum(viirs_frps) / len(viirs_frps), 1) if viirs_frps else 30.0
            existing_ids = {f.get("properties", {}).get("report_id") for f in base_fc.get("features", [])}
            for r in reports:
                if r.id not in existing_ids:
                    base_fc["features"].append({
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [r.lon, r.lat]},
                        "properties": {"source": "crowd", "report_id": r.id,
                                       "frp": avg_frp, "confidence": "crowd"},
                    })
            hs_crowd_path.write_text(_json.dumps(base_fc, ensure_ascii=False), encoding="utf-8")

            if not reports:
                # No crowd data — mirror ML/ into ML_crowd/, spatial_analysis_crowd done too
                for f in ml_dir.iterdir():
                    if f.is_file() and f.name != "STATUS.json":
                        shutil.copy2(f, crow_dir / f.name)
                _write_status(crow_dir, "done")
                _write_status(sp_crowd, "done")
                print(f"[crowd] ts={ts_id} no crowd reports — mirrored ML/")
                return

            # ── Step 4: build crowd-modified fire_state ──────────────────────
            import pyproj
            from shapely.geometry import Point as _Point
            from shapely.ops import unary_union, transform as shp_transform

            t1 = pd.Timestamp(ts.nearest_t1)
            if t1.tzinfo is not None:
                t1 = t1.tz_localize(None)

            assets     = _get_event_assets(event)
            fire_state = assets["fire_state"]

            # Find the closest key in boundary_after (same logic as _export_perimeter)
            t1_key = t1
            if t1_key not in fire_state.boundary_after:
                candidates = [k for k, v in fire_state.boundary_after.items() if v is not None]
                if candidates:
                    t1_key = min(candidates,
                                 key=lambda k: abs((pd.Timestamp(k) - t1).total_seconds()))

            proj_fwd = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3978", always_xy=True)
            crowd_geoms = [
                _Point(*proj_fwd.transform(r.lon, r.lat)).buffer(500)
                for r in reports
            ]
            existing_boundary = fire_state.boundary_after.get(t1_key)
            crowd_union = unary_union(
                ([existing_boundary] if existing_boundary and not existing_boundary.is_empty else [])
                + crowd_geoms
            )

            # Shallow-copy fire_state, replace boundary_after dict entry
            fs_crowd = copy.copy(fire_state)
            fs_crowd.boundary_after = dict(fire_state.boundary_after)
            fs_crowd.boundary_after[t1_key] = crowd_union

            # ── Save crowd perimeter GeoJSON ─────────────────────────────────
            proj_back = pyproj.Transformer.from_crs("EPSG:3978", "EPSG:4326", always_xy=True)
            crowd_perim_wgs84 = shp_transform(proj_back.transform, crowd_union)
            perim_crowd_path  = ts_base / "perimeter" / "perimeter_crowd.geojson"
            perim_crowd_path.parent.mkdir(parents=True, exist_ok=True)
            perim_crowd_path.write_text(_json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": crowd_perim_wgs84.__geo_interface__,
                    "properties": {
                        "t1":      t1.isoformat(),
                        "area_km2": round(crowd_union.area / 1e6, 3),
                        "source":  "crowd",
                    },
                }],
            }, ensure_ascii=False), encoding="utf-8")
            print(f"[crowd] ts={ts_id} perimeter_crowd.geojson written")

            # ── Step 5: run prediction with crowd fire_state ─────────────────
            from pipeline.predict import run_prediction
            predictor = _load_predictor()
            threshold = _load_threshold()
            pred_cache = assets["pred_cache"]

            print(f"[crowd] ts={ts_id} running prediction with crowd fire_state…")
            run_prediction(
                ts_id         = ts_id,
                overpass_time = t1,
                study         = assets["study"],
                fire_state    = fs_crowd,
                predictor     = predictor,
                threshold     = threshold,
                out_dir       = crow_dir,
                pred_cache    = pred_cache,
            )
            _write_status(crow_dir, "done")
            print(f"[crowd] ts={ts_id} crowd prediction done")

            # ── Step 6: spatial analysis against ML_crowd/ ───────────────────
            from pipeline.check.builder_stages import _run_spatial_stage_crowd
            _run_spatial_stage_crowd(event, ts, crow_dir, sp_crowd)

        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[crowd] ts={ts_id} FAILED: {e}")
            _write_status(crow_dir, "failed")


# ── Crowd augmentation ────────────────────────────────────────────────────────

def _augment_with_crowd(event, ts_id: int) -> None:
    """Append crowd-submitted fire_reports to the timestep's hotspots.geojson."""
    import json
    from db.connection import db
    from db.models import FieldReport, EventTimestep
    from pipeline.check.builder_slots import _read_status

    ts = EventTimestep.query.get(ts_id)
    if not ts:
        return
    ts_str = pd.Timestamp(ts.slot_time).strftime("%Y-%m-%dT%H%M")
    ml_dir = _DATA_DIR / "events" / f"{event.year}_{event.id:04d}" / "timesteps" / ts_str / "prediction" / "ML"
    if _read_status(ml_dir) != "done":
        return

    # Include all fire_reports for crowd hotspot augmentation
    reports = (
        FieldReport.query
        .filter(
            FieldReport.event_id == event.id,
            FieldReport.post_type == "fire_report",
        )
        .all()
    )
    if not reports:
        return

    ts_str   = pd.Timestamp(ts.slot_time).strftime("%Y-%m-%dT%H%M")
    hs_path  = (
        _DATA_DIR / "events" / f"{event.year}_{event.id:04d}"
        / "timesteps" / ts_str / "hotspot" / "hotspots.geojson"
    )
    if not hs_path.exists():
        return

    try:
        fc = json.loads(hs_path.read_text(encoding="utf-8"))
        viirs_frps = [f["properties"]["frp"] for f in fc.get("features", [])
                      if f.get("properties", {}).get("frp", 0) > 0]
        avg_frp = round(sum(viirs_frps) / len(viirs_frps), 1) if viirs_frps else 30.0
        existing_ids = {f.get("properties", {}).get("report_id") for f in fc.get("features", [])}
        added = 0
        for r in reports:
            if r.id in existing_ids:
                continue
            fc["features"].append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r.lon, r.lat]},
                "properties": {
                    "source":    "crowd",
                    "report_id": r.id,
                    "frp":       avg_frp,
                },
            })
            added += 1
        if added:
            hs_path.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
            log.info("[crowd] ts %d: added %d crowd hotspot(s)", ts_id, added)
    except Exception as e:
        log.warning("[crowd] augment failed for ts %d: %s", ts_id, e)
