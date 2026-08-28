from __future__ import annotations

import json
import os
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

from pipeline.event_config import EventConfig, THERMAL_MONITORING_MODE  # noqa: E402
from pipeline.thermal.history import (  # noqa: E402
    _date_chunks,
    collect_latest_firms,
    collect_firms_history,
    normalize_firms_frames,
    normalize_firms_history,
)


class _Response:
    text = (
        "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,"
        "satellite,instrument,confidence,version,bright_ti5,frp,daynight,type\n"
        "18.77495,73.83606,310.44,0.41,0.37,2024-01-05,2048,"
        "N20,VIIRS,n,2,287.57,1.43,N,0\n"
    )

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self):
        self.urls = []

    def get(self, url, timeout):
        self.urls.append((url, timeout))
        return _Response()


class ThermalHistoryTests(unittest.TestCase):
    def test_chunks_cover_window_without_overlap(self):
        chunks = list(_date_chunks(date(2024, 1, 1), date(2024, 1, 12), 5))
        self.assertEqual(
            chunks,
            [(date(2024, 1, 1), 5), (date(2024, 1, 6), 5), (date(2024, 1, 11), 2)],
        )

    def test_normalizer_removes_repeat_observations_deterministically(self):
        row = {
            "latitude": 18.77495,
            "longitude": 73.83606,
            "acq_date": "2024-01-05",
            "acq_time": "2048",
            "satellite": "N20",
            "instrument": "VIIRS",
            "frp": 1.43,
            "source_file": "first.csv",
            "source_product": "VIIRS_NOAA20_SP",
        }
        duplicate = {**row, "source_file": "second.csv"}

        history, duplicates_removed = normalize_firms_frames(
            [pd.DataFrame([duplicate]), pd.DataFrame([row])],
        )

        self.assertEqual(len(history), 1)
        self.assertEqual(duplicates_removed, 1)
        self.assertEqual(history.iloc[0]["acq_time"], "2048")
        self.assertEqual(history.iloc[0]["observed_at"].isoformat(), "2024-01-05T20:48:00+00:00")

    def test_collection_is_cached_and_normalization_writes_contract(self):
        config = EventConfig(
            event_id=2,
            name="test thermal",
            year=2024,
            bbox=(73.7, 18.7, 73.9, 18.9),
            start_date="2024-01-01",
            end_date="2024-01-05",
            description="",
            analysis_mode=THERMAL_MONITORING_MODE,
            country_code="in",
            roads_provider="osm",
            population_provider="none",
            actual_perimeter_provider="none",
            thermal_history_start="2024-01-05",
            thermal_history_end="2024-01-05",
            firms_history_sources=("VIIRS_NOAA20_SP",),
            firms_chunk_days=5,
        )
        event = SimpleNamespace(id=2, year=2024, name=config.name)

        with tempfile.TemporaryDirectory() as temporary:
            study = SimpleNamespace(project_dir=Path(temporary))
            session = _Session()
            with (
                patch.dict(os.environ, {"FIRMS_API_KEY": "secret-test-key"}),
                patch("pipeline.thermal.history.get_event_config", return_value=config),
            ):
                first = collect_firms_history(event, study, session=session)
                second = collect_firms_history(event, study, session=session)
                metadata = normalize_firms_history(event, study)

            self.assertEqual(first, second)
            self.assertEqual(len(session.urls), 1)
            self.assertNotIn("secret-test-key", first[0].read_text(encoding="utf-8"))
            self.assertEqual(metadata["observation_count"], 1)

            output_dir = Path(temporary) / "data_processed" / "thermal"
            self.assertTrue((output_dir / "firms_history.parquet").exists())
            self.assertTrue((output_dir / "firms_current.parquet").exists())
            saved = json.loads((output_dir / "history_metadata.json").read_text())
            self.assertEqual(saved["first_observed_at"], "2024-01-05T20:48:00+00:00")

    def test_live_collection_omits_date_and_archives_daily_source_file(self):
        config = EventConfig(
            event_id=2,
            name="test thermal",
            year=2024,
            bbox=(73.7, 18.7, 73.9, 18.9),
            start_date="2024-01-01",
            end_date="2024-01-05",
            description="",
            analysis_mode=THERMAL_MONITORING_MODE,
            country_code="in",
            roads_provider="none",
            population_provider="none",
            actual_perimeter_provider="none",
        )
        event = SimpleNamespace(id=2, year=2024, name=config.name)

        with tempfile.TemporaryDirectory() as temporary:
            study = SimpleNamespace(project_dir=Path(temporary))
            session = _Session()
            with (
                patch.dict(os.environ, {"FIRMS_API_KEY": "secret-test-key"}),
                patch("pipeline.thermal.history.get_event_config", return_value=config),
            ):
                result = collect_latest_firms(
                    event,
                    study,
                    day_range=2,
                    sources=("VIIRS_NOAA20_NRT",),
                    session=session,
                )
                metadata = normalize_firms_history(event, study)

            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["successful_source_count"], 1)
            self.assertTrue(session.urls[0][0].endswith("/VIIRS_NOAA20_NRT/73.7,18.7,73.9,18.9/2"))
            self.assertTrue(
                (Path(temporary) / "data_raw/firms/history/live_VIIRS_NOAA20_NRT_2024-01-05.csv").exists()
            )
            self.assertEqual(metadata["source_products"], ["VIIRS_NOAA20_NRT"])


if __name__ == "__main__":
    unittest.main()
