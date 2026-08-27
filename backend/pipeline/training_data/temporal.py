"""Strictly causal recurrence features; same/future timestamps are invisible."""

from __future__ import annotations

import numpy as np
import pandas as pd


EARTH_RADIUS_M = 6_371_008.8


def _distances_m(latitude: float, longitude: float, latitudes, longitudes) -> np.ndarray:
    lat1 = np.radians(latitude)
    lon1 = np.radians(longitude)
    lat2 = np.radians(np.asarray(latitudes, dtype=float))
    lon2 = np.radians(np.asarray(longitudes, dtype=float))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(value, 0, 1)))


def add_strictly_causal_temporal_features(
    frame: pd.DataFrame, *, radius_m: float = 1000.0,
) -> pd.DataFrame:
    result = frame.sort_values(
        ["region_id", "observed_at", "latitude", "longitude", "observation_id"],
        kind="mergesort",
    ).reset_index(drop=True).copy()
    times = pd.to_datetime(result["observed_at"], utc=True)
    output = {name: np.full(len(result), np.nan) for name in (
        "prior_detection_count_7d", "prior_detection_count_30d", "prior_detection_count_90d",
        "prior_active_days_30d", "prior_active_days_90d", "days_since_previous_detection",
        "prior_frp_mean_30d",
    )}
    for _, indexes in result.groupby("region_id", sort=True).groups.items():
        indexes = np.asarray(list(indexes), dtype=int)
        region_times = times.iloc[indexes]
        for offset, index in enumerate(indexes):
            current_time = times.iloc[index]
            earlier_offsets = np.flatnonzero(
                ((current_time - region_times.iloc[:offset]) > pd.Timedelta(0)).to_numpy()
            )
            if earlier_offsets.size == 0:
                for name in output:
                    output[name][index] = 0.0 if "count" in name or "active_days" in name else np.nan
                continue
            candidates = indexes[earlier_offsets]
            age_days = (current_time - times.iloc[candidates]).dt.total_seconds().to_numpy() / 86400.0
            within_space = _distances_m(
                result.at[index, "latitude"], result.at[index, "longitude"],
                result.loc[candidates, "latitude"], result.loc[candidates, "longitude"],
            ) <= radius_m
            for days in (7, 30, 90):
                selected = within_space & (age_days <= days)
                output[f"prior_detection_count_{days}d"][index] = float(selected.sum())
            for days in (30, 90):
                selected_indexes = candidates[within_space & (age_days <= days)]
                output[f"prior_active_days_{days}d"][index] = float(
                    times.iloc[selected_indexes].dt.date.nunique(),
                )
            spatial_earlier = candidates[within_space]
            if spatial_earlier.size:
                output["days_since_previous_detection"][index] = float(
                    (current_time - times.iloc[spatial_earlier].max()).total_seconds() / 86400.0,
                )
            selected_30 = candidates[within_space & (age_days <= 30)]
            if selected_30.size:
                output["prior_frp_mean_30d"][index] = float(
                    pd.to_numeric(result.loc[selected_30, "frp"], errors="coerce").mean(),
                )
    for name, values in output.items():
        result[name] = values
    return result
