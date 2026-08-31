from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.spatial.spatial_helpers import (  # noqa: E402
    population_counts,
    unavailable_population,
)
from api.ts_data_routes import _population_for_observation  # noqa: E402


class PopulationExposureTests(unittest.TestCase):
    def test_unavailable_population_uses_null_not_zero(self):
        result = unavailable_population("thermal_monitoring", "Missing raster")

        self.assertFalse(result["data_available"])
        self.assertEqual(result["exposure_mode"], "proximity_buffers")
        self.assertIsNone(result["within_1km"])
        self.assertIsNone(result["within_5km"])

    def test_worldpop_raster_builds_cumulative_thermal_buffers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "population.tif"
            values = np.ones((1, 120, 120), dtype="float32")
            with rasterio.open(
                path,
                "w",
                driver="GTiff",
                height=120,
                width=120,
                count=1,
                dtype="float32",
                crs="EPSG:4326",
                transform=from_origin(0, 0.12, 0.001, 0.001),
                nodata=-9999,
            ) as dataset:
                dataset.write(values)

            result = population_counts(
                path,
                None,
                {},
                2026,
                analysis_mode="thermal_monitoring",
                hotspot_geom=Point(0.06, 0.06),
            )

        self.assertTrue(result["data_available"])
        self.assertEqual(result["exposure_mode"], "proximity_buffers")
        self.assertGreater(result["within_1km"], 0)
        self.assertGreater(result["within_3km"], result["within_1km"])
        self.assertGreater(result["within_5km"], result["within_3km"])
        self.assertEqual(result["source"]["provider"], "WorldPop")

    def test_observation_falls_back_to_local_worldpop_grid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raster_path = root / "ind_pop_2026_CN_1km_R2025A_UA_v1.tif"
            values = np.ones((1, 120, 120), dtype="float32")
            with rasterio.open(
                raster_path,
                "w",
                driver="GTiff",
                height=120,
                width=120,
                count=1,
                dtype="float32",
                crs="EPSG:4326",
                transform=from_origin(86.0, 23.9, 0.001, 0.001),
                nodata=-9999,
            ) as dataset:
                dataset.write(values)

            hotspot_dir = root / "hotspot"
            hotspot_dir.mkdir()
            (hotspot_dir / "hotspots.geojson").write_text(
                '{"type":"FeatureCollection","features":[{"type":"Feature",'
                '"properties":{},"geometry":{"type":"Point","coordinates":[86.06,23.84]}}]}',
                encoding="utf-8",
            )
            output_path = root / "spatial_analysis" / "ML" / "population.json"
            event = SimpleNamespace(id=4, year=2026, name="Dhanbad")
            timestep = SimpleNamespace(slot_time="2026-08-30T07:54:00")
            with patch.dict("os.environ", {"WORLDPOP_RASTER_PATH": str(raster_path)}), patch(
                "api.ts_data_routes._hotspot_dir", return_value=hotspot_dir
            ):
                result = _population_for_observation(event, timestep, output_path)

        self.assertTrue(result["data_available"])
        self.assertGreater(result["within_5km"], result["within_1km"])
        self.assertEqual(result["source"]["resolution_m"], 1_000)


if __name__ == "__main__":
    unittest.main()
