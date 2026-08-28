"""Scheduled near-real-time FIRMS refresh for thermal-monitoring events."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL_HOURS = 4.0
DEFAULT_LIVE_LOOKBACK_DAYS = 2

_refresh_lock = threading.Lock()
_scheduler_lock = threading.Lock()
_scheduler_started = False


def _enabled(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def get_refresh_settings() -> dict:
    interval = float(os.getenv("THERMAL_REFRESH_INTERVAL_HOURS", DEFAULT_REFRESH_INTERVAL_HOURS))
    lookback = int(os.getenv("THERMAL_LIVE_LOOKBACK_DAYS", DEFAULT_LIVE_LOOKBACK_DAYS))
    if interval <= 0:
        raise ValueError("THERMAL_REFRESH_INTERVAL_HOURS must be greater than zero")
    if lookback < 1 or lookback > 5:
        raise ValueError("THERMAL_LIVE_LOOKBACK_DAYS must be between 1 and 5")
    return {
        "enabled": _enabled(os.getenv("THERMAL_AUTO_REFRESH"), default=True),
        "interval_hours": interval,
        "lookback_days": lookback,
    }


def _event_thermal_dir(event) -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "data"
        / "events"
        / f"{event.year}_{event.id:04d}"
        / "data_processed"
        / "thermal"
    )


def _status_path(event) -> Path:
    return _event_thermal_dir(event) / "refresh_metadata.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_refresh_status(event) -> dict:
    path = _status_path(event)
    if not path.exists():
        return {
            "event_id": event.id,
            "status": "never",
            "last_attempt_at": None,
            "last_success_at": None,
            "last_observed_at": None,
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"event_id": event.id, "status": "unknown"}


def _write_status(event, **updates) -> dict:
    payload = load_refresh_status(event)
    payload.update({"event_id": event.id, **updates})
    _atomic_write_json(_status_path(event), payload)
    return payload


def refresh_thermal_event(event, *, now: datetime | None = None) -> dict:
    """Fetch, normalize, enrich and reclassify one configured region."""
    from db.connection import db
    from pipeline.env import _create_event_timesteps, _make_study
    from pipeline.thermal import (
        collect_latest_firms,
        ensure_persistence_analysis,
        ensure_source_classification,
        ensure_thermal_context,
        load_history_metadata,
        normalize_firms_history,
    )

    settings = get_refresh_settings()
    attempted_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    previous = load_history_metadata(event) or {}
    previous_count = int(previous.get("observation_count") or 0)
    previous_last = previous.get("last_observed_at")
    _write_status(
        event,
        status="running",
        last_attempt_at=attempted_at.isoformat(),
        interval_hours=settings["interval_hours"],
        lookback_days=settings["lookback_days"],
        error=None,
    )

    try:
        study = _make_study(event)
        collection = collect_latest_firms(
            event,
            study,
            day_range=settings["lookback_days"],
        )
        if collection.get("successful_source_count", 0) == 0:
            messages = [item.get("error", "unknown error") for item in collection.get("errors", [])]
            raise RuntimeError("all FIRMS sources failed: " + "; ".join(messages))

        history = normalize_firms_history(event, study)
        if history.get("data_available"):
            ensure_thermal_context(event, study)
            ensure_persistence_analysis(event, study)
            ensure_source_classification(event, study)

        current_count = int(history.get("observation_count") or 0)
        current_last = history.get("last_observed_at")
        data_changed = current_count != previous_count or current_last != previous_last

        if current_last:
            observed_date = pd.Timestamp(current_last).date()
            if event.end_date is None or observed_date > event.end_date:
                event.end_date = observed_date
                db.session.commit()

        if data_changed and history.get("data_available"):
            _create_event_timesteps(event)

        completed_at = datetime.now(timezone.utc)
        status = _write_status(
            event,
            status="succeeded",
            last_success_at=completed_at.isoformat(),
            next_refresh_at=(completed_at + timedelta(hours=settings["interval_hours"])).isoformat(),
            last_observed_at=current_last,
            observation_count=current_count,
            new_observation_count=max(0, current_count - previous_count),
            data_changed=data_changed,
            fetched_record_count=int(collection.get("record_count") or 0),
            source_errors=collection.get("errors", []),
            error=None,
        )
        log.info(
            "[thermal-live] event %d refresh complete: %d total, %d new",
            event.id,
            current_count,
            status["new_observation_count"],
        )
        return status
    except Exception as exc:
        db.session.rollback()
        log.exception("[thermal-live] event %d refresh failed: %s", event.id, exc)
        _write_status(
            event,
            status="failed",
            error=str(exc),
            next_refresh_at=(attempted_at + timedelta(hours=settings["interval_hours"])).isoformat(),
        )
        raise


def refresh_all_thermal_events(app) -> list[dict]:
    """Refresh configured public India regions, skipping overlapping cycles."""
    if not _refresh_lock.acquire(blocking=False):
        log.warning("[thermal-live] refresh cycle skipped because another cycle is running")
        return []
    try:
        with app.app_context():
            from db.models import FireEvent
            from pipeline.event_config import should_prepare_event

            results = []
            events = [event for event in FireEvent.query.order_by(FireEvent.id).all() if should_prepare_event(event)]
            for event in events:
                try:
                    results.append(refresh_thermal_event(event))
                except Exception:
                    # One unavailable region or sensor must not block the rest.
                    continue
            return results
    finally:
        _refresh_lock.release()


def start_thermal_refresh_scheduler(app):
    """Start one daemon scheduler per Python process; returns its thread."""
    global _scheduler_started
    settings = get_refresh_settings()
    if not settings["enabled"]:
        log.info("[thermal-live] automatic refresh disabled")
        return None
    if not os.getenv("FIRMS_API_KEY", "").strip():
        log.warning("[thermal-live] scheduler disabled because FIRMS_API_KEY is missing")
        return None

    with _scheduler_lock:
        if _scheduler_started:
            return None
        _scheduler_started = True

    def _run() -> None:
        interval_seconds = settings["interval_hours"] * 3600
        while True:
            refresh_all_thermal_events(app)
            threading.Event().wait(interval_seconds)

    thread = threading.Thread(target=_run, name="thermal-firms-refresh", daemon=True)
    thread.start()
    log.info(
        "[thermal-live] scheduler started (every %.2f hours, %d-day lookback)",
        settings["interval_hours"],
        settings["lookback_days"],
    )
    return thread
