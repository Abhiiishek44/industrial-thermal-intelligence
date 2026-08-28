from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.thermal.refresh import get_refresh_settings, refresh_thermal_event  # noqa: E402


class ThermalRefreshTests(unittest.TestCase):
    def test_refresh_settings_default_to_four_hours(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = get_refresh_settings()

        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["interval_hours"], 4.0)
        self.assertEqual(settings["lookback_days"], 2)

    def test_successful_refresh_advances_event_and_rebuilds_timeline(self):
        event = SimpleNamespace(
            id=2,
            year=2026,
            end_date=date(2026, 8, 27),
        )
        study = SimpleNamespace(project_dir=Path("/unused"))
        history = {
            "data_available": True,
            "observation_count": 3,
            "last_observed_at": "2026-08-28T04:15:00+00:00",
        }

        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.dict(os.environ, {
                    "THERMAL_REFRESH_INTERVAL_HOURS": "4",
                    "THERMAL_LIVE_LOOKBACK_DAYS": "2",
                }),
                patch(
                    "pipeline.thermal.refresh._status_path",
                    return_value=Path(temporary) / "refresh_metadata.json",
                ),
                patch("pipeline.env._make_study", return_value=study),
                patch("pipeline.env._create_event_timesteps") as create_timesteps,
                patch("pipeline.thermal.load_history_metadata", return_value={
                    "observation_count": 2,
                    "last_observed_at": "2026-08-27T20:00:00+00:00",
                }),
                patch("pipeline.thermal.collect_latest_firms", return_value={
                    "successful_source_count": 2,
                    "record_count": 3,
                    "errors": [],
                }),
                patch("pipeline.thermal.normalize_firms_history", return_value=history),
                patch("pipeline.thermal.ensure_thermal_context"),
                patch("pipeline.thermal.ensure_persistence_analysis"),
                patch("pipeline.thermal.ensure_source_classification"),
                patch("db.connection.db.session.commit") as commit,
            ):
                status = refresh_thermal_event(event)

        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(status["new_observation_count"], 1)
        self.assertEqual(event.end_date, date(2026, 8, 28))
        commit.assert_called_once()
        create_timesteps.assert_called_once_with(event)


if __name__ == "__main__":
    unittest.main()
