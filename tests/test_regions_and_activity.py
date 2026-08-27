from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from api.events import (  # noqa: E402
    _filter_geojson_bbox,
    _filter_thermal_features,
    _india_thermal_overview,
)
from pipeline.regions import (  # noqa: E402
    FOREST_FOCUS,
    INDUSTRIAL_FOCUS,
    REGIONS,
    get_active_region,
    get_auto_prepare_region_ids,
)


class RegionAndActivityTests(unittest.TestCase):
    def test_india_overview_merges_regions_and_adds_event_context(self):
        class Event:
            id = 2
            name = "Vijayanagar"
            year = 2026

        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [76.67, 15.17]},
            "properties": {
                "source_class": "industrial",
                "raw_observation_count": 4,
            },
        }
        serialized = {
            "region_id": "vijayanagar",
            "state": "Karnataka",
            "monitoring_focus": "industrial",
            "data_ready": True,
            "bbox": [76.58, 15.10, 76.76, 15.24],
        }
        with patch("api.events.is_public_event", create=True, return_value=True), patch(
            "api.events._serialize", return_value=serialized
        ), patch("pathlib.Path.exists", return_value=True), patch(
            "pathlib.Path.read_text",
            return_value='{"type":"FeatureCollection","features":[' + str(feature).replace("'", '"').replace('True', 'true') + ']}'
        ):
            overview = _india_thermal_overview([Event()])

        self.assertEqual(overview["metadata"]["region_count"], 1)
        self.assertEqual(overview["metadata"]["source_count"], 1)
        self.assertEqual(overview["metadata"]["observation_count"], 4)
        self.assertEqual(overview["features"][0]["properties"]["event_id"], 2)

    def test_vijayanagar_region_contract(self):
        region = REGIONS["vijayanagar"]
        self.assertEqual(region.center, (15.172, 76.677))
        self.assertEqual(region.bbox, (76.58, 15.10, 76.76, 15.24))
        self.assertEqual(get_active_region(), region)

    def test_catalog_contains_industrial_and_forest_regions_in_india(self):
        self.assertEqual(len(REGIONS), 10)
        self.assertTrue(all(region.country_code == "in" for region in REGIONS.values()))
        self.assertEqual(
            sum(region.monitoring_focus == INDUSTRIAL_FOCUS for region in REGIONS.values()),
            6,
        )
        self.assertEqual(
            sum(region.monitoring_focus == FOREST_FOCUS for region in REGIONS.values()),
            4,
        )
        self.assertEqual(len({region.event_id for region in REGIONS.values()}), len(REGIONS))

    def test_forest_regions_use_standard_fire_season_products(self):
        forest = REGIONS["gadchiroli_tadoba"]
        self.assertEqual(forest.history_start, "2026-03-20")
        self.assertEqual(forest.history_end, "2026-04-18")
        self.assertEqual(
            forest.firms_history_sources,
            ("VIIRS_NOAA20_SP", "VIIRS_SNPP_SP"),
        )

    def test_forest_dashboard_extents_are_tighter_than_collection_extents(self):
        for region in REGIONS.values():
            if region.monitoring_focus != FOREST_FOCUS:
                continue
            self.assertIsNotNone(region.view_bbox)
            data_width = region.bbox[2] - region.bbox[0]
            data_height = region.bbox[3] - region.bbox[1]
            view_width = region.view_bbox[2] - region.view_bbox[0]
            view_height = region.view_bbox[3] - region.view_bbox[1]
            self.assertLess(view_width, data_width)
            self.assertLess(view_height, data_height)

    def test_auto_prepare_accepts_all_or_a_subset(self):
        with patch.dict("os.environ", {"AUTO_PREPARE_REGIONS": "all"}):
            self.assertEqual(get_auto_prepare_region_ids(), set(REGIONS))
        with patch.dict(
            "os.environ",
            {"AUTO_PREPARE_REGIONS": "vijayanagar,gadchiroli_tadoba"},
        ):
            self.assertEqual(
                get_auto_prepare_region_ids(),
                {"vijayanagar", "gadchiroli_tadoba"},
            )

    def test_five_day_activity_is_cumulative_and_inclusive(self):
        payload = {
            "type": "FeatureCollection",
            "features": [
                {"properties": {"observed_at": "2026-08-22T10:00:00Z"}},
                {"properties": {"observed_at": "2026-08-23T10:00:00Z"}},
                {"properties": {"observed_at": "2026-08-27T10:00:00Z"}},
                {"properties": {"observed_at": "2026-08-27T11:00:00Z"}},
            ],
        }
        selected, start = _filter_thermal_features(
            payload,
            5,
            datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(start.isoformat(), "2026-08-23T10:00:00+00:00")
        self.assertEqual(len(selected), 2)

    def test_dashboard_bbox_removes_out_of_region_points(self):
        payload = {
            "type": "FeatureCollection",
            "features": [
                {"geometry": {"type": "Point", "coordinates": [80.0, 20.1]}},
                {"geometry": {"type": "Point", "coordinates": [79.2, 19.6]}},
            ],
        }
        filtered = _filter_geojson_bbox(payload, (79.75, 19.85, 80.25, 20.45))
        self.assertEqual(len(filtered["features"]), 1)


if __name__ == "__main__":
    unittest.main()
