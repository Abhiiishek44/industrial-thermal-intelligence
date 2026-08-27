from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.weather.open_meteo_fallback import _open_meteo_records  # noqa: E402


class WeatherFallbackTests(unittest.TestCase):
    def test_open_meteo_response_becomes_thirteen_dashboard_hours(self):
        times = [f"2026-04-18T{hour:02d}:00" for hour in range(24)]
        payload = {
            "hourly": {
                "time": times,
                "temperature_2m": [20 + hour / 10 for hour in range(24)],
                "relative_humidity_2m": [40 + hour for hour in range(24)],
                "wind_speed_10m": [10 + hour for hour in range(24)],
                "wind_direction_10m": [90] * 24,
                "wind_gusts_10m": [20 + hour for hour in range(24)],
            }
        }
        records = _open_meteo_records(
            payload,
            "2026-04-18T02:38:00+00:00",
            "test provider",
        )

        self.assertEqual(len(records), 13)
        self.assertEqual(records[0]["hour"], 0)
        self.assertEqual(records[-1]["hour"], 12)
        self.assertEqual(records[0]["wind_speed_kmh"], 12.0)
        self.assertEqual(records[0]["max_wind_speed_kmh"], 22.0)
        self.assertEqual(records[0]["source"], "test provider")


if __name__ == "__main__":
    unittest.main()
