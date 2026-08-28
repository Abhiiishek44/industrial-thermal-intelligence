"""Event-scoped historical NASA FIRMS collection and normalization.

Raw API responses are cached in non-overlapping, profile-sized date chunks. The
normalizer keeps the union of original FIRMS columns, adds a canonical UTC
``observed_at`` value, and removes repeat observations deterministically.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

from pipeline.event_config import THERMAL_MONITORING_MODE, get_event_config

log = logging.getLogger(__name__)

FIRMS_AREA_CSV_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
LIVE_FIRMS_SOURCES = ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT")
_IDENTITY_COLUMNS = (
    "latitude",
    "longitude",
    "acq_date",
    "acq_time",
    "satellite",
    "instrument",
)


@dataclass(frozen=True)
class ThermalHistoryPaths:
    raw_dir: Path
    processed_dir: Path
    history_path: Path
    current_path: Path
    metadata_path: Path

    @classmethod
    def from_study(cls, study) -> "ThermalHistoryPaths":
        project_dir = Path(study.project_dir)
        processed_dir = project_dir / "data_processed" / "thermal"
        return cls(
            raw_dir=project_dir / "data_raw" / "firms" / "history",
            processed_dir=processed_dir,
            history_path=processed_dir / "firms_history.parquet",
            current_path=processed_dir / "firms_current.parquet",
            metadata_path=processed_dir / "history_metadata.json",
        )


def _date_chunks(start: date, end: date, chunk_days: int) -> Iterable[tuple[date, int]]:
    if chunk_days < 1 or chunk_days > 5:
        raise ValueError("NASA FIRMS chunk size must be between 1 and 5 days")
    if end < start:
        raise ValueError("thermal history end date precedes start date")

    cursor = start
    while cursor <= end:
        days = min(chunk_days, (end - cursor).days + 1)
        yield cursor, days
        cursor += timedelta(days=days)


def _chunk_name(source: str, start: date, days: int) -> str:
    end = start + timedelta(days=days - 1)
    return f"{source}_{start.isoformat()}_{end.isoformat()}"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _source_product_from_filename(path: Path) -> str:
    """Recover the FIRMS product name from historical and live filenames."""
    stem = path.stem.removeprefix("live_")
    return re.sub(r"_\d{4}-\d{2}-\d{2}(?:_\d{4}-\d{2}-\d{2})?$", "", stem)


def collect_latest_firms(
    event,
    study,
    *,
    day_range: int = 2,
    sources: tuple[str, ...] = LIVE_FIRMS_SOURCES,
    session=requests,
) -> dict:
    """Fetch latest region-scoped NRT observations and archive them by day.

    Omitting the Area API date requests the newest available data. Daily files
    allow an incomplete current day to be safely replaced on the next refresh
    without discarding observations from earlier days.
    """
    config = get_event_config(event)
    if config.analysis_mode != THERMAL_MONITORING_MODE:
        return {"data_available": False, "files": [], "record_count": 0, "errors": []}
    if day_range < 1 or day_range > 5:
        raise ValueError("NASA FIRMS live day range must be between 1 and 5 days")

    api_key = os.getenv("FIRMS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("FIRMS_API_KEY is not configured")

    paths = ThermalHistoryPaths.from_study(study)
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    area = ",".join(f"{coordinate:.10g}" for coordinate in config.bbox)
    written: list[Path] = []
    errors: list[dict] = []
    record_count = 0
    successful_sources = 0

    for source in sources:
        url = f"{FIRMS_AREA_CSV_URL}/{api_key}/{source}/{area}/{day_range}"
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
        except requests.RequestException as exc:
            log.warning("[thermal-live] event %d source %s failed: %s", event.id, source, exc)
            errors.append({"source": source, "error": str(exc)})
            continue

        body = response.text.lstrip("\ufeff").strip()
        first_line = body.splitlines()[0].lower() if body else ""
        if "latitude" not in first_line or "longitude" not in first_line:
            if body and "no data" not in body.lower():
                errors.append({"source": source, "error": "unexpected FIRMS response"})
            else:
                successful_sources += 1
            continue

        successful_sources += 1

        frame = pd.read_csv(
            io.StringIO(body),
            dtype={"acq_date": "string", "acq_time": "string"},
        )
        frame.columns = [str(column).strip().lower() for column in frame.columns]
        if "acq_date" not in frame.columns:
            errors.append({"source": source, "error": "acq_date missing from FIRMS response"})
            continue

        frame["acq_date"] = frame["acq_date"].astype("string").str.strip()
        for acquisition_date, daily in frame.groupby("acq_date", sort=True):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(acquisition_date)):
                continue
            output = paths.raw_dir / f"live_{source}_{acquisition_date}.csv"
            temporary = output.with_suffix(output.suffix + ".tmp")
            daily.to_csv(temporary, index=False)
            temporary.replace(output)
            written.append(output)
            record_count += len(daily)

    return {
        "data_available": bool(written),
        "files": [str(path) for path in sorted(written)],
        "record_count": int(record_count),
        "successful_source_count": successful_sources,
        "requested_source_count": len(sources),
        "errors": errors,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def collect_firms_history(event, study, *, session=requests) -> list[Path]:
    """Download missing historical FIRMS chunks and return cached CSV paths.

    Empty successful responses receive an explicit marker, so reruns remain
    idempotent without inventing observations. The API key is never written to
    logs or metadata.
    """
    config = get_event_config(event)
    history_dates = config.thermal_history_dates
    if config.analysis_mode != THERMAL_MONITORING_MODE or history_dates is None:
        return []

    api_key = os.getenv("FIRMS_API_KEY", "").strip()
    if not api_key:
        log.info("[thermal-history] event %d collection skipped: FIRMS_API_KEY missing", event.id)
        return []

    paths = ThermalHistoryPaths.from_study(study)
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    area = ",".join(f"{coordinate:.10g}" for coordinate in config.bbox)
    collected: list[Path] = []

    for source in config.firms_history_sources:
        for chunk_start, days in _date_chunks(
            history_dates[0], history_dates[1], config.firms_chunk_days,
        ):
            stem = _chunk_name(source, chunk_start, days)
            csv_path = paths.raw_dir / f"{stem}.csv"
            empty_path = paths.raw_dir / f"{stem}.empty.json"
            if csv_path.exists():
                collected.append(csv_path)
                continue
            if empty_path.exists():
                continue

            url = (
                f"{FIRMS_AREA_CSV_URL}/{api_key}/{source}/{area}/{days}/"
                f"{chunk_start.isoformat()}"
            )
            try:
                response = session.get(url, timeout=60)
                response.raise_for_status()
            except requests.RequestException as exc:
                # Sensors have independent availability windows. Preserve and
                # normalize successful products even when one source is
                # temporarily unavailable or unsupported for a requested date.
                log.warning(
                    "[thermal-history] event %d skipped %s %s: %s",
                    event.id,
                    source,
                    chunk_start,
                    exc,
                )
                continue
            body = response.text.lstrip("\ufeff").strip()

            first_line = body.splitlines()[0].lower() if body else ""
            if "latitude" not in first_line or "longitude" not in first_line:
                if body and "no data" not in body.lower():
                    preview = " ".join(body.split())[:160]
                    raise RuntimeError(
                        f"unexpected FIRMS response for {source} on {chunk_start}: {preview}"
                    )
                marker = {
                    "source": source,
                    "start_date": chunk_start.isoformat(),
                    "day_range": days,
                    "empty": True,
                }
                _atomic_write_text(empty_path, json.dumps(marker, indent=2))
                continue

            _atomic_write_text(csv_path, body + "\n")
            collected.append(csv_path)
            log.info(
                "[thermal-history] event %d cached %s %s (%d day(s))",
                event.id,
                source,
                chunk_start,
                days,
            )

    return sorted(collected)


def _read_raw_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(
        path,
        dtype={"acq_date": "string", "acq_time": "string"},
    )
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    frame["source_file"] = path.name
    if path.parent.name == "history":
        frame["source_product"] = _source_product_from_filename(path)
    else:
        frame["source_product"] = "existing_event_feed"
    return frame


def normalize_firms_frames(frames: Iterable[pd.DataFrame]) -> tuple[pd.DataFrame, int]:
    """Normalize and deterministically deduplicate raw FIRMS frames."""
    materialized = [frame.copy() for frame in frames if not frame.empty]
    if not materialized:
        return pd.DataFrame(), 0

    history = pd.concat(materialized, ignore_index=True, sort=False)
    history.columns = [str(column).strip().lower() for column in history.columns]
    missing = [column for column in _IDENTITY_COLUMNS if column not in history.columns]
    if missing:
        raise ValueError(f"FIRMS data missing identity columns: {', '.join(missing)}")

    history["acq_date"] = history["acq_date"].astype("string").str.strip()
    history["acq_time"] = (
        history["acq_time"].astype("string").str.replace(r"\.0$", "", regex=True).str.zfill(4)
    )
    history["observed_at"] = pd.to_datetime(
        history["acq_date"] + history["acq_time"],
        format="%Y-%m-%d%H%M",
        errors="coerce",
        utc=True,
    )
    history = history[history["observed_at"].notna()].copy()

    for column in ("latitude", "longitude"):
        history[column] = pd.to_numeric(history[column], errors="coerce")
    history = history[
        history["latitude"].notna() & history["longitude"].notna()
    ].copy()

    before = len(history)
    sort_columns = ["observed_at", "latitude", "longitude", "satellite", "instrument"]
    history = history.sort_values(sort_columns, kind="mergesort")
    history = history.drop_duplicates(subset=list(_IDENTITY_COLUMNS), keep="first")
    history = history.reset_index(drop=True)
    return history, before - len(history)


def _raw_history_paths(study, paths: ThermalHistoryPaths) -> list[Path]:
    candidates = list(paths.raw_dir.glob("*.csv"))
    existing_feed = Path(study.project_dir) / "data_raw" / "firms" / "hotspots_raw.csv"
    if existing_feed.exists():
        candidates.append(existing_feed)
    return sorted(set(candidates))


def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(temporary, index=False)
    temporary.replace(path)


def normalize_firms_history(event, study) -> dict:
    """Build normalized history/current Parquet files and summary metadata."""
    config = get_event_config(event)
    paths = ThermalHistoryPaths.from_study(study)
    raw_paths = _raw_history_paths(study, paths)
    frames = [_read_raw_csv(path) for path in raw_paths]
    history, duplicates_removed = normalize_firms_frames(frames)

    if history.empty:
        metadata = {
            "event_id": event.id,
            "data_available": False,
            "observation_count": 0,
            "first_observed_at": None,
            "last_observed_at": None,
            "duplicates_removed": duplicates_removed,
            "raw_file_count": len(raw_paths),
        }
        _atomic_write_text(paths.metadata_path, json.dumps(metadata, indent=2))
        return metadata

    latest_date = history["observed_at"].max().date()
    current = history[history["observed_at"].dt.date == latest_date].copy()
    _atomic_write_parquet(history, paths.history_path)
    _atomic_write_parquet(current, paths.current_path)

    history_dates = config.thermal_history_dates
    metadata = {
        "event_id": event.id,
        "data_available": True,
        "observation_count": len(history),
        "first_observed_at": history["observed_at"].min().isoformat(),
        "last_observed_at": history["observed_at"].max().isoformat(),
        "current_observation_count": len(current),
        "current_observation_date": latest_date.isoformat(),
        "duplicates_removed": duplicates_removed,
        "raw_file_count": len(raw_paths),
        "source_products": sorted(history["source_product"].dropna().unique().tolist()),
        "requested_start_date": history_dates[0].isoformat() if history_dates else None,
        "requested_end_date": history_dates[1].isoformat() if history_dates else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_text(paths.metadata_path, json.dumps(metadata, indent=2))
    log.info(
        "[thermal-history] event %d normalized %d observation(s), removed %d duplicate(s)",
        event.id,
        len(history),
        duplicates_removed,
    )
    return metadata


def ensure_thermal_history(event, study) -> dict | None:
    """Collect configured history and regenerate its normalized contract."""
    config = get_event_config(event)
    if config.analysis_mode != THERMAL_MONITORING_MODE:
        return None

    auto_fetch = os.getenv("FIRMS_HISTORY_AUTO_FETCH", "1").strip().lower()
    if auto_fetch not in {"0", "false", "no", "off"}:
        collect_firms_history(event, study)
    paths = ThermalHistoryPaths.from_study(study)
    raw_inputs = list(paths.raw_dir.glob("*.csv")) + list(paths.raw_dir.glob("*.empty.json"))
    if paths.history_path.exists() and paths.metadata_path.exists() and raw_inputs:
        newest_input = max(path.stat().st_mtime for path in raw_inputs)
        if paths.history_path.stat().st_mtime >= newest_input:
            return json.loads(paths.metadata_path.read_text(encoding="utf-8"))
    return normalize_firms_history(event, study)


def load_history_metadata(event) -> dict | None:
    """Load event history metadata without importing the Study dependency."""
    project_dir = Path(__file__).resolve().parents[3] / "data" / "events" / (
        f"{event.year}_{event.id:04d}"
    )
    path = project_dir / "data_processed" / "thermal" / "history_metadata.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
