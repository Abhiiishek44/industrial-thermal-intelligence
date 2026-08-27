"""
pipeline/check/builder_slots.py
--------------------------------
Slot generation, timestep DB helpers.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd

log = logging.getLogger(__name__)

_GAP_WARN_H = 12.0


def _as_utc(value) -> pd.Timestamp:
    """Return a timezone-aware UTC timestamp for DB-safe comparisons."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _generate_slots(event) -> list[pd.Timestamp]:
    """Generate hourly UTC slots from event.start_date through event.end_date."""
    start = _as_utc(event.start_date).floor("3h")
    end   = _as_utc(event.end_date) + pd.Timedelta(hours=23, minutes=59)
    slots, t = [], start
    while t <= end:
        slots.append(t)
        t += timedelta(hours=1)
    return slots


def _nearest_past_t1(slot: pd.Timestamp, steps: list[pd.Timestamp]):
    """Return the most recent overpass at or before slot, or None."""
    slot = _as_utc(slot)
    normalized_steps = [_as_utc(step) for step in steps]
    past = [step for step in normalized_steps if step <= slot]
    return max(past) if past else None


def _upsert_timesteps(
    event_id: int, slots: list, steps: list, *, prune_missing: bool = False,
) -> list:
    from db.models import EventTimestep
    from db.connection import db
    from sqlalchemy.dialects.postgresql import insert

    values = []
    for slot in slots:
        slot = _as_utc(slot)
        t1 = _nearest_past_t1(slot, steps)
        if t1 is None:
            continue

        gap_h = (slot - t1).total_seconds() / 3600.0
        if gap_h > _GAP_WARN_H:
            log.debug("[builder] slot %s: gap=%.1fh (data_gap_warn=True)", slot, gap_h)

        values.append({
            "event_id":      event_id,
            "slot_time":     slot.to_pydatetime(),
            "nearest_t1":    t1.to_pydatetime(),
            "gap_hours":     round(gap_h, 2),
            "data_gap_warn": gap_h > _GAP_WARN_H,
        })

    if not values:
        log.warning(
            "[builder] event %d timestep generation produced no rows: "
            "no fire overpass exists at or before any slot",
            event_id,
        )
        return []

    statement = insert(EventTimestep).values(values)
    statement = statement.on_conflict_do_update(
        index_elements=["event_id", "slot_time"],
        set_={
            "nearest_t1": statement.excluded.nearest_t1,
            "gap_hours": statement.excluded.gap_hours,
            "data_gap_warn": statement.excluded.data_gap_warn,
        },
    )

    removed_count = 0
    desired_slots = [value["slot_time"] for value in values]
    if prune_missing:
        removed_count = (
            db.session.query(EventTimestep)
            .filter(
                EventTimestep.event_id == event_id,
                EventTimestep.slot_time.notin_(desired_slots),
            )
            .delete(synchronize_session=False)
        )

    existing_slots = {
        _as_utc(value)
        for value, in db.session.query(EventTimestep.slot_time)
        .filter_by(event_id=event_id)
        .all()
    }
    created_count = sum(
        1 for value in values if _as_utc(value["slot_time"]) not in existing_slots
    )

    try:
        db.session.execute(statement)
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("[builder] event %d timestep persistence failed", event_id)
        raise

    result = (
        EventTimestep.query
        .filter_by(event_id=event_id)
        .order_by(EventTimestep.slot_time)
        .all()
    )
    log.info(
        "[builder] event %d timestep generation complete: %d created, %d removed, %d total",
        event_id,
        created_count,
        removed_count,
        len(result),
    )
    return result


import threading

# In-memory tracking of currently-running stages.
# Key: absolute string path of the status directory (e.g. ".../prediction/ML")
# "running" is never written to disk — it lives here only, so it vanishes on
# restart and never leaves zombie statuses behind.
_running: set[str] = set()
_running_lock = threading.Lock()

def _mark_running(status_dir) -> None:
    with _running_lock:
        _running.add(str(status_dir))


def _try_mark_running(status_dir) -> bool:
    """Atomically claim a non-completed stage for one background worker."""
    import json
    from pathlib import Path

    key = str(status_dir)
    with _running_lock:
        if key in _running:
            return False

        path = Path(status_dir) / "STATUS.json"
        if path.exists():
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("status") == "done":
                    return False
            except Exception:
                pass

        _running.add(key)
        return True


def _write_status(status_dir, status: str) -> None:
    """Persist a terminal status (done/failed) to STATUS.json.
    'running' is intentionally NOT written to disk — use _mark_running() instead.
    """
    import json
    from pathlib import Path
    if status == "running":
        _mark_running(status_dir)
        return
    status_dir = Path(status_dir)
    status_dir.mkdir(parents=True, exist_ok=True)
    # Keep the claim lock through the durable terminal write so another request
    # cannot observe a gap between "running" and "done".
    with _running_lock:
        (status_dir / "STATUS.json").write_text(
            json.dumps({"status": status}, indent=2), encoding="utf-8"
        )
        _running.discard(str(status_dir))


def _read_status(status_dir) -> str:
    """Return current status, consulting in-memory running set first."""
    import json
    from pathlib import Path
    with _running_lock:
        if str(status_dir) in _running:
            return "running"
    path = Path(status_dir) / "STATUS.json"
    if not path.exists():
        return "pending"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("status", "pending")
    except Exception:
        return "pending"
