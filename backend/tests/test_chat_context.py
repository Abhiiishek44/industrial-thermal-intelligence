"""Tests for region-scoped assistant retrieval context."""

import unittest
import sys
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from agents.chat_agent import run_chat_agent


class ChatRetrievalContextTests(unittest.TestCase):
    def test_authoritative_region_evidence_is_injected_before_report(self):
        evidence = {
            "scope": "selected_region_and_observation",
            "event_id": 8,
            "timestep_id": 40437,
            "evidence": {
                "region": {
                    "region_id": "gadchiroli_tadoba",
                    "name": "Gadchiroli Forest Landscape Monitoring",
                    "state": "Maharashtra",
                },
                "observation_time": "2026-10-06T02:16:00",
                "thermal": {"detection_count": 2},
            },
        }

        with patch("agents.chat_agent.stream_llm", return_value=iter(["ok"])) as mocked:
            output = "".join(run_chat_agent(
                summary="Secondary generated report",
                message="Which region is selected?",
                history=[],
                analysis_mode="thermal_monitoring",
                retrieved_evidence=evidence,
            ))

        self.assertEqual(output, "ok")
        system_prompt = mocked.call_args.args[0]
        self.assertIn("AUTHORITATIVE RETRIEVED REGION AND OBSERVATION CONTEXT", system_prompt)
        self.assertIn("Gadchiroli Forest Landscape Monitoring", system_prompt)
        self.assertIn('"detection_count": 2', system_prompt)
        self.assertLess(
            system_prompt.index("AUTHORITATIVE RETRIEVED REGION"),
            system_prompt.index("Secondary generated report"),
        )


if __name__ == "__main__":
    unittest.main()
