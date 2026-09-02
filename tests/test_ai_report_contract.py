from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agents.evacuation_agent import run_evacuation_agent  # noqa: E402
from agents.impact_agent import run_impact_agent  # noqa: E402
from agents.summary_agent import run_summary_agent  # noqa: E402
from api.ts_data_routes import _load_ai_report, _save_ai_report  # noqa: E402


class AIReportContractTests(unittest.TestCase):
    def test_missing_population_is_unavailable_and_does_not_call_llm(self):
        population = {
            "within_1km": 0,
            "within_3km": 0,
            "within_5km": 0,
            "data_available": False,
            "reason": "WorldPop raster is not configured.",
        }
        with patch("agents.impact_agent.call_llm") as call:
            result = run_impact_agent(
                {"analysis_mode": "thermal_monitoring"}, population
            )

        call.assert_not_called()
        self.assertFalse(result["population"]["data_available"])
        self.assertIsNone(result["population"]["within_1km"])
        self.assertIn("not configured", result["impact_summary"])

    def test_llm_cannot_change_authoritative_population_numbers(self):
        model_output = json.dumps({
            "population": {"within_1km": 999999},
            "communities_affected": [],
            "worsening_factors": ["wind"],
            "impact_summary": "Estimated exposure requires review.",
        })
        population = {
            "within_1km": 12,
            "within_3km": 34,
            "within_5km": 56,
            "data_available": True,
            "exposure_mode": "proximity_buffers",
            "source": {"provider": "WorldPop"},
        }
        with patch("agents.impact_agent.call_llm", return_value=model_output):
            result = run_impact_agent(
                {"analysis_mode": "thermal_monitoring"}, population
            )

        self.assertEqual(result["population"]["within_1km"], 12)
        self.assertEqual(result["population"]["within_5km"], 56)
        self.assertEqual(result["population"]["source"]["provider"], "WorldPop")

    def test_missing_roads_never_generates_an_evacuation_route(self):
        with patch("agents.evacuation_agent.call_llm") as call:
            result = run_evacuation_agent(
                {"analysis_mode": "thermal_monitoring"},
                [],
                [],
                roads_available=False,
            )

        call.assert_not_called()
        self.assertFalse(result["data_available"])
        self.assertIsNone(result["top_route"])

    def test_thermal_summary_has_mode_specific_non_forecast_assessment(self):
        model_output = json.dumps({
            "key_points": ["One observed hotspot", "Population unavailable", "Verify source"],
            "situation": "A thermal observation requires review.",
            "key_risks": "The source is unresolved.",
            "immediate_actions": "Verify the source on the ground.",
        })
        context = {
            "analysis_mode": "thermal_monitoring",
            "thermal": {"detection_count": 1, "frp_max_mw": 5},
            "region": {"name": "Example Region"},
        }
        with patch("agents.summary_agent.call_llm", return_value=model_output):
            result = run_summary_agent({}, {}, {}, report_context=context)

        self.assertEqual(result["risk_level"], "Unknown")
        self.assertEqual(result["assessment_level"], "Review")
        self.assertEqual(result["report_mode"], "thermal_monitoring")

    def test_report_cache_requires_matching_metadata(self):
        summary = {
            "risk_level": "Unknown",
            "assessment_level": "Review",
            "report_mode": "thermal_monitoring",
            "key_points": [],
            "situation": "Observed.",
            "key_risks": "Unknown source.",
            "immediate_actions": "Verify.",
        }
        metadata = {
            "schema_version": 2,
            "prompt_version": "v1",
            "provider": "huggingface",
            "model": "test-model",
            "input_hash": "abc",
            "report_kind": "standard",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            _save_ai_report(path, {}, {}, {}, summary, metadata)

            self.assertIsNotNone(_load_ai_report(path, metadata))
            self.assertIsNone(_load_ai_report(path, {**metadata, "input_hash": "changed"}))


if __name__ == "__main__":
    unittest.main()
