from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.training_data.builder import _write_parquet  # noqa: E402
from pipeline.training_data.features import feature_schema, generate_features  # noqa: E402
from pipeline.training_data.manifests import sha256_file  # noqa: E402
from pipeline.training_data.regions import TRAINING_REGIONS  # noqa: E402
from pipeline.training_data.schemas import (  # noqa: E402
    DatasetSplit,
    LabelState,
    TrainingClass,
    validate_label_record,
)
from pipeline.training_data.splits import assign_grouped_splits  # noqa: E402


def _observations() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "observation_id": "a",
            "region_id": "region-a",
            "origin_event_id": 10,
            "geographic_group_id": "geo-a",
            "temporal_group_id": "geo-a-2020",
            "observed_at": pd.Timestamp("2020-01-01T00:00:00Z"),
            "latitude": 10.0,
            "longitude": 20.0,
            "frp": 2.0,
            "bright_ti4": 310.0,
            "bright_ti5": 290.0,
            "satellite": "N",
            "instrument": "VIIRS",
            "confidence": "n",
            "daynight": "N",
        },
        {
            "observation_id": "b",
            "region_id": "region-a",
            "origin_event_id": 10,
            "geographic_group_id": "geo-a",
            "temporal_group_id": "geo-a-2020",
            "observed_at": pd.Timestamp("2020-01-01T00:00:00Z"),
            "latitude": 10.0001,
            "longitude": 20.0001,
            "frp": 4.0,
            "bright_ti4": 312.0,
            "bright_ti5": 291.0,
            "satellite": "N",
            "instrument": "VIIRS",
            "confidence": "n",
            "daynight": "N",
        },
        {
            "observation_id": "c",
            "region_id": "region-a",
            "origin_event_id": 10,
            "geographic_group_id": "geo-a",
            "temporal_group_id": "geo-a-2020",
            "observed_at": pd.Timestamp("2020-01-02T00:00:00Z"),
            "latitude": 10.0001,
            "longitude": 20.0001,
            "frp": 6.0,
            "bright_ti4": 315.0,
            "bright_ti5": 292.0,
            "satellite": "N",
            "instrument": "VIIRS",
            "confidence": "n",
            "daynight": "D",
        },
    ])


class TrainingDataTests(unittest.TestCase):
    def test_class_contract_has_confirmed_other_and_no_unknown_class(self):
        values = {item.value for item in TrainingClass}
        self.assertIn("other_confirmed", values)
        self.assertNotIn("other_or_unknown", values)

    def test_ambiguous_and_tier_c_records_cannot_enter_supervision(self):
        with self.assertRaises(ValueError):
            validate_label_record({
                "label_state": LabelState.AMBIGUOUS.value,
                "class_label": TrainingClass.INDUSTRIAL_THERMAL.value,
                "evidence_tier": None,
            })
        with self.assertRaises(ValueError):
            validate_label_record({
                "label_state": LabelState.LABELED.value,
                "class_label": TrainingClass.INDUSTRIAL_THERMAL.value,
                "evidence_tier": "C",
                "evidence_source": "candidate",
                "evidence_source_url": "https://example.invalid",
                "evidence_record_id": "candidate-1",
                "evidence_method": "heuristic",
            })

    def test_temporal_features_are_strictly_causal(self):
        featured = generate_features(_observations()).set_index("observation_id")
        self.assertEqual(featured.at["a", "prior_detection_count_7d"], 0)
        self.assertEqual(featured.at["b", "prior_detection_count_7d"], 0)
        self.assertEqual(featured.at["c", "prior_detection_count_7d"], 2)
        self.assertAlmostEqual(featured.at["c", "prior_frp_mean_30d"], 3.0)

    def test_chakan_is_hard_excluded(self):
        chakan = next(region for region in TRAINING_REGIONS if region.region_id == "chakan_2024_demo")
        self.assertTrue(chakan.exclude_from_model_fitting)
        self.assertEqual(chakan.fixed_split, DatasetSplit.EXCLUDED)

    def test_split_assignment_groups_regions_and_events(self):
        base = TRAINING_REGIONS[0]
        train = replace(
            base, region_id="train-region", geographic_group_id="train-geo",
            temporal_group_id="train-time", source_event_id=10, fixed_split=DatasetSplit.TRAIN,
        )
        validation = replace(
            base, region_id="validation-region", geographic_group_id="validation-geo",
            temporal_group_id="validation-time", source_event_id=20,
            fixed_split=DatasetSplit.VALIDATION,
        )
        rows = pd.DataFrame([
            {
                "observation_id": "train", "region_id": train.region_id,
                "origin_event_id": 10, "geographic_group_id": "train-geo",
                "temporal_group_id": "train-time", "observed_at": pd.Timestamp("2020-01-01T00:00Z"),
                "label_state": "labeled", "class_label": "wildfire_or_vegetation", "evidence_tier": "A",
            },
            {
                "observation_id": "validation", "region_id": validation.region_id,
                "origin_event_id": 20, "geographic_group_id": "validation-geo",
                "temporal_group_id": "validation-time", "observed_at": pd.Timestamp("2021-01-01T00:00Z"),
                "label_state": "labeled", "class_label": "other_confirmed", "evidence_tier": "B",
            },
        ])
        assigned, checks = assign_grouped_splits(rows, (train, validation))
        self.assertEqual(set(assigned["split"]), {"train", "validation"})
        self.assertTrue(all(not value for value in checks.values()))

    def test_event_overlap_is_rejected(self):
        base = TRAINING_REGIONS[0]
        train = replace(base, region_id="a", source_event_id=99, fixed_split=DatasetSplit.TRAIN)
        validation = replace(
            base, region_id="b", geographic_group_id="b", temporal_group_id="b-2021",
            source_event_id=99, fixed_split=DatasetSplit.VALIDATION,
        )
        rows = pd.DataFrame([
            {"observation_id": "a", "region_id": "a", "origin_event_id": 99,
             "geographic_group_id": train.geographic_group_id, "temporal_group_id": train.temporal_group_id,
             "observed_at": pd.Timestamp("2020-01-01T00:00Z"), "label_state": "labeled",
             "class_label": "wildfire_or_vegetation", "evidence_tier": "A"},
            {"observation_id": "b", "region_id": "b", "origin_event_id": 99,
             "geographic_group_id": "b", "temporal_group_id": "b-2021",
             "observed_at": pd.Timestamp("2021-01-01T00:00Z"), "label_state": "labeled",
             "class_label": "other_confirmed", "evidence_tier": "A"},
        ])
        with self.assertRaisesRegex(ValueError, "split leakage"):
            assign_grouped_splits(rows, (train, validation))

    def test_model_schema_excludes_location_and_firms_type(self):
        excluded = feature_schema()["excluded_identifiers"]
        self.assertIn("latitude", excluded)
        self.assertIn("longitude", excluded)
        self.assertIn("type", excluded)

    def test_parquet_writer_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rows.parquet"
            frame = pd.DataFrame({"observation_id": ["b", "a"], "value": [2, 1]})
            _write_parquet(frame, path)
            first = sha256_file(path)
            _write_parquet(frame.iloc[::-1], path)
            self.assertEqual(first, sha256_file(path))


if __name__ == "__main__":
    unittest.main()
