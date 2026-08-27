from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.check.builder import _load_observation_steps, _playback_slots  # noqa: E402
from pipeline.check.builder_stages import (  # noqa: E402
    _export_thermal_observations,
    _run_thermal_monitoring_stage,
)


class ThermalDashboardTests(unittest.TestCase):
    def test_monitoring_uses_only_observation_timestamps(self):
        steps = [
            pd.Timestamp("2024-01-02T08:24:00Z"),
            pd.Timestamp("2024-01-05T19:58:00Z"),
        ]
        with patch("pipeline.event_config.uses_wildfire_model", return_value=False):
            slots, prune_missing = _playback_slots(SimpleNamespace(), steps)

        self.assertEqual(slots, steps)
        self.assertTrue(prune_missing)

    def test_history_steps_are_filtered_to_replay_window(self):
        observations = pd.DataFrame({
            "observed_at": pd.to_datetime([
                "2024-01-02T08:24:00Z",
                "2024-01-30T23:59:00Z",
                "2024-01-31T00:00:00Z",
            ], utc=True),
        })
        event = SimpleNamespace(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 30),
        )
        with tempfile.TemporaryDirectory() as temporary:
            processed = Path(temporary)
            thermal = processed / "thermal"
            thermal.mkdir()
            (thermal / "firms_enriched.parquet").touch()
            study = SimpleNamespace(data_processed_dir=processed)
            with (
                patch("pipeline.event_config.uses_wildfire_model", return_value=False),
                patch("pipeline.check.builder.pd.read_parquet", return_value=observations),
            ):
                steps = _load_observation_steps(event, study)

        self.assertEqual(
            steps,
            [
                pd.Timestamp("2024-01-02T08:24:00Z"),
                pd.Timestamp("2024-01-30T23:59:00Z"),
            ],
        )

    def test_export_contains_enriched_dashboard_properties(self):
        observations = pd.DataFrame([{
            "observed_at": pd.Timestamp("2024-01-14T08:49:00Z"),
            "latitude": 18.78,
            "longitude": 73.82,
            "satellite": "N20",
            "confidence": "n",
            "frp": 4.5,
            "inside_midc": True,
            "near_industrial_facility": True,
            "nearest_industry_name": "Example Works",
            "landcover_group": "built_up",
        }])
        with tempfile.TemporaryDirectory() as temporary:
            processed = Path(temporary) / "data_processed"
            thermal = processed / "thermal"
            thermal.mkdir(parents=True)
            (thermal / "firms_enriched.parquet").touch()
            out_path = Path(temporary) / "hotspots.geojson"
            study = SimpleNamespace(data_processed_dir=processed)
            with patch(
                "pipeline.check.builder_stages.pd.read_parquet",
                return_value=observations,
            ):
                selected = _export_thermal_observations(
                    study,
                    pd.Timestamp("2024-01-14T08:49:00"),
                    out_path,
                )

            payload = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertEqual(len(selected), 1)
        self.assertEqual(len(payload["features"]), 1)
        properties = payload["features"][0]["properties"]
        self.assertEqual(properties["nearest_industry_name"], "Example Works")
        self.assertEqual(properties["landcover_group"], "built_up")
        self.assertTrue(properties["inside_midc"])

    def test_monitoring_stage_writes_thermal_dashboard_context(self):
        observations = pd.DataFrame([{
            "observed_at": pd.Timestamp("2024-01-14T08:49:00Z"),
            "latitude": 18.78,
            "longitude": 73.82,
            "satellite": "N20",
            "confidence": "n",
            "frp": 4.5,
            "bright_ti4": 325.0,
            "inside_midc": True,
            "inside_industrial_polygon": True,
            "near_industrial_facility": True,
            "nearest_industry_name": "Example Works",
            "landcover_group": "built_up",
        }])
        event = SimpleNamespace(id=2, year=2024)
        timestep = SimpleNamespace(
            id=7,
            slot_time=pd.Timestamp("2024-01-14T08:49:00Z"),
            nearest_t1=pd.Timestamp("2024-01-14T08:49:00Z"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            processed = root / "data_processed"
            thermal = processed / "thermal"
            thermal.mkdir(parents=True)
            (thermal / "firms_enriched.parquet").touch()
            study = SimpleNamespace(data_processed_dir=processed)
            statuses = []
            with (
                patch("pipeline.check.builder_stages.pd.read_parquet", return_value=observations),
                patch("pipeline.check.builder_stages._timestep_dir", return_value=root / "timestep"),
                patch("pipeline.check.builder_slots._write_status", side_effect=lambda path, status: statuses.append(status)),
            ):
                _run_thermal_monitoring_stage(event, timestep, study)

            context_path = root / "timestep" / "prediction" / "ML" / "fire_context.json"
            context = json.loads(context_path.read_text(encoding="utf-8"))

        self.assertEqual(statuses[-1], "done")
        self.assertEqual(context["analysis_mode"], "thermal_monitoring")
        self.assertEqual(context["thermal"]["detection_count"], 1)
        self.assertEqual(context["thermal"]["inside_midc_count"], 1)
        self.assertEqual(context["thermal"]["landcover_group_counts"], {"built_up": 1})


if __name__ == "__main__":
    unittest.main()
