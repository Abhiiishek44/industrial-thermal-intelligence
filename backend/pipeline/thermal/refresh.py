"""Scheduled near-real-time FIRMS refresh for thermal-monitoring events."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL_MINUTES = 15
DEFAULT_LIVE_LOOKBACK_DAYS = 2
DEFAULT_FAILURE_RETRY_MINUTES = 15

_refresh_lock = threading.Lock()
_scheduler_lock = threading.Lock()
_scheduler_started = False
_ADVISORY_LOCK_KEY = 914_202_615


def _enabled(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _acquire_distributed_lock(db):
    """Hold a PostgreSQL advisory lock on one dedicated physical connection."""
    if db.engine.dialect.name != "postgresql":
        return None, True
    connection = db.engine.raw_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        acquired = bool(cursor.fetchone()[0])
    finally:
        cursor.close()
    if not acquired:
        connection.close()
        return None, False
    return connection, True


def _release_distributed_lock(connection) -> None:
    if connection is None:
        return
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        connection.commit()
    finally:
        cursor.close()
        connection.close()


def get_refresh_settings() -> dict:
    legacy_interval = os.getenv("THERMAL_REFRESH_INTERVAL_HOURS")
    interval_minutes = int(os.getenv(
        "FIRMS_POLL_INTERVAL_MINUTES",
        str(round(float(legacy_interval) * 60) if legacy_interval else DEFAULT_REFRESH_INTERVAL_MINUTES),
    ))
    lookback_hours = int(os.getenv("FIRMS_LOOKBACK_HOURS", str(DEFAULT_LIVE_LOOKBACK_DAYS * 24)))
    lookback = max(1, min(5, math.ceil(lookback_hours / 24)))
    failure_retry = int(os.getenv("THERMAL_FAILURE_RETRY_MINUTES", DEFAULT_FAILURE_RETRY_MINUTES))
    sources = tuple(
        item.strip() for item in os.getenv(
            "FIRMS_SOURCES", "VIIRS_NOAA20_NRT,VIIRS_NOAA21_NRT"
        ).split(",") if item.strip()
    )
    if interval_minutes <= 0:
        raise ValueError("FIRMS_POLL_INTERVAL_MINUTES must be greater than zero")
    if lookback < 1 or lookback > 5:
        raise ValueError("THERMAL_LIVE_LOOKBACK_DAYS must be between 1 and 5")
    if failure_retry < 1 or failure_retry > 60:
        raise ValueError("THERMAL_FAILURE_RETRY_MINUTES must be between 1 and 60")
    return {
        "enabled": _enabled(os.getenv("THERMAL_AUTO_REFRESH"), default=True),
        "interval_minutes": interval_minutes,
        "interval_hours": interval_minutes / 60.0,
        "lookback_days": lookback,
        "failure_retry_minutes": failure_retry,
        "sources": sources,
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
    cycle_started = time.monotonic()
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
            sources=settings["sources"],
        )
        if collection.get("successful_source_count", 0) == 0:
            messages = [item.get("error", "unknown error") for item in collection.get("errors", [])]
            raise RuntimeError("all FIRMS sources failed: " + "; ".join(messages))

        history = normalize_firms_history(event, study)
        current_count = int(history.get("observation_count") or 0)
        current_last = history.get("last_observed_at")
        data_changed = current_count != previous_count or current_last != previous_last

        # A repeated NRT response is normalized for safety but must not trigger
        # WorldCover/context work or model scoring again.
        derived_missing = not (
            _event_thermal_dir(event) / "detections_aggregated.parquet"
        ).exists()
        if (data_changed or derived_missing) and history.get("data_available"):
            ensure_thermal_context(event, study)
            ensure_persistence_analysis(event, study)
            ensure_source_classification(event, study)

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
        from services.firms_ingestion_service import firms_ingestion
        ingestion_stats = firms_ingestion.sync_event_artifacts(event, now=completed_at)
        status["ingestion"] = ingestion_stats.as_dict()
        _atomic_write_json(_status_path(event), status)
        log.info(
            "[thermal-live] event %d refresh complete: %d total, %d new, %d persisted",
            event.id,
            current_count,
            status["new_observation_count"],
            ingestion_stats.new_records,
        )
        log.info(
            "FIRMS ingestion completed event_id=%d records_received=%d duplicates=%d "
            "new_records=%d predictions_completed=%d prediction_failures=%d "
            "events_created=%d events_updated=%d alerts_created=%d duration_ms=%d",
            event.id,
            ingestion_stats.records_received,
            ingestion_stats.duplicates,
            ingestion_stats.new_records,
            ingestion_stats.predictions_completed,
            ingestion_stats.prediction_failures,
            ingestion_stats.events_created,
            ingestion_stats.events_updated,
            ingestion_stats.alerts_created,
            int((time.monotonic() - cycle_started) * 1000),
        )
        return status
    except Exception as exc:
        db.session.rollback()
        log.exception("[thermal-live] event %d refresh failed: %s", event.id, exc)
        _write_status(
            event,
            status="failed",
            error=str(exc),
            next_refresh_at=(attempted_at + timedelta(minutes=settings["failure_retry_minutes"])).isoformat(),
        )
        raise


def refresh_all_thermal_events(app) -> list[dict]:
    """Refresh configured public India regions, skipping overlapping cycles."""
    if not _refresh_lock.acquire(blocking=False):
        log.warning("[thermal-live] refresh cycle skipped because another cycle is running")
        return []
    try:
        with app.app_context():
            from db.connection import db
            from db.models import FireEvent
            from pipeline.event_config import should_prepare_event
            from services.firms_ingestion_service import IngestionStats, record_sync_status

            lock_connection, acquired = _acquire_distributed_lock(db)
            if not acquired:
                log.warning("[thermal-live] refresh skipped; distributed lock is held")
                return []

            results = []
            combined = IngestionStats()
            try:
                events = [event for event in FireEvent.query.order_by(FireEvent.id).all() if should_prepare_event(event)]
                for event in events:
                    try:
                        result = refresh_thermal_event(event)
                        results.append(result)
                        for key, value in result.get("ingestion", {}).items():
                            setattr(combined, key, getattr(combined, key) + int(value))
                    except Exception as exc:
                        # One unavailable region or sensor must not block the rest.
                        results.append({"event_id": event.id, "status": "failed", "error": str(exc)})
                        continue
                failures = [result for result in results if result.get("status") != "succeeded"]
                record_sync_status(
                    combined,
                    error="; ".join(str(item.get("error")) for item in failures) if failures and len(failures) == len(results) else None,
                )
                return results
            finally:
                _release_distributed_lock(lock_connection)
    finally:
        _refresh_lock.release()


def bootstrap_cached_firms(app) -> list[dict]:
    """Populate durable monitoring tables from existing enriched artifacts.

    This runs before the first network polling cycle, so a deployment with
    recent cached FIRMS data can render the rolling window immediately. It does
    not fetch data, retrain the model, or rebuild WorldCover/context artifacts.
    """
    with app.app_context():
        from db.connection import db
        from db.models import FireEvent, FirmsSyncState
        from pipeline.event_config import should_prepare_event
        from services.firms_ingestion_service import IngestionStats, firms_ingestion

        state = db.session.get(FirmsSyncState, 1) or FirmsSyncState(id=1)
        state.status = "bootstrapping"
        state.last_attempt_at = datetime.now(timezone.utc)
        state.error = None
        db.session.add(state)
        db.session.commit()

        lock_connection, acquired = _acquire_distributed_lock(db)
        if not acquired:
            log.info("[thermal-live] cached FIRMS bootstrap skipped; lock is held")
            return []

        combined = IngestionStats()
        results = []
        try:
            events = [
                event for event in FireEvent.query.order_by(FireEvent.id).all()
                if should_prepare_event(event)
            ]
            for event in events:
                try:
                    stats = firms_ingestion.sync_event_artifacts(event)
                    results.append({"event_id": event.id, **stats.as_dict()})
                    for key, value in stats.as_dict().items():
                        setattr(combined, key, getattr(combined, key) + int(value))
                except Exception as exc:
                    log.exception(
                        "[thermal-live] cached FIRMS bootstrap failed event_id=%d",
                        event.id,
                    )
                    results.append({
                        "event_id": event.id, "status": "failed", "error": str(exc),
                    })

            state = db.session.get(FirmsSyncState, 1) or FirmsSyncState(id=1)
            state.status = "ready"
            state.records_received = combined.records_received
            state.new_records = combined.new_records
            state.duplicates = combined.duplicates
            state.prediction_failures = combined.prediction_failures
            failures = [item for item in results if item.get("status") == "failed"]
            state.error = (
                f"{len(failures)} cached region(s) could not be imported"
                if failures else None
            )
            db.session.add(state)
            db.session.commit()
            log.info(
                "FIRMS cached bootstrap completed records_received=%d new_records=%d "
                "duplicates=%d events_created=%d events_updated=%d",
                combined.records_received,
                combined.new_records,
                combined.duplicates,
                combined.events_created,
                combined.events_updated,
            )
            return results
        finally:
            _release_distributed_lock(lock_connection)


def start_thermal_refresh_scheduler(app):
    """Start one daemon scheduler per Python process; returns its thread."""
    global _scheduler_started
    settings = get_refresh_settings()
    if not settings["enabled"]:
        log.info("[thermal-live] automatic refresh disabled")
        return None
    if not (os.getenv("FIRMS_API_KEY") or os.getenv("NASA_FIRMS_MAP_KEY") or "").strip():
        log.warning("[thermal-live] scheduler disabled because FIRMS_API_KEY is missing")
        return None

    with _scheduler_lock:
        if _scheduler_started:
            return None
        _scheduler_started = True

    def _run() -> None:
        while True:
            results = refresh_all_thermal_events(app)
            retrying = any(result.get("status") != "succeeded" for result in results)
            delay_seconds = (
                settings["failure_retry_minutes"] * 60
                if retrying else settings["interval_minutes"] * 60
            )
            threading.Event().wait(delay_seconds)

    thread = threading.Thread(target=_run, name="thermal-firms-refresh", daemon=True)
    thread.start()
    log.info(
        "[thermal-live] scheduler started (every %d minutes, %d-day lookback)",
        settings["interval_minutes"],
        settings["lookback_days"],
    )
    return thread
