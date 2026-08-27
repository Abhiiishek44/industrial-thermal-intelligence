from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point, box


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.thermal.context import (  # noqa: E402
    ThermalContextPaths,
    _worldcover_tile_ids,
    enrich_thermal_history,
)

WGS84_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,'
    '298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
)


class ThermalContextTests(unittest.TestCase):
    def test_worldcover_tiles_are_derived_from_bbox(self):
        self.assertEqual(
            _worldcover_tile_ids((73.73, 18.70, 73.87, 18.81)),
            ["N18E072"],
        )

    def test_enrichment_adds_context_without_classification(self):
        event = SimpleNamespace(id=2, year=2024, name="test thermal")
        config = SimpleNamespace(industrial_near_distance_m=1000.0)

        with tempfile.TemporaryDirectory() as temporary:
            project_dir = Path(temporary)
            study = SimpleNamespace(project_dir=project_dir)
            paths = ThermalContextPaths.from_project_dir(project_dir)
            paths.thermal_dir.mkdir(parents=True)
            history = pd.DataFrame([
                {
                    "latitude": 18.80,
                    "longitude": 73.80,
                    "observed_at": pd.Timestamp("2024-01-01T00:00:00Z"),
                    "acq_date": "2024-01-01",
                    "acq_time": "0000",
                    "satellite": "N20",
                    "instrument": "VIIRS",
                },
                {
                    "latitude": 18.72,
                    "longitude": 73.84,
                    "observed_at": pd.Timestamp("2024-01-02T00:00:00Z"),
                    "acq_date": "2024-01-02",
                    "acq_time": "0000",
                    "satellite": "N20",
                    "instrument": "VIIRS",
                },
            ])
            history.to_parquet(paths.thermal_dir / "firms_history.parquet", index=False)

            paths.industrial_dir.mkdir(parents=True)
            midc = gpd.GeoDataFrame(
                [{"name": "phase", "geometry": box(73.79, 18.79, 73.81, 18.81)}],
                crs=rasterio.crs.CRS.from_wkt(WGS84_WKT),
            )
            areas = gpd.GeoDataFrame(
                [{"name": "area", "geometry": box(73.795, 18.795, 73.805, 18.805)}],
                crs="EPSG:4326",
            )
            facilities = gpd.GeoDataFrame(
                [{"name": "works", "industry_type": "works", "geometry": Point(73.801, 18.801)}],
                crs="EPSG:4326",
            )
            paths.midc_path.write_text(midc.to_json(drop_id=True), encoding="utf-8")
            paths.industrial_areas_path.write_text(areas.to_json(drop_id=True), encoding="utf-8")
            paths.facilities_path.write_text(facilities.to_json(drop_id=True), encoding="utf-8")

            paths.landcover_dir.mkdir(parents=True)
            data = np.full((1, 200, 200), 40, dtype=np.uint8)
            row, column = rasterio.transform.rowcol(
                from_origin(73.70, 18.90, 0.001, 0.001), 73.80, 18.80,
            )
            data[0, row - 8:row + 8, column - 8:column + 8] = 50
            with rasterio.open(
                paths.landcover_path,
                "w",
                driver="GTiff",
                height=200,
                width=200,
                count=1,
                dtype="uint8",
                crs=rasterio.crs.CRS.from_wkt(WGS84_WKT),
                transform=from_origin(73.70, 18.90, 0.001, 0.001),
                nodata=0,
            ) as destination:
                destination.write(data)

            with patch("pipeline.thermal.context.get_event_config", return_value=config):
                metadata = enrich_thermal_history(event, study)

            enriched = pd.read_parquet(paths.enriched_path)
            self.assertEqual(len(enriched), 2)
            self.assertTrue(bool(enriched.iloc[0]["inside_midc"]))
            self.assertTrue(bool(enriched.iloc[0]["inside_industrial_polygon"]))
            self.assertTrue(bool(enriched.iloc[0]["near_industrial_facility"]))
            self.assertEqual(enriched.iloc[0]["landcover_group"], "built_up")
            self.assertEqual(enriched.iloc[1]["landcover_group"], "agricultural")
            self.assertFalse(metadata["classification_available"])
            saved = json.loads(paths.enrichment_metadata_path.read_text())
            self.assertEqual(saved["observation_count"], 2)


if __name__ == "__main__":
    unittest.main()
