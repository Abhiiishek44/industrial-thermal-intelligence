from __future__ import annotations

import sys
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from db.models import FireAlert, FireDetection, MonitoringFireEvent  # noqa: E402
from services.firms_ingestion_service import (  # noqa: E402
    MODEL_FEATURES,
    FirmsIngestionService,
    build_acquisition_time,
    detection_key,
    event_matches_detection,
    filter_recent_records,
    rolling_cutoff,
    should_create_alert,
    validate_model_schema,
)
from services.live_thermal_inference import live_thermal_inference  # noqa: E402
from pipeline.thermal.history import _get_with_retry, normalize_firms_frames  # noqa: E402

import pandas as pd  # noqa: E402
import requests  # noqa: E402


UTC = timezone.utc


class FirmsTimeTests(unittest.TestCase):
    def test_build_acquisition_time_is_utc_and_zero_pads(self):
        actual = build_acquisition_time("2026-10-06", "215")
        self.assertEqual(actual, datetime(2026, 10, 6, 2, 15, tzinfo=UTC))
        self.assertIs(actual.tzinfo, UTC)

    def test_rolling_cutoff_is_exact_not_calendar_day(self):
        now = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)
        self.assertEqual(
            rolling_cutoff(24, now=now),
            datetime(2026, 10, 5, 6, 0, tzinfo=UTC),
        )

    def test_recent_filter_includes_boundary_and_excludes_older(self):
        now = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)
        rows = [
            {"acquisition_time": now - timedelta(hours=24), "id": "boundary"},
            {"acquisition_time": now - timedelta(hours=24, seconds=1), "id": "old"},
        ]
        self.assertEqual(
            [row["id"] for row in filter_recent_records(rows, 24, now=now)],
            ["boundary"],
        )

    def test_malformed_firms_rows_are_dropped_by_normalizer(self):
        frame = pd.DataFrame([
            {"latitude": 15.1, "longitude": 76.9, "acq_date": "2026-10-06", "acq_time": "0215", "satellite": "N21", "instrument": "VIIRS"},
            {"latitude": "bad", "longitude": 76.9, "acq_date": "2026-10-06", "acq_time": "0215", "satellite": "N21", "instrument": "VIIRS"},
        ])
        normalized, _ = normalize_firms_frames([frame])
        self.assertEqual(len(normalized), 1)

    def test_empty_firms_frames_return_empty_result(self):
        normalized, duplicates = normalize_firms_frames([pd.DataFrame()])
        self.assertTrue(normalized.empty)
        self.assertEqual(duplicates, 0)

    def test_mixed_firms_version_values_are_parquet_safe_strings(self):
        frame = pd.DataFrame([
            {"latitude": 15.1, "longitude": 76.9, "acq_date": "2026-10-06", "acq_time": "0215", "satellite": "N21", "instrument": "VIIRS", "version": 2},
            {"latitude": 15.2, "longitude": 76.8, "acq_date": "2026-10-06", "acq_time": "0315", "satellite": "N20", "instrument": "VIIRS", "version": "2.0NRT"},
        ])
        normalized, _ = normalize_firms_frames([frame])
        self.assertEqual(normalized["version"].dtype.name, "string")
        self.assertEqual(normalized["version"].tolist(), ["2", "2.0NRT"])

    @patch("pipeline.thermal.history.time.sleep", return_value=None)
    def test_temporary_firms_failure_retries_then_succeeds(self, _sleep):
        response = Mock()
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.side_effect = [requests.ConnectionError("down"), response]
        self.assertIs(_get_with_retry(session, "https://example.test"), response)
        self.assertEqual(session.get.call_count, 2)

    @patch("pipeline.thermal.history.time.sleep", return_value=None)
    def test_persistent_firms_failure_is_bounded(self, _sleep):
        session = Mock()
        session.get.side_effect = requests.ConnectionError("down")
        with self.assertRaises(requests.ConnectionError):
            _get_with_retry(session, "https://example.test", attempts=3)
        self.assertEqual(session.get.call_count, 3)


class DeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "latitude": 15.123456,
            "longitude": 76.987654,
            "acq_date": "2026-10-06",
            "acq_time": "0215",
            "satellite": "N21",
            "instrument": "VIIRS",
        }

    def test_detection_key_is_deterministic(self):
        self.assertEqual(detection_key(self.record), detection_key(dict(self.record)))

    def test_sensor_is_part_of_detection_identity(self):
        other = dict(self.record, satellite="N20")
        self.assertNotEqual(detection_key(self.record), detection_key(other))

    def test_database_declares_unique_detection_and_alert_constraints(self):
        detection_constraints = {
            constraint.name for constraint in FireDetection.__table__.constraints
        }
        alert_constraints = {
            constraint.name for constraint in FireAlert.__table__.constraints
        }
        self.assertIn("uq_fire_detections_detection_key", detection_constraints)
        self.assertIn("uq_fire_alerts_deduplication_key", alert_constraints)

    def test_live_records_and_grouped_events_are_region_scoped(self):
        self.assertIn("monitoring_region_id", FireDetection.__table__.columns)
        self.assertIn("monitoring_region_id", MonitoringFireEvent.__table__.columns)


class ModelAndEnrichmentTests(unittest.TestCase):
    def test_saved_model_schema_matches_live_feature_order(self):
        features = validate_model_schema()
        self.assertEqual(len(features), 16)

    def test_worldcover_and_nasa_confidence_remain_distinct(self):
        service = FirmsIngestionService()
        acquired = datetime(2026, 10, 6, 2, 15, tzinfo=UTC)
        row = {
            "latitude": 15.1,
            "longitude": 76.9,
            "satellite": "N21",
            "instrument": "VIIRS",
            "confidence": "h",
            "landcover_class": "built_up",
            "landcover_group": "built_up",
            "inside_industrial_polygon": True,
            "near_industrial_facility": False,
            "bright_ti4": 330.0,
            "frp": 27.4,
        }
        key_record = {**row, "acq_date": "2026-10-06", "acq_time": "0215"}
        detection = service._new_detection(
            detection_key(key_record, acquired), row, acquired,
            acquired + timedelta(minutes=10),
            {"predicted_class": "industrial_anomaly", "model_confidence": 0.82},
            monitoring_region_id=7,
        )
        self.assertEqual(detection.land_cover, "built_up")
        self.assertEqual(detection.firms_confidence, "h")
        self.assertEqual(detection.model_confidence, 0.82)
        self.assertEqual(detection.monitoring_region_id, 7)
        self.assertEqual(detection.landcover_group, "built_up")
        self.assertTrue(detection.inside_industrial_polygon)
        self.assertFalse(detection.near_industrial_facility)

    def test_existing_worldcover_classification_is_reused(self):
        detections = pd.DataFrame([{"latitude": 15.1, "longitude": 76.9}])
        sources = pd.DataFrame([{
            "latitude": 15.1,
            "longitude": 76.9,
            "source_class": "industrial_fire",
            "classification_confidence": 0.91,
            "thermal_footprint_radius_m": 400,
        }])
        actual = FirmsIngestionService._add_existing_classifications(detections, sources)
        self.assertEqual(actual.iloc[0]["source_class"], "industrial_fire")
        self.assertEqual(actual.iloc[0]["classification_confidence"], 0.91)

    def test_live_row_without_mature_baseline_still_gets_model_confidence(self):
        prepared = pd.DataFrame([{name: 0.0 for name in MODEL_FEATURES}])
        strict_result = prepared.copy()
        strict_result["industrial_anomaly_score"] = float("nan")
        strict_result["iforest_raw_score"] = float("nan")
        strict_result["industrial_is_anomaly"] = False
        strict_result["iforest_eligible"] = False
        with (
            patch.object(live_thermal_inference, "_prepare_observations", return_value=prepared),
            patch.object(live_thermal_inference, "_add_model_features", return_value=prepared),
            patch.object(live_thermal_inference, "_score_iforest", return_value=strict_result),
        ):
            scored = FirmsIngestionService()._score_rows(pd.DataFrame([{}]), "test")
        self.assertTrue(scored["model_confidence"].notna().all())
        self.assertIn(scored.iloc[0]["predicted_class"], {
            "industrial_anomaly", "industrial_normal",
        })


class EventAndAlertTests(unittest.TestCase):
    def test_nearby_recent_detection_updates_event(self):
        observed = datetime(2026, 10, 6, 2, 15, tzinfo=UTC)
        event = SimpleNamespace(
            centroid_latitude=15.1, centroid_longitude=76.9,
            last_detected_at=observed - timedelta(hours=2),
        )
        self.assertTrue(event_matches_detection(
            event, latitude=15.11, longitude=76.9, acquisition_time=observed,
            radius_km=3, time_gap_hours=6,
        ))

    def test_old_event_does_not_match_and_alert_threshold_is_inclusive(self):
        observed = datetime(2026, 10, 6, 2, 15, tzinfo=UTC)
        event = SimpleNamespace(
            centroid_latitude=15.1, centroid_longitude=76.9,
            last_detected_at=observed - timedelta(hours=7),
        )
        self.assertFalse(event_matches_detection(
            event, latitude=15.1, longitude=76.9, acquisition_time=observed,
            radius_km=3, time_gap_hours=6,
        ))
        self.assertTrue(should_create_alert(0.75, 0.75))
        self.assertFalse(should_create_alert(0.74, 0.75))
        self.assertFalse(should_create_alert(None, 0.75))


if __name__ == "__main__":
    unittest.main()
