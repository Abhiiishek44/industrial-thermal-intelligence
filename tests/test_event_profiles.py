from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.event_config import (  # noqa: E402
    THERMAL_MONITORING_MODE,
    WILDFIRE_MODE,
    get_event_config,
    uses_wildfire_model,
)


FORT = SimpleNamespace(id=1, name="Fort McMurray Wildfire 2016", year=2016)
CHAKAN = SimpleNamespace(id=2, name="Chakan Industrial Thermal Monitoring", year=2024)


class EventProfileTests(unittest.TestCase):
    def test_fort_profile_keeps_wildfire_model(self):
        self.assertEqual(get_event_config(FORT).analysis_mode, WILDFIRE_MODE)
        self.assertTrue(uses_wildfire_model(FORT))

    def test_chakan_profile_has_independent_history_window(self):
        config = get_event_config(CHAKAN)

        self.assertEqual(config.analysis_mode, THERMAL_MONITORING_MODE)
        self.assertFalse(uses_wildfire_model(CHAKAN))
        self.assertEqual(config.start_date, "2024-01-01")
        self.assertEqual(config.end_date, "2024-01-30")
        self.assertEqual(
            tuple(value.isoformat() for value in config.thermal_history_dates),
            ("2024-01-01", "2024-12-31"),
        )
        self.assertEqual(config.industrial_boundary_provider, "midc_arcgis")
        self.assertEqual(config.industrial_context_provider, "osm_overpass")
        self.assertEqual(config.landcover_provider, "esa_worldcover_2021")

    def test_fort_profile_has_no_thermal_context_providers(self):
        config = get_event_config(FORT)

        self.assertEqual(config.industrial_boundary_provider, "none")
        self.assertEqual(config.industrial_context_provider, "none")
        self.assertEqual(config.landcover_provider, "none")

    def test_chakan_runtime_does_not_load_wildfire_predictor(self):
        from pipeline.check import builder

        assets = {"study": object()}
        with (
            patch.object(builder, "_get_event_assets", return_value=assets),
            patch.object(
                builder,
                "_load_predictor",
                side_effect=AssertionError("wildfire predictor must not be loaded"),
            ),
            patch.object(
                builder,
                "_load_threshold",
                side_effect=AssertionError("wildfire threshold must not be loaded"),
            ),
        ):
            runtime_assets, predictor, threshold = builder._get_build_runtime(CHAKAN)

        self.assertIs(runtime_assets, assets)
        self.assertIsNone(predictor)
        self.assertIsNone(threshold)


if __name__ == "__main__":
    unittest.main()
