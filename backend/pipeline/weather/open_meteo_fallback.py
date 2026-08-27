"""Cached point-weather fallback for thermal-monitoring replay dashboards."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
)


def _utc_hour(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.floor("h")


def _open_meteo_records(payload: dict, t1, source: str) -> list[dict]:
    """Convert Open-Meteo's columnar hourly response into dashboard records."""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    values = {
        key: hourly.get(key) or []
        for key in HOURLY_VARIABLES
    }
    t0 = _utc_hour(t1)
    end = t0 + pd.Timedelta(hours=12)
    records = []
    for index, time_value in enumerate(times):
        timestamp = _utc_hour(time_value)
        if timestamp < t0 or timestamp > end:
            continue

        def value(key):
            column = values[key]
            if index >= len(column) or column[index] is None:
                return None
            return float(column[index])

        wind_speed = value("wind_speed_10m")
        wind_gust = value("wind_gusts_10m")
        records.append({
            "hour": int((timestamp - t0).total_seconds() // 3600),
            "valid_time": timestamp.isoformat(),
            "temp_c": value("temperature_2m"),
            "rh": value("relative_humidity_2m"),
            "wind_speed_kmh": wind_speed,
            "max_wind_speed_kmh": wind_gust if wind_gust is not None else wind_speed,
            "wind_dir": value("wind_direction_10m"),
            "source": source,
        })
    return sorted(records, key=lambda record: record["hour"])


def _provider_urls(t0: pd.Timestamp) -> list[tuple[str, str]]:
    now = pd.Timestamp(datetime.now(timezone.utc)).floor("h")
    if t0 >= now - pd.Timedelta(days=5):
        return [
            (FORECAST_URL, "Open-Meteo forecast archive"),
            (ARCHIVE_URL, "Open-Meteo historical reanalysis"),
        ]
    return [
        (ARCHIVE_URL, "Open-Meteo historical reanalysis"),
        (FORECAST_URL, "Open-Meteo forecast archive"),
    ]


def ensure_open_meteo_forecast(event, t1, out_dir: Path) -> list[dict]:
    """Fetch a region-centred +12h weather series and cache ``forecast.json``."""
    forecast_path = Path(out_dir) / "forecast.json"
    if forecast_path.exists():
        try:
            cached = json.loads(forecast_path.read_text(encoding="utf-8"))
            if cached:
                return cached
        except (OSError, ValueError):
            pass

    from pipeline.event_config import get_event_config

    config = get_event_config(event)
    min_lon, min_lat, max_lon, max_lat = config.view_bbox or config.bbox
    latitude = (min_lat + max_lat) / 2
    longitude = (min_lon + max_lon) / 2
    t0 = _utc_hour(t1)
    end = t0 + pd.Timedelta(hours=12)
    params = {
        "latitude": round(latitude, 5),
        "longitude": round(longitude, 5),
        "start_date": t0.date().isoformat(),
        "end_date": end.date().isoformat(),
        "hourly": ",".join(HOURLY_VARIABLES),
        "timezone": "UTC",
        "wind_speed_unit": "kmh",
    }

    for url, source in _provider_urls(t0):
        try:
            response = requests.get(url, params=params, timeout=20)
            response.raise_for_status()
            records = _open_meteo_records(response.json(), t0, source)
            if not records:
                continue
            forecast_path.parent.mkdir(parents=True, exist_ok=True)
            forecast_path.write_text(
                json.dumps(records, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            log.info(
                "[weather] cached %d Open-Meteo hours for event %d at %.4f, %.4f",
                len(records), event.id, latitude, longitude,
            )
            return records
        except (requests.RequestException, ValueError, OSError) as exc:
            log.warning("[weather] Open-Meteo fallback failed via %s: %s", url, exc)
    return []
