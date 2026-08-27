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

from pipeline.thermal.classification import (  # noqa: E402
    CLASSIFICATION_METHOD,
    classify_persistent_sources,
    classify_source,
    ensure_source_classification,
)


def _source(**overrides):
    base = {
        "cluster_id": "cluster_001",
        "latitude": 15.17,
        "longitude": 76.67,
        "inside_industrial_polygon": False,
        "near_industrial_facility": False,
        "distance_to_nearest_industry_m": None,
        "landcover_group": "unknown",
        "landcover_class": "unknown",
        "persistence_level": "LOW",
        "unique_active_days": 2,
        "night_ratio": 0.0,
        "detection_count": 2,
        "raw_observation_count": 2,
        "first_seen": pd.Timestamp("2026-08-01T10:00:00Z"),
        "last_seen": pd.Timestamp("2026-08-05T10:00:00Z"),
    }
    base.update(overrides)
    return base


class ThermalClassificationTests(unittest.TestCase):
    def test_industrial_class_requires_context_and_has_evidence(self):
        result = classify_source(_source(
            inside_industrial_polygon=True,
            near_industrial_facility=True,
            distance_to_nearest_industry_m=80.0,
            persistence_level="HIGH",
            unique_active_days=14,
            night_ratio=0.8,
            landcover_group="built_up",
        ))

        self.assertEqual(result["source_class"], "industrial")
        self.assertEqual(result["source_subtype"], "persistent_industrial_source")
        self.assertGreaterEqual(result["classification_confidence"], 0.8)
        self.assertTrue(any("industrial land-use" in item for item in result["classification_evidence"]))
        self.assertEqual(result["classification_method"], CLASSIFICATION_METHOD)

    def test_natural_class_uses_landcover_and_industrial_distance(self):
        result = classify_source(_source(
            distance_to_nearest_industry_m=3000.0,
            landcover_group="vegetation",
            landcover_class="tree_cover",
        ))

        self.assertEqual(result["source_class"], "natural")
        self.assertTrue(any("tree cover" in item for item in result["classification_evidence"]))

    def test_weak_or_conflicting_evidence_stays_unknown(self):
        result = classify_source(_source(
            distance_to_nearest_industry_m=2500.0,
            landcover_group="bare",
            landcover_class="bare_sparse_vegetation",
            night_ratio=0.7,
        ))

        self.assertEqual(result["source_class"], "unknown")
        self.assertLessEqual(result["classification_confidence"], 0.65)

    def test_classification_writes_separate_gis_artifacts(self):
        clusters = pd.DataFrame([_source(
            inside_industrial_polygon=True,
            near_industrial_facility=True,
            distance_to_nearest_industry_m=50.0,
            persistence_level="HIGH",
            unique_active_days=12,
        )])
        with tempfile.TemporaryDirectory() as temporary:
            processed = Path(temporary) / "data_processed"
            thermal = processed / "thermal"
            thermal.mkdir(parents=True)
            clusters.to_parquet(thermal / "persistent_clusters.parquet", index=False)
            metadata = ensure_source_classification(
                SimpleNamespace(id=2),
                SimpleNamespace(data_processed_dir=processed),
            )
            payload = json.loads((thermal / "classified_sources.geojson").read_text())

            self.assertTrue((thermal / "classified_sources.parquet").exists())
            self.assertEqual(metadata["class_counts"], {"industrial": 1})
            self.assertFalse(metadata["is_trained_model"])
            self.assertEqual(payload["features"][0]["properties"]["source_class"], "industrial")
            self.assertIsInstance(
                payload["features"][0]["properties"]["classification_evidence"], list,
            )

    def test_dataframe_classification_preserves_cluster_metrics(self):
        classified = classify_persistent_sources(pd.DataFrame([_source()]))
        self.assertEqual(classified.iloc[0]["cluster_id"], "cluster_001")
        self.assertIn("source_class", classified.columns)


if __name__ == "__main__":
    unittest.main()
