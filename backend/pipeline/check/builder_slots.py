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


def _generate_slots(event) -> list[pd.Timestamp]:
    """Generate hourly slots from event.start_date to event.end_date (tz-naive)."""
    start = pd.Timestamp(event.start_date).floor("3h")
    end   = pd.Timestamp(event.end_date) + pd.Timedelta(hours=23, minutes=59)
    slots, t = [], start
    while t <= end:
        slots.append(t)
        t += timedelta(hours=1)
    return slots


def _nearest_past_t1(slot: pd.Timestamp, steps: list[pd.Timestamp]):
    """Return the most recent overpass at or before slot, or None."""
    past = [s for s in steps if s <= slot]
    return max(past) if past else None


def _upsert_timesteps(event_id: int, slots: list, steps: list) -> list:
    from db.models import EventTimestep
    from db.connection import db

    result = []
    for slot in slots:
        t1 = _nearest_past_t1(slot, steps)
        if t1 is None:
            continue

        gap_h = (slot - t1).total_seconds() / 3600.0
        if gap_h > _GAP_WARN_H:
            log.debug("[builder] slot %s: gap=%.1fh (data_gap_warn=True)", slot, gap_h)

        ts = EventTimestep.query.filter_by(event_id=event_id, slot_time=slot).first()
        if ts is None:
            ts = EventTimestep(
                event_id      = event_id,
                slot_time     = slot,
                nearest_t1    = t1,
                gap_hours     = round(gap_h, 2),
                data_gap_warn = gap_h > _GAP_WARN_H,
            )
            db.session.add(ts)
            db.session.flush()
        result.append(ts)

    db.session.commit()
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


def _mark_done(status_dir) -> None:
    with _running_lock:
        _running.discard(str(status_dir))


def _write_status(status_dir, status: str) -> None:
    """Persist a terminal status (done/failed) to STATUS.json.
    'running' is intentionally NOT written to disk — use _mark_running() instead.
    """
    import json
    from pathlib import Path
    if status == "running":
        _mark_running(status_dir)
        return
    _mark_done(status_dir)
    status_dir = Path(status_dir)
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / "STATUS.json").write_text(
        json.dumps({"status": status}, indent=2), encoding="utf-8"
    )


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
