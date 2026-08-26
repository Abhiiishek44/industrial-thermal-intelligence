"""
pipeline/check/
---------------
Startup checks: verify required files exist. Does NOT download anything.
Run `python prepare.py` first to download models, ERA5, and static GeoPackages.
"""

from __future__ import annotations

from pathlib import Path

_DATA_DIR   = Path(__file__).resolve().parents[3] / "data"
_MODELS_DIR = _DATA_DIR / "static" / "models"

_STATIC_FILES = {
    "population.gpkg":   "https://zenodo.org/records/19434352/files/population.gpkg?download=1",
    "roads_canada.gpkg": "https://zenodo.org/records/19436338/files/roads_canada.gpkg?download=1",
}

_REQUIRED_MODELS = [
    "model_full_rf.pkl",
    "model_full_xgb.pkl",
    "model_full_lr.pkl",
    "model_full_thresholds.json",
]

from pipeline.check.builder import build_playback_events


def run_checks(app) -> None:
    """Verify environment at startup — warns only, does not download."""
    print("=" * 50)
    print("  Wildfire Decision Support — Environment Check")
    print("=" * 50)
    _check_models()
    _check_static_gpkg()
    with app.app_context():
        _check_events()
    print("=" * 50)


def _check_models() -> None:
    missing = [f for f in _REQUIRED_MODELS if not (_MODELS_DIR / f).exists()]
    if missing:
        print(f"[checks] WARN: ML models missing ({', '.join(missing)}) — run prepare.py")
    else:
        print("[checks] ML models — OK")


def _check_static_gpkg() -> None:
    static_dir = _DATA_DIR / "static"
    missing = [f for f in _STATIC_FILES if not (static_dir / f).exists()]
    if missing:
        print(f"[checks] WARN: static files missing ({', '.join(missing)}) — run prepare.py")
    else:
        print("[checks] static GeoPackages — OK")


def _check_events() -> None:
    from db.models import FireEvent
    events = FireEvent.query.all()
    if not events:
        print("[checks] WARN: no FireEvents in DB")
        return
    for event in events:
        event_data_dir = _DATA_DIR / "events" / f"{event.year}_{event.id:04d}"
        era5 = event_data_dir / "data_processed" / "weather" / "era5.parquet"
        fire_state = event_data_dir / "data_processed" / "training" / "fire_state.pkl"
        missing = [p for p in (era5, fire_state) if not p.exists()]
        if missing:
            print(f"[checks] WARN: {event.name} — missing {[p.name for p in missing]} — run prepare.py")
        else:
            print(f"[checks] {event.name} — OK")

