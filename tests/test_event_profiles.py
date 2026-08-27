from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.event_config import (  # noqa: E402
    INDIA_EVENT_CONFIGS,
    THERMAL_MONITORING_MODE,
    WILDFIRE_MODE,
    get_event_config,
    uses_wildfire_model,
)


FORT = SimpleNamespace(id=1, name="Fort McMurray Wildfire 2016", year=2016)
VIJAYANAGAR = SimpleNamespace(
    id=2,
    name="Vijayanagar / Toranagallu Industrial Region Thermal Monitoring",
    year=2026,
)


class EventProfileTests(unittest.TestCase):
    def test_fort_profile_keeps_wildfire_model(self):
        self.assertEqual(get_event_config(FORT).analysis_mode, WILDFIRE_MODE)
        self.assertTrue(uses_wildfire_model(FORT))

    def test_vijayanagar_profile_has_independent_history_window(self):
        config = get_event_config(VIJAYANAGAR)

        self.assertEqual(config.analysis_mode, THERMAL_MONITORING_MODE)
        self.assertFalse(uses_wildfire_model(VIJAYANAGAR))
        self.assertEqual(config.region_id, "vijayanagar")
        self.assertEqual(config.bbox, (76.58, 15.10, 76.76, 15.24))
        self.assertEqual(config.start_date, "2026-07-29")
        self.assertEqual(config.end_date, "2026-08-27")
        self.assertEqual(
            tuple(value.isoformat() for value in config.thermal_history_dates),
            ("2026-07-29", "2026-08-27"),
        )
        self.assertEqual(config.industrial_boundary_provider, "none")
        self.assertEqual(config.industrial_context_provider, "osm_overpass")
        self.assertEqual(config.landcover_provider, "esa_worldcover_2021")
        self.assertEqual(
            config.firms_history_sources,
            ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"),
        )
        self.assertEqual(config.monitoring_focus, "industrial")

    def test_india_catalog_profiles_are_public_thermal_monitoring_events(self):
        self.assertEqual(len(INDIA_EVENT_CONFIGS), 10)
        self.assertTrue(all(config.public for config in INDIA_EVENT_CONFIGS))
        self.assertTrue(all(config.country_code == "in" for config in INDIA_EVENT_CONFIGS))
        self.assertTrue(
            all(config.analysis_mode == THERMAL_MONITORING_MODE for config in INDIA_EVENT_CONFIGS)
        )
        self.assertEqual(
            sum(config.monitoring_focus == "forest" for config in INDIA_EVENT_CONFIGS),
            4,
        )

    def test_forest_profile_uses_landcover_without_wildfire_predictor(self):
        forest = SimpleNamespace(
            id=8,
            name="Gadchiroli Forest Landscape Monitoring",
            year=2026,
        )
        config = get_event_config(forest)

        self.assertEqual(config.monitoring_focus, "forest")
        self.assertEqual(config.roads_provider, "none")
        self.assertEqual(config.landcover_provider, "esa_worldcover_2021")
        self.assertFalse(uses_wildfire_model(forest))

    def test_fort_profile_has_no_thermal_context_providers(self):
        config = get_event_config(FORT)

        self.assertEqual(config.industrial_boundary_provider, "none")
        self.assertEqual(config.industrial_context_provider, "none")
        self.assertEqual(config.landcover_provider, "none")

    def test_vijayanagar_runtime_does_not_load_wildfire_predictor(self):
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
            runtime_assets, predictor, threshold = builder._get_build_runtime(VIJAYANAGAR)

        self.assertIs(runtime_assets, assets)
        self.assertIsNone(predictor)
        self.assertIsNone(threshold)


if __name__ == "__main__":
    unittest.main()
