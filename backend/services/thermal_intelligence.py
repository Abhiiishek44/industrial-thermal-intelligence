"""Cached access to the prepared thermal anomaly and source artifacts.

The Isolation Forest and preprocessing pipeline are intentionally not executed
here.  This service only reads their final, presentation-ready Parquet output.
"""

from __future__ import annotations

import math
import os
import threading
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


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
    "final_risk_level",
    "final_confidence",
    "final_risk_score",
    "max_frp",
    "source_baseline_frp",
    "max_frp_ratio",
    "iforest_agreement",
    "detection_count",
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
    ) -> pd.DataFrame:
        frame = self.events
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
        return [json_safe(record) for record in selected.to_dict("records")]

    def stats(self, *, region: str | None = None, year: int | None = None) -> dict:
        frame = self._filter_events(region=region, year=year)
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
        matches = self.events[
            self.events["source_event_id"].astype(str).eq(str(source_event_id))
        ]
        if matches.empty:
            return None
        event = self._records(matches.head(1))[0]
        # Source fields are contextual evidence, not a verified source label.
        _ = self.sources
        source = (self._source_lookup or {}).get(str(event.get("thermal_source_id")))
        event["assessment"] = "Abnormal thermal signature"
        event["supporting_context"] = json_safe({
            key: value for key, value in (source or {}).items()
            if key not in {"_region_key", "thermal_source_id", "region_name"}
        })
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
