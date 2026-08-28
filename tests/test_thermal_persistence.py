from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.thermal.persistence import (  # noqa: E402
    aggregate_multisensor_observations,
    build_classification_candidates,
    build_persistent_sources,
    ensure_persistence_analysis,
)


def _row(timestamp, latitude, longitude, satellite, frp=10.0, daynight="N"):
    return {
        "observed_at": pd.Timestamp(timestamp),
        "latitude": latitude,
        "longitude": longitude,
        "satellite": satellite,
        "instrument": "VIIRS",
        "frp": frp,
        "bright_ti4": 330.0,
        "daynight": daynight,
        "confidence": "n",
        "inside_industrial_polygon": True,
        "near_industrial_facility": True,
        "distance_to_nearest_industry_m": 80.0,
        "nearest_industry_name": "Example Steel Works",
        "nearest_industry_type": "steel",
        "landcover_group": "built_up",
        "landcover_class": "built_up",
    }


class ThermalPersistenceTests(unittest.TestCase):
    def test_cross_sensor_overlap_is_aggregated(self):
        observations = pd.DataFrame([
            _row("2026-08-20T10:00:00Z", 15.1700, 76.6700, "N20", 10.0),
            _row("2026-08-20T10:30:00Z", 15.1710, 76.6700, "N21", 14.0),
        ])

        detections = aggregate_multisensor_observations(observations)

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections.iloc[0]["raw_observation_count"], 2)
        self.assertEqual(detections.iloc[0]["sensor_count"], 2)
        self.assertAlmostEqual(detections.iloc[0]["frp"], 12.0)

    def test_same_sensor_pixels_remain_distinct_detections(self):
        observations = pd.DataFrame([
            _row("2026-08-20T10:00:00Z", 15.1700, 76.6700, "N20"),
            _row("2026-08-20T10:01:00Z", 15.1710, 76.6700, "N20"),
        ])

        detections = aggregate_multisensor_observations(observations)

        self.assertEqual(len(detections), 2)

    def test_persistent_cluster_contains_temporal_metrics(self):
        detections = aggregate_multisensor_observations(pd.DataFrame([
            _row("2026-08-01T10:00:00Z", 15.1700, 76.6700, "N20", 10.0, "D"),
            _row("2026-08-05T20:00:00Z", 15.1705, 76.6700, "N20", 20.0, "N"),
            _row("2026-08-10T20:00:00Z", 15.1702, 76.6703, "N21", 30.0, "N"),
            _row("2026-08-10T20:00:00Z", 15.2200, 76.7400, "N20", 5.0, "N"),
        ]))

        clusters = build_persistent_sources(detections)

        self.assertEqual(len(clusters), 1)
        cluster = clusters.iloc[0]
        self.assertEqual(cluster["detection_count"], 3)
        self.assertEqual(cluster["unique_active_days"], 3)
        self.assertEqual(cluster["persistence_level"], "MEDIUM")
        self.assertAlmostEqual(cluster["night_ratio"], 2 / 3, places=3)
        self.assertAlmostEqual(cluster["mean_frp"], 20.0)
        self.assertAlmostEqual(cluster["frp_peak_ratio"], 1.5)

    def test_classification_candidates_keep_one_day_episode(self):
        detections = aggregate_multisensor_observations(pd.DataFrame([
            _row("2026-08-01T10:00:00Z", 15.1700, 76.6700, "N20", 80.0),
        ]))

        self.assertTrue(build_persistent_sources(detections).empty)
        candidates = build_classification_candidates(detections)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates.iloc[0]["unique_active_days"], 1)

    def test_analysis_writes_separate_derived_artifacts(self):
        observations = pd.DataFrame([
            _row("2026-08-01T10:00:00Z", 15.1700, 76.6700, "N20"),
            _row("2026-08-05T20:00:00Z", 15.1705, 76.6700, "N21"),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            processed = Path(temporary) / "data_processed"
            thermal = processed / "thermal"
            thermal.mkdir(parents=True)
            observations.to_parquet(thermal / "firms_enriched.parquet", index=False)
            metadata = ensure_persistence_analysis(
                SimpleNamespace(id=2),
                SimpleNamespace(data_processed_dir=processed),
            )
            clusters = json.loads((thermal / "persistent_clusters.geojson").read_text())

            self.assertTrue((thermal / "detections_aggregated.parquet").exists())
            self.assertTrue((thermal / "persistent_clusters.parquet").exists())
            self.assertEqual(metadata["raw_observation_count"], 2)
            self.assertEqual(len(clusters["features"]), 1)

    def test_windowed_persistence_does_not_use_future_detections(self):
        detections = aggregate_multisensor_observations(pd.DataFrame([
            _row("2026-08-01T10:00:00Z", 15.1700, 76.6700, "N20"),
            _row("2026-08-05T10:00:00Z", 15.1702, 76.6700, "N20"),
            _row("2026-08-20T10:00:00Z", 15.1701, 76.6701, "N21"),
        ]))
        end = pd.Timestamp("2026-08-05T23:59:59Z")
        window = detections[pd.to_datetime(detections["observed_at"], utc=True) <= end]

        clusters = build_persistent_sources(window)

        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters.iloc[0]["detection_count"], 2)
        self.assertLessEqual(clusters.iloc[0]["last_seen"], end)


if __name__ == "__main__":
    unittest.main()
