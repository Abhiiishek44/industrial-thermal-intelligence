"""Region-neutral FIRMS/context feature generation."""

from __future__ import annotations

import numpy as np
import pandas as pd

from pipeline.training_data.temporal import add_strictly_causal_temporal_features


NUMERIC_FEATURES = (
    "frp", "log1p_frp", "bright_ti4", "bright_ti5", "brightness_delta",
    "scan", "track", "local_solar_hour", "day_of_year_sin", "day_of_year_cos",
    "inside_authoritative_industrial_area", "inside_osm_industrial_area",
    "distance_to_authoritative_industrial_area_m", "distance_to_nearest_industry_m",
    "industrial_feature_count_500m", "industrial_feature_count_1km",
    "landcover_code", "forest_fraction_500m", "cropland_fraction_500m",
    "builtup_fraction_500m", "prior_detection_count_7d", "prior_detection_count_30d",
    "prior_detection_count_90d", "prior_active_days_30d", "prior_active_days_90d",
    "days_since_previous_detection", "prior_frp_mean_30d",
)

CATEGORICAL_FEATURES = (
    "sensor", "instrument", "confidence", "daynight", "nearest_industry_type",
    "landcover_class", "landcover_group",
)

OPTIONAL_WEATHER_FEATURES = (
    "temp_c", "dewpoint_c", "rh", "precip_mm", "wind_speed", "wind_direction",
    "u10", "v10",
)


def _series(frame: pd.DataFrame, name: str, default=None) -> pd.Series:
    if name in frame:
        return frame[name]
    return pd.Series(default, index=frame.index)


def _canonical_sensor(row: pd.Series) -> str:
    product = str(row.get("source_product", "")).upper()
    satellite = str(row.get("satellite", "")).upper()
    instrument = str(row.get("instrument", "")).upper()
    if "NOAA20" in product or satellite == "N20":
        return "viirs_noaa20"
    if "NOAA21" in product or satellite == "N21":
        return "viirs_noaa21"
    if "SNPP" in product or (instrument == "VIIRS" and satellite in {"N", "SNPP"}):
        return "viirs_snpp"
    if instrument == "MODIS" and satellite in {"T", "TERRA"}:
        return "modis_terra"
    if instrument == "MODIS" and satellite in {"A", "AQUA"}:
        return "modis_aqua"
    return f"{instrument.lower()}_{satellite.lower()}".strip("_") or "unknown"


def generate_features(observations: pd.DataFrame) -> pd.DataFrame:
    result = observations.copy()
    observed = pd.to_datetime(result["observed_at"], utc=True)
    result["frp"] = pd.to_numeric(_series(result, "frp"), errors="coerce")
    result["log1p_frp"] = np.log1p(result["frp"].clip(lower=0))
    result["bright_ti4"] = pd.to_numeric(_series(result, "bright_ti4"), errors="coerce")
    result["bright_ti5"] = pd.to_numeric(_series(result, "bright_ti5"), errors="coerce")
    result["brightness_delta"] = result["bright_ti4"] - result["bright_ti5"]
    result["scan"] = pd.to_numeric(_series(result, "scan"), errors="coerce")
    result["track"] = pd.to_numeric(_series(result, "track"), errors="coerce")
    result["sensor"] = result.apply(_canonical_sensor, axis=1)
    result["instrument"] = _series(result, "instrument", "unknown").fillna("unknown").astype(str)
    result["confidence"] = _series(result, "confidence", "unknown").fillna("unknown").astype(str).str.lower()
    result["daynight"] = _series(result, "daynight", "unknown").fillna("unknown").astype(str).str.upper()

    local_hour = (observed.dt.hour + observed.dt.minute / 60.0 + result["longitude"] / 15.0) % 24.0
    result["local_solar_hour"] = local_hour
    day_angle = 2.0 * np.pi * (observed.dt.dayofyear - 1) / 365.25
    result["day_of_year_sin"] = np.sin(day_angle)
    result["day_of_year_cos"] = np.cos(day_angle)

    result["inside_authoritative_industrial_area"] = _series(result, "inside_midc", False).fillna(False).astype(bool)
    result["inside_osm_industrial_area"] = _series(result, "inside_industrial_polygon", False).fillna(False).astype(bool)
    result["distance_to_authoritative_industrial_area_m"] = pd.to_numeric(
        _series(result, "distance_to_midc_m"), errors="coerce",
    )
    for name in (
        "distance_to_nearest_industry_m", "industrial_feature_count_500m",
        "industrial_feature_count_1km", "landcover_code", "forest_fraction_500m",
        "cropland_fraction_500m", "builtup_fraction_500m",
    ):
        result[name] = pd.to_numeric(_series(result, name), errors="coerce")
    for name in ("nearest_industry_type", "landcover_class", "landcover_group"):
        result[name] = _series(result, name, "unknown").fillna("unknown").astype(str)
    return add_strictly_causal_temporal_features(result)


def feature_schema() -> dict:
    return {
        "numeric": list(NUMERIC_FEATURES),
        "categorical": list(CATEGORICAL_FEATURES),
        "optional_weather": list(OPTIONAL_WEATHER_FEATURES),
        "excluded_identifiers": [
            "latitude", "longitude", "observed_at", "acq_date", "acq_time",
            "source_file", "nearest_industry_name", "type", "version",
        ],
        "temporal_contract": "all recurrence features use strictly earlier observations only",
        "worldcover_vintage": "ESA WorldCover 2021; consumers must evaluate event-year mismatch",
        "osm_vintage": "cache snapshot time, not observation time; treat as potentially mismatched",
    }
