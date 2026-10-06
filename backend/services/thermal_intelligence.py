"""Cached access to the prepared thermal anomaly and source artifacts.

The Isolation Forest and preprocessing pipeline are intentionally not executed
here.  This service only reads their final, presentation-ready Parquet output.
"""

from __future__ import annotations

import math
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd

from services.live_thermal_inference import live_thermal_inference


PROJECT_DIR = Path(__file__).resolve().parents[2]
LOCAL_EVENTS_PATH = PROJECT_DIR / "data" / "processed" / "final_fused_events.parquet"
LOCAL_SOURCES_PATH = PROJECT_DIR / "data" / "processed" / "thermal_sources.parquet"
BUNDLED_EVENTS_PATH = PROJECT_DIR / "runtime_artifacts" / "final_fused_events.parquet"
BUNDLED_SOURCES_PATH = PROJECT_DIR / "runtime_artifacts" / "thermal_sources.parquet"
DEFAULT_EVENTS_PATH = LOCAL_EVENTS_PATH if LOCAL_EVENTS_PATH.exists() else BUNDLED_EVENTS_PATH
DEFAULT_SOURCES_PATH = LOCAL_SOURCES_PATH if LOCAL_SOURCES_PATH.exists() else BUNDLED_SOURCES_PATH

MAP_FIELDS = (
    "source_event_id",
    "thermal_source_id",
    "latitude",
    "longitude",
    "region_name",
    "event_start",
    "final_risk_level",
    "final_confidence",
    "final_risk_score",
    "max_frp",
    "source_baseline_frp",
    "max_frp_ratio",
    "iforest_agreement",
    "iforest_evaluated",
    "detection_count",
    "data_mode",
    "is_live",
    "latest_observation_at",
    "model_name",
    "nearest_industry_name",
    "nearest_industry_type",
    "distance_to_nearest_industry_m",
)


def json_safe(value: Any) -> Any:
    """Convert pandas/NumPy scalar values into strict JSON-compatible values."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, np.datetime64):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _normalise_region(value: Any) -> str:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    # The existing dashboard uses the shorter catalog id for this region.
    return {"vijayanagar": "vijayanagar_toranagallu"}.get(key, key)


def _risk_event_today() -> pd.Timestamp:
    """Return today's midnight for presentation of replayed risk events."""
    timezone_name = os.getenv("RISK_EVENT_TIMEZONE", "Asia/Kolkata")
    try:
        application_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        application_timezone = ZoneInfo("UTC")
    return pd.Timestamp(datetime.now(application_timezone).date(), tz="UTC")


def _present_risk_event_dates(
    frame: pd.DataFrame, *, today: pd.Timestamp | None = None
) -> pd.DataFrame:
    """Rebase risk-event occurrence dates to today without changing clock times.

    Prepared risk events are replay data, but the operational dashboard presents
    them as today's events. Source-history and model-training dates are excluded
    deliberately because they describe provenance rather than event occurrence.
    """
    presented = frame.copy()
    target_day = (today if today is not None else _risk_event_today()).floor("D")
    for column in ("event_date", "event_start", "event_end", "latest_observation_at"):
        if column not in presented.columns:
            continue
        timestamps = pd.to_datetime(presented[column], utc=True, errors="coerce")
        time_of_day = timestamps - timestamps.dt.floor("D")
        presented[column] = target_day + time_of_day
    return presented


class ThermalIntelligenceService:
    """Thread-safe, lazy, process-local cache for prepared thermal artifacts."""

    def __init__(self, events_path: Path | None = None, sources_path: Path | None = None):
        self.events_path = events_path or Path(
            os.getenv("THERMAL_EVENTS_PATH", str(DEFAULT_EVENTS_PATH))
        )
        self.sources_path = sources_path or Path(
            os.getenv("THERMAL_SOURCES_PATH", str(DEFAULT_SOURCES_PATH))
        )
        self._events: pd.DataFrame | None = None
        self._sources: pd.DataFrame | None = None
        self._source_lookup: dict[str, dict[str, Any]] | None = None
        self._lock = threading.RLock()

    def _read_parquet(self, path: Path, label: str) -> pd.DataFrame:
        if not path.exists():
            raise FileNotFoundError(f"{label} artifact not found: {path}")
        return pd.read_parquet(path)

    @property
    def events(self) -> pd.DataFrame:
        if self._events is None:
            with self._lock:
                if self._events is None:
                    frame = self._read_parquet(self.events_path, "Fused thermal events")
                    required = {
                        "source_event_id", "thermal_source_id", "event_start",
                        "region_name", "final_risk_score", "final_risk_level",
                    }
                    missing = required.difference(frame.columns)
                    if missing:
                        raise ValueError(
                            "Fused thermal events artifact is missing: "
                            + ", ".join(sorted(missing))
                        )
                    frame = frame.copy()
                    frame["_region_key"] = frame["region_name"].map(_normalise_region)
                    frame["_event_year"] = pd.to_datetime(
                        frame["event_start"], utc=True, errors="coerce"
                    ).dt.year
                    frame["data_mode"] = "historical"
                    frame["is_live"] = False
                    self._events = frame
        return self._events

    @property
    def sources(self) -> pd.DataFrame:
        if self._sources is None:
            with self._lock:
                if self._sources is None:
                    frame = self._read_parquet(self.sources_path, "Thermal sources")
                    if "thermal_source_id" not in frame.columns:
                        raise ValueError("Thermal sources artifact has no thermal_source_id")
                    frame = frame.copy()
                    frame["_region_key"] = frame.get(
                        "region_name", pd.Series("", index=frame.index)
                    ).map(_normalise_region)
                    self._sources = frame
                    self._source_lookup = {
                        str(record["thermal_source_id"]): record
                        for record in frame.to_dict("records")
                    }
        return self._sources

    def _filter_events(
        self,
        *,
        risk_levels: Iterable[str] | None = None,
        confidence: str | None = None,
        region: str | None = None,
        year: int | None = None,
        iforest_agreement: bool | None = None,
        data_mode: str | None = None,
    ) -> pd.DataFrame:
        mode = str(data_mode or "all").strip().lower()
        frames = []
        if mode not in {"live", "historical"}:
            mode = "all"
        if mode in {"all", "historical"}:
            frames.append(self.events)
        if mode in {"all", "live"}:
            live = live_thermal_inference.all_events(region)
            if not live.empty:
                live = live.copy()
                live["_region_key"] = live["region_name"].map(_normalise_region)
                live["_event_year"] = pd.to_datetime(
                    live["event_start"], utc=True, errors="coerce"
                ).dt.year
                frames.append(live)
        frame = pd.concat(frames, ignore_index=True, sort=False) if frames else self.events.iloc[0:0]
        mask = pd.Series(True, index=frame.index)
        if risk_levels:
            wanted = {str(level).strip().lower() for level in risk_levels if str(level).strip()}
            mask &= frame["final_risk_level"].astype(str).str.lower().isin(wanted)
        if confidence:
            mask &= frame["final_confidence"].astype(str).str.lower().eq(confidence.lower())
        if region:
            mask &= frame["_region_key"].eq(_normalise_region(region))
        if year is not None:
            mask &= frame["_event_year"].eq(year)
        if iforest_agreement is not None:
            mask &= frame["iforest_agreement"].fillna(False).astype(bool).eq(iforest_agreement)
        return frame.loc[mask]

    @staticmethod
    def _records(frame: pd.DataFrame, fields: Iterable[str] | None = None) -> list[dict]:
        selected = frame[list(fields)] if fields is not None else frame.drop(
            columns=["_region_key", "_event_year"], errors="ignore"
        )
        selected = _present_risk_event_dates(selected)
        return [json_safe(record) for record in selected.to_dict("records")]

    def stats(
        self, *, region: str | None = None, year: int | None = None,
        data_mode: str | None = None,
    ) -> dict:
        frame = self._filter_events(region=region, year=year, data_mode=data_mode)
        levels = frame["final_risk_level"].astype(str).str.lower().value_counts()
        regions = frame["region_name"].astype(str).value_counts().sort_index()
        high_confidence = (
            frame["high_confidence_alert"].fillna(False).astype(bool).sum()
            if "high_confidence_alert" in frame
            else frame["final_confidence"].astype(str).str.lower().eq("high").sum()
        )
        return json_safe({
            "total_events": len(frame),
            "low_events": int(levels.get("low", 0)),
            "moderate_events": int(levels.get("moderate", 0)),
            "high_events": int(levels.get("high", 0)),
            "critical_events": int(levels.get("critical", 0)),
            "high_confidence_alerts": int(high_confidence),
            "iforest_agreement_count": int(
                frame["iforest_agreement"].fillna(False).astype(bool).sum()
            ),
            "live_events": int(frame.get("is_live", False).fillna(False).astype(bool).sum()),
            "latest_live_observation": (
                pd.to_datetime(
                    frame.loc[frame.get("is_live", False).fillna(False).astype(bool), "latest_observation_at"],
                    utc=True, errors="coerce",
                ).max()
                if "latest_observation_at" in frame else None
            ),
            "model_name": "Isolation Forest",
            "model_training_period": "2021-01-01 to 2024-12-31",
            "counts_per_region": {str(key): int(value) for key, value in regions.items()},
        })

    def list_events(self, *, limit: int, offset: int = 0, **filters) -> dict:
        frame = self._filter_events(**filters).sort_values(
            "final_risk_score", ascending=False, na_position="last"
        )
        page = frame.iloc[offset:offset + limit]
        return {
            "events": self._records(page),
            "count": len(page),
            "total": len(frame),
            "limit": limit,
            "offset": offset,
        }

    def map_events(self, *, limit: int, **filters) -> dict:
        frame = self._filter_events(**filters).sort_values(
            "final_risk_score", ascending=False, na_position="last"
        )
        available_fields = [field for field in MAP_FIELDS if field in frame.columns]
        page = frame.head(limit)
        return {
            "events": self._records(page, available_fields),
            "count": len(page),
            "total": len(frame),
            "limit": limit,
        }

    def event_detail(self, source_event_id: str) -> dict | None:
        frame = self.events
        if str(source_event_id).startswith("live_"):
            matches = live_thermal_inference.find_event(source_event_id)
        else:
            matches = frame[frame["source_event_id"].astype(str).eq(str(source_event_id))]
        if matches.empty:
            return None
        event = self._records(matches.head(1))[0]
        # Source fields are contextual evidence, not a verified source label.
        source = None
        if not event.get("is_live"):
            _ = self.sources
            source = (self._source_lookup or {}).get(str(event.get("thermal_source_id")))
        event["assessment"] = "Abnormal thermal signature"
        event["supporting_context"] = json_safe({
            key: value for key, value in (source or {}).items()
            if key not in {"_region_key", "thermal_source_id", "region_name"}
        })
        if event.get("is_live"):
            event["supporting_context"] = {
                "data_mode": "live",
                "model_name": event.get("model_name"),
                "model_training_period": event.get("model_training_period"),
                "latest_observation_at": event.get("latest_observation_at"),
                "nearest_industry_name": event.get("nearest_industry_name"),
                "nearest_industry_type": event.get("nearest_industry_type"),
                "distance_to_nearest_industry_m": event.get("distance_to_nearest_industry_m"),
            }
        return event

    def list_sources(self, *, region: str | None, limit: int, offset: int = 0) -> dict:
        frame = self.sources
        if region:
            frame = frame[frame["_region_key"].eq(_normalise_region(region))]
        sort_column = "total_detections" if "total_detections" in frame else "thermal_source_id"
        frame = frame.sort_values(sort_column, ascending=False, na_position="last")
        page = frame.iloc[offset:offset + limit].drop(columns=["_region_key"], errors="ignore")
        return {
            "sources": self._records(page),
            "count": len(page),
            "total": len(frame),
            "limit": limit,
            "offset": offset,
        }


thermal_intelligence = ThermalIntelligenceService()
