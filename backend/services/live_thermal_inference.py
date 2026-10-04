"""Live Isolation Forest inference over the application's current FIRMS history.

This module never trains a model.  It recreates the saved model's feature
contract from each region's enriched/aggregated FIRMS history, scores the most
recent UTC day, and applies the same source-event and fusion rules used by the
offline pipeline.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN


log = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_DIR / "data" / "events"
MODEL_PATH = Path(os.getenv(
    "THERMAL_IFOREST_MODEL_PATH",
    str(PROJECT_DIR / "models" / "industrial_iforest_train_2021_2024.joblib"),
))
EARTH_RADIUS_KM = 6371.0088
GRID_DEGREES = 0.01
SOURCE_RADIUS_KM = 1.0
MIN_SOURCE_SAMPLES = 3
MIN_BASELINE_DETECTIONS = 5
MIN_RELIABLE_DETECTIONS = 10
MIN_RELIABLE_ACTIVE_DAYS = 3

REGION_EVENT_IDS = {
    "vijayanagar": 2,
    "talcher_angul": 3,
    "dhanbad_bokaro": 4,
    "singrauli_sonbhadra": 5,
    "korba": 6,
    "jamnagar_vadinar": 7,
    "gadchiroli_tadoba": 8,
    "kanha_pench": 9,
    "bastar": 10,
    "mizoram": 11,
}
REGION_CANONICAL = {"vijayanagar": "vijayanagar_toranagallu"}


def _region_key(value: Any) -> str:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key == "vijayanagar_toranagallu":
        return "vijayanagar"
    return key


def _canonical_region(key: str) -> str:
    return REGION_CANONICAL.get(key, key)


def _empty_events() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "thermal_source_id", "region_name", "event_date", "event_start", "event_end",
        "latitude", "longitude", "detection_count", "max_frp", "mean_frp",
        "source_baseline_frp", "max_frp_ratio", "max_frp_zscore",
        "max_brightness_zscore", "max_source_anomaly_score", "mean_source_anomaly_score",
        "source_detections_prev_30d", "source_active_days_prev_30d", "max_alert_rank",
        "source_alert_level", "event_duration_hours", "strong_frp_event",
        "strong_brightness_event", "multi_detection_event", "source_event_score",
        "source_event_level", "source_event_reason", "source_event_id",
        "iforest_agreement", "iforest_evaluated", "max_iforest_anomaly_score",
        "iforest_match_count",
        "industrial_context_score", "final_risk_score", "final_risk_level",
        "final_confidence", "final_reason", "high_confidence_alert",
        "validated_by_unseen_iforest", "data_mode", "is_live", "model_name",
        "model_training_period", "latest_observation_at", "nearest_industry_name",
        "nearest_industry_type", "distance_to_nearest_industry_m",
        "builtup_fraction_500m", "industrial_feature_count_1km",
    ])


def _rolling(
    frame: pd.DataFrame,
    group_columns: list[str],
    value_column: str,
    window: str,
    operation: str,
    *,
    min_periods: int = 1,
    shift: bool = False,
) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for _, group in frame.groupby(group_columns, sort=False, dropna=False):
        ordered = group.sort_values(["timestamp", "_row_order"])
        values = pd.to_numeric(ordered[value_column], errors="coerce")
        series = pd.Series(values.to_numpy(), index=pd.DatetimeIndex(ordered["timestamp"]))
        if shift:
            series = series.shift(1)
        roller = series.rolling(window, min_periods=min_periods, closed="right")
        rolled = getattr(roller, operation)().to_numpy()
        result.loc[ordered.index] = rolled
    return result


def _rolling_counts(
    frame: pd.DataFrame,
    group_columns: list[str],
    window: str,
    *,
    shift: bool = False,
) -> pd.Series:
    marker = "_rolling_marker"
    working = frame.copy()
    working[marker] = 1.0
    return _rolling(
        working, group_columns, marker, window, "sum", min_periods=1, shift=shift
    )


def _active_days(frame: pd.DataFrame, group_column: str, *, previous_only: bool) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for _, group in frame.groupby(group_column, sort=False, dropna=False):
        days = pd.DatetimeIndex(group["timestamp"].dt.floor("D"))
        unique_days = pd.DatetimeIndex(sorted(days.unique()))
        counts = {}
        for day in unique_days:
            upper = day - pd.Timedelta(microseconds=1) if previous_only else day
            counts[day] = int(((unique_days > day - pd.Timedelta(days=30)) & (unique_days <= upper)).sum())
        result.loc[group.index] = [counts[day] for day in days]
    return result


def _risk_level(score: float) -> str:
    if score >= 0.80:
        return "critical"
    if score >= 0.60:
        return "high"
    if score >= 0.40:
        return "moderate"
    return "low"


class LiveThermalInferenceService:
    """Load the trained model once and cache scores until a region file changes."""

    def __init__(self):
        self._artifact: dict | None = None
        self._cache: dict[str, tuple[int, pd.DataFrame]] = {}
        self._lock = threading.RLock()

    @property
    def artifact(self) -> dict:
        if self._artifact is None:
            with self._lock:
                if self._artifact is None:
                    if not MODEL_PATH.exists():
                        raise FileNotFoundError(f"Isolation Forest model not found: {MODEL_PATH}")
                    artifact = joblib.load(MODEL_PATH)
                    required = {"pipeline", "features", "normalization"}
                    if not isinstance(artifact, dict) or not required.issubset(artifact):
                        raise ValueError("Isolation Forest artifact has an unsupported format")
                    self._artifact = artifact
        return self._artifact

    def _input_path(self, region: str) -> Path | None:
        event_id = REGION_EVENT_IDS.get(_region_key(region))
        if event_id is None:
            return None
        path = DATA_DIR / f"2026_{event_id:04d}" / "data_processed" / "thermal" / "detections_aggregated.parquet"
        return path if path.exists() else None

    def score_region(self, region: str) -> pd.DataFrame:
        key = _region_key(region)
        path = self._input_path(key)
        if path is None:
            return _empty_events()
        modified = path.stat().st_mtime_ns
        cached = self._cache.get(key)
        if cached and cached[0] == modified:
            return cached[1]
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] == modified:
                return cached[1]
            try:
                scored = self._score_frame(pd.read_parquet(path), key)
            except Exception:
                log.exception("Live thermal inference failed for region %s", key)
                return _empty_events()
            self._cache[key] = (modified, scored)
            return scored

    def all_events(self, region: str | None = None) -> pd.DataFrame:
        if region:
            return self.score_region(region).copy()
        frames = [self.score_region(key) for key in REGION_EVENT_IDS]
        frames = [frame for frame in frames if not frame.empty]
        return pd.concat(frames, ignore_index=True) if frames else _empty_events()

    def find_event(self, source_event_id: str) -> pd.DataFrame:
        """Resolve a live event without cold-scoring every configured region."""
        event_id = str(source_event_id)
        for region in REGION_EVENT_IDS:
            if event_id.startswith(f"live_{_canonical_region(region)}_source_"):
                frame = self.score_region(region)
                return frame[frame["source_event_id"].astype(str).eq(event_id)]
        # Unknown/legacy live id: inspect already-warm caches only.
        cached_frames = [item[1] for item in self._cache.values() if not item[1].empty]
        if not cached_frames:
            return _empty_events()
        frame = pd.concat(cached_frames, ignore_index=True, sort=False)
        return frame[frame["source_event_id"].astype(str).eq(event_id)]

    def invalidate(self, region: str | None = None) -> None:
        with self._lock:
            if region:
                self._cache.pop(_region_key(region), None)
            else:
                self._cache.clear()

    def _prepare_observations(self, raw: pd.DataFrame, region: str) -> pd.DataFrame:
        frame = raw.copy()
        frame["timestamp"] = pd.to_datetime(frame.get("observed_at"), utc=True, errors="coerce")
        frame = frame[frame["timestamp"].notna()].copy()
        if frame.empty:
            return frame
        frame["_row_order"] = np.arange(len(frame))
        frame["frp"] = pd.to_numeric(frame.get("frp_mean_mw", frame.get("frp")), errors="coerce")
        if "frp_mean_mw" in frame and "frp" in raw:
            frame["frp"] = frame["frp"].fillna(pd.to_numeric(raw.loc[frame.index, "frp"], errors="coerce"))
        frame["brightness"] = pd.to_numeric(
            frame.get("brightness_ti4_mean_k", frame.get("bright_ti4")), errors="coerce"
        )
        if "brightness_ti4_mean_k" in frame and "bright_ti4" in raw:
            frame["brightness"] = frame["brightness"].fillna(
                pd.to_numeric(raw.loc[frame.index, "bright_ti4"], errors="coerce")
            )
        frame = frame[frame[["latitude", "longitude", "frp", "brightness"]].notna().all(axis=1)].copy()
        frame["region_name"] = _canonical_region(region)
        frame["daynight"] = frame.get("daynight", "U").fillna("U").astype(str)
        frame["grid_lat"] = np.floor(frame["latitude"] / GRID_DEGREES).astype("int32")
        frame["grid_lon"] = np.floor(frame["longitude"] / GRID_DEGREES).astype("int32")
        frame["thermal_cell"] = frame["grid_lat"].astype(str) + "_" + frame["grid_lon"].astype(str)
        frame.sort_values(["thermal_cell", "timestamp", "_row_order"], inplace=True)
        return frame

    def _add_model_features(self, frame: pd.DataFrame) -> pd.DataFrame:
        # Detection behavior is cell-based; current/baseline thermal behavior
        # follows the training contract's cell + day/night grouping.
        frame["detections_24h"] = _rolling_counts(frame, ["thermal_cell"], "24h")
        frame["detections_7d"] = _rolling_counts(frame, ["thermal_cell"], "7D")
        frame["detections_30d"] = _rolling_counts(frame, ["thermal_cell"], "30D")
        frame["active_days_30d"] = _active_days(frame, "thermal_cell", previous_only=False)
        frame["persistence_30d"] = frame["active_days_30d"] / 30.0
        frame["hours_since_previous_detection"] = (
            frame["timestamp"] - frame.groupby("thermal_cell", sort=False)["timestamp"].shift(1)
        ).dt.total_seconds() / 3600.0

        keys = ["thermal_cell", "daynight"]
        frame["frp_mean_24h"] = _rolling(frame, keys, "frp", "24h", "mean")
        frame["frp_max_24h"] = _rolling(frame, keys, "frp", "24h", "max")
        frame["brightness_mean_24h"] = _rolling(frame, keys, "brightness", "24h", "mean")
        frame["brightness_max_24h"] = _rolling(frame, keys, "brightness", "24h", "max")
        for value, prefix in (("frp", "frp"), ("brightness", "brightness")):
            frame[f"{prefix}_mean_prev_30d"] = _rolling(
                frame, keys, value, "30D", "mean", min_periods=3, shift=True
            )
            frame[f"{prefix}_median_prev_30d"] = _rolling(
                frame, keys, value, "30D", "median", min_periods=3, shift=True
            )
            frame[f"{prefix}_std_prev_30d"] = _rolling(
                frame, keys, value, "30D", "std", min_periods=3, shift=True
            )
        frame["frp_ratio_24h_vs_30d"] = frame["frp_mean_24h"] / frame["frp_median_prev_30d"].replace(0, np.nan)
        frame["brightness_ratio_24h_vs_30d"] = frame["brightness_mean_24h"] / frame["brightness_median_prev_30d"].replace(0, np.nan)
        frame["frp_zscore_24h"] = (
            frame["frp_mean_24h"] - frame["frp_mean_prev_30d"]
        ) / frame["frp_std_prev_30d"].replace(0, np.nan)
        frame["brightness_zscore_24h"] = (
            frame["brightness_mean_24h"] - frame["brightness_mean_prev_30d"]
        ) / frame["brightness_std_prev_30d"].replace(0, np.nan)
        frame["frp_percent_change_24h"] = (
            (frame["frp_mean_24h"] - frame["frp_median_prev_30d"])
            / frame["frp_median_prev_30d"].replace(0, np.nan)
        ) * 100
        frame["brightness_percent_change_24h"] = (
            (frame["brightness_mean_24h"] - frame["brightness_median_prev_30d"])
            / frame["brightness_median_prev_30d"].replace(0, np.nan)
        ) * 100
        frame["has_30d_baseline"] = (
            frame["frp_median_prev_30d"].notna()
            & frame["brightness_median_prev_30d"].notna()
        )
        return frame

    def _industrial_mask(self, frame: pd.DataFrame) -> pd.Series:
        distance = pd.to_numeric(frame.get("distance_to_nearest_industry_m"), errors="coerce")
        built = pd.to_numeric(frame.get("builtup_fraction_500m"), errors="coerce")
        count = pd.to_numeric(frame.get("industrial_feature_count_1km"), errors="coerce")
        inside = frame.get("inside_industrial_polygon", pd.Series(False, index=frame.index))
        nearby = frame.get("near_industrial_facility", pd.Series(False, index=frame.index))
        return (
            inside.fillna(False).astype(bool)
            | nearby.fillna(False).astype(bool)
            | distance.le(2000)
            | built.ge(0.30)
            | count.ge(1)
        ).fillna(False)

    def _score_iforest(self, frame: pd.DataFrame) -> pd.DataFrame:
        artifact = self.artifact
        features = list(artifact["features"])
        frame["iforest_raw_score"] = np.nan
        frame["industrial_anomaly_score"] = np.nan
        frame["industrial_is_anomaly"] = False
        valid = (
            self._industrial_mask(frame)
            & frame["has_30d_baseline"].fillna(False)
            & frame["active_days_30d"].ge(3)
        )
        frame["iforest_eligible"] = valid
        if not valid.any():
            return frame
        inputs = frame.loc[valid, features].replace([np.inf, -np.inf], np.nan)
        raw_scores = artifact["pipeline"].score_samples(inputs)
        predictions = artifact["pipeline"].predict(inputs)
        low = float(artifact["normalization"]["low"])
        high = float(artifact["normalization"]["high"])
        strength = np.clip(-np.asarray(raw_scores), low, high)
        normalized = np.zeros(len(strength)) if high <= low else (strength - low) / (high - low)
        frame.loc[valid, "iforest_raw_score"] = raw_scores
        frame.loc[valid, "industrial_anomaly_score"] = normalized
        frame.loc[valid, "industrial_is_anomaly"] = predictions == -1
        return frame

    def _assign_sources(self, frame: pd.DataFrame, region: str) -> pd.DataFrame:
        frame["thermal_source_id"] = None
        mask = self._industrial_mask(frame)
        industrial = frame.loc[mask]
        if len(industrial) < MIN_SOURCE_SAMPLES:
            return frame
        coords = np.radians(industrial[["latitude", "longitude"]].to_numpy())
        labels = DBSCAN(
            eps=SOURCE_RADIUS_KM / EARTH_RADIUS_KM,
            min_samples=MIN_SOURCE_SAMPLES,
            metric="haversine",
            algorithm="ball_tree",
            n_jobs=-1,
        ).fit_predict(coords)
        canonical = _canonical_region(region)
        for index, label in zip(industrial.index, labels):
            if label >= 0:
                frame.at[index, "thermal_source_id"] = f"live_{canonical}_source_{int(label)}"
        return frame

    def _add_source_features(self, frame: pd.DataFrame) -> pd.DataFrame:
        valid = frame["thermal_source_id"].notna()
        source = frame.loc[valid].copy()
        if source.empty:
            return frame
        keys = ["thermal_source_id"]
        source["source_frp_mean_prev_30d"] = _rolling(source, keys, "frp", "30D", "mean", min_periods=MIN_BASELINE_DETECTIONS, shift=True)
        source["source_frp_std_prev_30d"] = _rolling(source, keys, "frp", "30D", "std", min_periods=MIN_BASELINE_DETECTIONS, shift=True)
        source["source_brightness_mean_prev_30d"] = _rolling(source, keys, "brightness", "30D", "mean", min_periods=MIN_BASELINE_DETECTIONS, shift=True)
        source["source_brightness_std_prev_30d"] = _rolling(source, keys, "brightness", "30D", "std", min_periods=MIN_BASELINE_DETECTIONS, shift=True)
        source["source_detections_prev_30d"] = _rolling_counts(source, keys, "30D", shift=True)
        source["source_active_days_prev_30d"] = _active_days(source, "thermal_source_id", previous_only=True)
        source["source_frp_ratio_30d"] = source["frp"] / source["source_frp_mean_prev_30d"].replace(0, np.nan)
        source["source_brightness_ratio_30d"] = source["brightness"] / source["source_brightness_mean_prev_30d"].replace(0, np.nan)
        source["source_frp_zscore_30d"] = np.where(
            source["source_frp_std_prev_30d"] > 0.05,
            (source["frp"] - source["source_frp_mean_prev_30d"]) / source["source_frp_std_prev_30d"],
            np.nan,
        )
        source["source_brightness_zscore_30d"] = np.where(
            source["source_brightness_std_prev_30d"] > 0.10,
            (source["brightness"] - source["source_brightness_mean_prev_30d"]) / source["source_brightness_std_prev_30d"],
            np.nan,
        )
        reliable = (
            source["source_frp_mean_prev_30d"].notna()
            & source["source_brightness_mean_prev_30d"].notna()
            & source["source_detections_prev_30d"].ge(MIN_RELIABLE_DETECTIONS)
            & source["source_active_days_prev_30d"].ge(MIN_RELIABLE_ACTIVE_DAYS)
        )
        source["source_history_reliable"] = reliable
        positive = (
            source["source_frp_ratio_30d"].ge(1.5)
            | source["source_frp_zscore_30d"].ge(2.0)
            | source["source_brightness_ratio_30d"].ge(1.05)
            | source["source_brightness_zscore_30d"].ge(2.0)
        )
        source["source_alert_candidate"] = reliable & positive
        frp_ratio_component = ((source["source_frp_ratio_30d"] - 1.0) / 4.0).clip(0, 1)
        frp_z_component = (source["source_frp_zscore_30d"] / 8.0).clip(0, 1)
        brightness_component = (source["source_brightness_zscore_30d"] / 8.0).clip(0, 1)
        source["source_anomaly_score"] = (
            0.40 * frp_ratio_component + 0.35 * frp_z_component + 0.25 * brightness_component
        )
        source.loc[~reliable, "source_anomaly_score"] = np.nan
        source["source_alert_level"] = "none"
        source.loc[source["source_alert_candidate"], "source_alert_level"] = "elevated"
        strong = source["source_alert_candidate"] & (
            source["source_frp_ratio_30d"].ge(2.0)
            | source["source_frp_zscore_30d"].ge(3.0)
            | source["source_brightness_zscore_30d"].ge(3.0)
        )
        source.loc[strong, "source_alert_level"] = "high"
        critical = source["source_alert_candidate"] & source["source_frp_zscore_30d"].ge(4.0) & source["source_frp_ratio_30d"].ge(2.0) & source["source_anomaly_score"].ge(0.70)
        source.loc[critical, "source_alert_level"] = "critical"
        # Only the source-derived columns need to be merged back. Reassigning
        # the entire frame coerces bool columns into existing float blocks on
        # newer pandas versions.
        source_columns = [
            "source_frp_mean_prev_30d", "source_frp_std_prev_30d",
            "source_brightness_mean_prev_30d", "source_brightness_std_prev_30d",
            "source_detections_prev_30d", "source_active_days_prev_30d",
            "source_frp_ratio_30d", "source_brightness_ratio_30d",
            "source_frp_zscore_30d", "source_brightness_zscore_30d",
            "source_history_reliable", "source_alert_candidate",
            "source_anomaly_score", "source_alert_level",
        ]
        bool_columns = {"source_history_reliable", "source_alert_candidate"}
        for column in source_columns:
            if column in bool_columns:
                frame[column] = pd.Series(False, index=frame.index, dtype=bool)
            elif column == "source_alert_level":
                frame[column] = pd.Series("none", index=frame.index, dtype=object)
            else:
                frame[column] = np.nan
            frame.loc[source.index, column] = source[column].to_numpy()
        return frame

    def _build_events(self, frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return _empty_events()
        latest = frame["timestamp"].max()
        current_day = latest.floor("D")
        alert_candidate = frame.get(
            "source_alert_candidate", pd.Series(False, index=frame.index)
        )
        candidates = frame[
            frame["timestamp"].dt.floor("D").eq(current_day)
            & alert_candidate.fillna(False).astype(bool)
        ].copy()
        if candidates.empty:
            return _empty_events()
        candidates["event_date"] = candidates["timestamp"].dt.floor("D")
        candidates["alert_rank"] = candidates["source_alert_level"].map({"none": 0, "elevated": 1, "high": 2, "critical": 3}).fillna(0).astype(int)
        candidates["iforest_positive_alert"] = (
            candidates["industrial_is_anomaly"].fillna(False).astype(bool)
            & (
                candidates["frp_ratio_24h_vs_30d"].ge(1.5)
                | candidates["frp_zscore_24h"].ge(2.0)
                | candidates["brightness_ratio_24h_vs_30d"].ge(1.05)
                | candidates["brightness_zscore_24h"].ge(2.0)
            )
        )
        grouped = candidates.groupby(["thermal_source_id", "region_name", "event_date"], as_index=False).agg(
            event_start=("timestamp", "min"), event_end=("timestamp", "max"),
            latitude=("latitude", "median"), longitude=("longitude", "median"),
            detection_count=("timestamp", "size"), max_frp=("frp", "max"), mean_frp=("frp", "mean"),
            source_baseline_frp=("source_frp_mean_prev_30d", "median"),
            max_frp_ratio=("source_frp_ratio_30d", "max"), max_frp_zscore=("source_frp_zscore_30d", "max"),
            max_brightness_zscore=("source_brightness_zscore_30d", "max"),
            max_source_anomaly_score=("source_anomaly_score", "max"), mean_source_anomaly_score=("source_anomaly_score", "mean"),
            source_detections_prev_30d=("source_detections_prev_30d", "max"),
            source_active_days_prev_30d=("source_active_days_prev_30d", "max"), max_alert_rank=("alert_rank", "max"),
            iforest_agreement=("iforest_positive_alert", "max"),
            iforest_evaluated=("iforest_eligible", "max"),
            max_iforest_anomaly_score=("industrial_anomaly_score", "max"), iforest_match_count=("iforest_positive_alert", "sum"),
            nearest_industry_name=("nearest_industry_name", "first"), nearest_industry_type=("nearest_industry_type", "first"),
            distance_to_nearest_industry_m=("distance_to_nearest_industry_m", "min"),
            builtup_fraction_500m=("builtup_fraction_500m", "mean"),
            industrial_feature_count_1km=("industrial_feature_count_1km", "max"),
        )
        grouped["source_alert_level"] = grouped["max_alert_rank"].map({0: "none", 1: "elevated", 2: "high", 3: "critical"})
        grouped["event_duration_hours"] = (grouped["event_end"] - grouped["event_start"]).dt.total_seconds() / 3600.0
        grouped["strong_frp_event"] = grouped["max_frp_ratio"].ge(2.0) | grouped["max_frp_zscore"].ge(3.0)
        grouped["strong_brightness_event"] = grouped["max_brightness_zscore"].ge(3.0)
        grouped["multi_detection_event"] = grouped["detection_count"].ge(2)
        grouped["source_event_score"] = (
            0.25 * ((grouped["max_frp_ratio"] - 1.0) / 5.0).clip(0, 1)
            + 0.20 * (grouped["max_frp_zscore"] / 10.0).clip(0, 1)
            + 0.15 * (grouped["max_brightness_zscore"] / 10.0).clip(0, 1)
            + 0.30 * grouped["max_source_anomaly_score"].clip(0, 1)
            + 0.10 * (grouped["detection_count"] / 5.0).clip(0, 1)
        )
        grouped["source_event_level"] = grouped["source_event_score"].map(_risk_level)
        grouped["source_event_reason"] = grouped.apply(self._source_reason, axis=1)
        grouped["source_event_id"] = grouped["thermal_source_id"].astype(str) + "_" + grouped["event_date"].dt.strftime("%Y%m%d")
        grouped["industrial_context_score"] = 1.0
        grouped["final_risk_score"] = (
            0.40 * grouped["source_event_score"].clip(0, 1)
            + 0.20 * grouped["max_iforest_anomaly_score"].fillna(0).clip(0, 1)
            + 0.15 * grouped["iforest_agreement"].astype(float)
            + 0.10 * ((grouped["max_frp_ratio"] - 1.0) / 5.0).clip(0, 1)
            + 0.05 * (grouped["max_brightness_zscore"] / 10.0).clip(0, 1)
            + 0.05 * (grouped["detection_count"] / 5.0).clip(0, 1)
            + 0.05
        ).clip(0, 1)
        grouped["final_risk_level"] = grouped["final_risk_score"].map(_risk_level)
        grouped["final_confidence"] = grouped.apply(self._confidence, axis=1)
        grouped["final_reason"] = grouped.apply(self._final_reason, axis=1)
        grouped["high_confidence_alert"] = grouped["final_risk_level"].isin(["high", "critical"]) & grouped["final_confidence"].eq("high")
        grouped["validated_by_unseen_iforest"] = False
        grouped["data_mode"] = "live"
        grouped["is_live"] = True
        grouped["model_name"] = "Isolation Forest"
        grouped["model_training_period"] = str(self.artifact.get("training_period") or "2021-2024")
        grouped["latest_observation_at"] = latest
        return grouped.sort_values("final_risk_score", ascending=False).reset_index(drop=True)

    @staticmethod
    def _source_reason(row) -> str:
        reasons = []
        if pd.notna(row["max_frp_ratio"]) and row["max_frp_ratio"] >= 1.5:
            reasons.append(f"FRP {row['max_frp_ratio']:.1f}x baseline")
        if pd.notna(row["max_frp_zscore"]) and row["max_frp_zscore"] >= 2:
            reasons.append(f"FRP z={row['max_frp_zscore']:.1f}")
        if pd.notna(row["max_brightness_zscore"]) and row["max_brightness_zscore"] >= 2:
            reasons.append(f"brightness z={row['max_brightness_zscore']:.1f}")
        if row["detection_count"] >= 2:
            reasons.append(f"{int(row['detection_count'])} detections")
        reasons.append(f"event score={row['source_event_score']:.2f}")
        return "; ".join(reasons)

    @staticmethod
    def _confidence(row) -> str:
        signals = sum([
            row["source_event_score"] >= 0.60,
            bool(row["iforest_agreement"]),
            bool(row["strong_frp_event"]),
            bool(row["strong_brightness_event"]),
            bool(row["multi_detection_event"]),
        ])
        return "high" if signals >= 4 else "medium" if signals >= 2 else "low"

    @staticmethod
    def _final_reason(row) -> str:
        reasons = ["live FIRMS source-specific assessment"]
        if row["source_event_score"] >= 0.60:
            reasons.append(f"source event score={row['source_event_score']:.2f}")
        if row["iforest_agreement"]:
            reasons.append("Isolation Forest also flagged anomaly")
        if pd.notna(row["max_frp_ratio"]) and row["max_frp_ratio"] >= 2:
            reasons.append(f"FRP {row['max_frp_ratio']:.1f}x baseline")
        if pd.notna(row["max_frp_zscore"]) and row["max_frp_zscore"] >= 3:
            reasons.append(f"FRP z={row['max_frp_zscore']:.1f}")
        if pd.notna(row["max_brightness_zscore"]) and row["max_brightness_zscore"] >= 3:
            reasons.append(f"brightness z={row['max_brightness_zscore']:.1f}")
        if row["multi_detection_event"]:
            reasons.append(f"{int(row['detection_count'])} detections")
        return "; ".join(reasons)

    def _score_frame(self, raw: pd.DataFrame, region: str) -> pd.DataFrame:
        frame = self._prepare_observations(raw, region)
        if frame.empty:
            return _empty_events()
        frame = self._add_model_features(frame)
        frame = self._score_iforest(frame)
        frame = self._assign_sources(frame, region)
        frame = self._add_source_features(frame)
        return self._build_events(frame)


live_thermal_inference = LiveThermalInferenceService()
