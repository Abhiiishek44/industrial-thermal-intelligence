from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agents.chat_agent import run_chat_agent  # noqa: E402
from agents.prompts import CHAT_AGENT_SYSTEM, THERMAL_CHAT_AGENT_SYSTEM  # noqa: E402


class ChatAgentPromptTests(unittest.TestCase):
    def _capture_system(self, analysis_mode: str | None) -> str:
        captured: dict[str, str] = {}

        def fake_stream(system, _messages):
            captured["system"] = system
            yield "ok"

        with patch("agents.chat_agent.stream_llm", side_effect=fake_stream):
            self.assertEqual(
                list(run_chat_agent(
                    summary="FRP 11.8 MW; one observation; exposure unknown.",
                    message="What was detected?",
                    history=[],
                    analysis_mode=analysis_mode,
                )),
                ["ok"],
            )
        return captured["system"]

    def test_thermal_mode_uses_industrial_thermal_prompt(self):
        system = self._capture_system("thermal_monitoring")
        self.assertTrue(system.startswith(THERMAL_CHAT_AGENT_SYSTEM))
        self.assertIn("Industrial Thermal Intelligence", system)
        self.assertIn("Near a facility", system)
        self.assertIn("Current industrial thermal intelligence report", system)
        self.assertNotIn("pre-computed situational analysis report", system)

    def test_wildfire_mode_keeps_existing_prompt(self):
        system = self._capture_system("wildfire_prediction")
        self.assertTrue(system.startswith(CHAT_AGENT_SYSTEM))
        self.assertIn("Current situational report", system)
        self.assertNotIn("Current industrial thermal intelligence report", system)


if __name__ == "__main__":
    unittest.main()
