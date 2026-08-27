from __future__ import annotations

import sys
import unittest
from pathlib import Path

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from api.crowd import _parse_report_payload  # noqa: E402


class FieldReportPayloadTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_all_four_report_types_are_accepted(self):
        for post_type in ("fire_report", "info", "request_help", "offer_help"):
            with self.subTest(post_type=post_type), self.app.test_request_context(
                "/", method="POST", json={
                    "post_type": post_type,
                    "description": "Field observation",
                    "lat": 20.25,
                    "lon": 79.10,
                    "observed_at": "2026-04-19T02:38:00Z",
                }
            ):
                parsed = _parse_report_payload()
                self.assertEqual(parsed[0], post_type)
                self.assertEqual(parsed[1], "Field observation")
                self.assertEqual(parsed[2:4], (20.25, 79.10))
                self.assertEqual(parsed[4].isoformat(), "2026-04-19T02:38:00")

    def test_unknown_report_type_is_rejected(self):
        with self.app.test_request_context(
            "/", method="POST", json={
                "post_type": "emergency",
                "description": "Test",
                "lat": 20,
                "lon": 79,
            }
        ):
            with self.assertRaisesRegex(ValueError, "invalid post_type"):
                _parse_report_payload()

    def test_description_and_coordinate_ranges_are_required(self):
        bad_payloads = (
            ({"post_type": "info", "description": "", "lat": 20, "lon": 79}, "description required"),
            ({"post_type": "info", "description": "Test", "lat": 120, "lon": 79}, "outside valid range"),
            ({"post_type": "info", "description": "Test", "lat": 20}, "valid lat and lon required"),
        )
        for payload, message in bad_payloads:
            with self.subTest(payload=payload), self.app.test_request_context(
                "/", method="POST", json=payload
            ):
                with self.assertRaisesRegex(ValueError, message):
                    _parse_report_payload()


if __name__ == "__main__":
    unittest.main()
