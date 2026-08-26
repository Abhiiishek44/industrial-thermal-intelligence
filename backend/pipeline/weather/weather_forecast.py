"""
pipeline/weather/weather_forecast.py
--------------------------------------
Build hourly weather forecast (+12h) from ERA5 parquet for one timestep.

Outputs (timestep_dir/weather/):
  forecast.json   — [{hour, temp_c, rh, wind_speed_kmh, max_wind_speed_kmh, wind_dir}, ...]
  wind_field.json — leaflet-velocity format: [{hour, data:[u_obj, v_obj]}, ...]

ERA5 valid_time is stored in local (Mountain) time matching fire_state keys.
wind_speed in ERA5 is m/s — converted to km/h (* 3.6) where needed.
u10/v10 are kept in m/s (leaflet-velocity standard).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_HOURS = 12


def build_weather_forecast(study, t1: pd.Timestamp, out_dir: Path) -> None:
    """Extract next 12 hours of ERA5 weather and write forecast.json + wind_field.json.

    Args:
        study:   whp.Study instance (provides data_processed_dir)
        t1:      Observation time (naive local time; ERA5 stored in same tz)
        out_dir: Output directory (e.g. timestep_dir / "weather")
    """
    forecast_path  = out_dir / "forecast.json"
    wind_field_path = out_dir / "wind_field.json"
    if forecast_path.exists() and wind_field_path.exists():
        return

    era5_path = study.data_processed_dir / "weather" / "era5.parquet"
    if not era5_path.exists():
        log.warning("[weather] era5.parquet not found — skipping weather forecast")
        return

    df = pd.read_parquet(era5_path)
    df["valid_time"] = pd.to_datetime(df["valid_time"])

    # Infer regular grid dimensions once (same for all hours)
    sample = df[df["valid_time"] == df["valid_time"].iloc[0]]
    lats = sorted(sample["latitude"].unique())
    lons = sorted(sample["longitude"].unique())
    ny, nx = len(lats), len(lons)
    la1 = float(max(lats))   # north edge (leaflet-velocity starts from top)
    la2 = float(min(lats))
    lo1 = float(min(lons))
    lo2 = float(max(lons))
    dy  = round((la1 - la2) / max(ny - 1, 1), 6)
    dx  = round((lo2 - lo1) / max(nx - 1, 1), 6)

    t0 = pd.Timestamp(t1).floor("h")
    forecast_records = []
    wind_field_hours = []

    for h in range(_HOURS + 1):
        t = t0 + pd.Timedelta(hours=h)
        sub = df[df["valid_time"] == t]
        if sub.empty:
            continue

        ws_kmh = sub["wind_speed"] * 3.6

        # ── Area-average forecast record ──────────────────────────────────────
        forecast_records.append({
            "hour":               h,
            "temp_c":             round(float(sub["temp_c"].mean()), 1),
            "rh":                 round(float(sub["rh"].mean()), 1),
            "wind_speed_kmh":     round(float(ws_kmh.mean()), 1),
            "max_wind_speed_kmh": round(float(ws_kmh.max()), 1),
            "wind_dir":           round(float(sub["wind_dir"].mean()), 1),
        })

        # ── Wind field (leaflet-velocity GRIB-style) ──────────────────────────
        # Sort: north→south (descending lat), west→east (ascending lon)
        sub_sorted = sub.sort_values(["latitude", "longitude"],
                                     ascending=[False, True])
        header = {
            "parameterCategory": 2,
            "lo1": lo1, "la1": la1, "lo2": lo2, "la2": la2,
            "dx": dx, "dy": dy, "nx": nx, "ny": ny,
            "refTime": t.isoformat(),
        }
        u_data = [round(float(v), 3) for v in sub_sorted["u10"]]
        v_data = [round(float(v), 3) for v in sub_sorted["v10"]]

        wind_field_hours.append({
            "hour": h,
            "data": [
                {"header": {**header, "parameterNumber": 2}, "data": u_data},
                {"header": {**header, "parameterNumber": 3}, "data": v_data},
            ],
        })

    if not forecast_records:
        log.warning("[weather] no ERA5 data found for t1=%s", t1)
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    forecast_path.write_text(
        json.dumps(forecast_records, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    wind_field_path.write_text(
        json.dumps(wind_field_hours, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    log.info("[weather] forecast.json + wind_field.json written → %d hours", len(forecast_records))
