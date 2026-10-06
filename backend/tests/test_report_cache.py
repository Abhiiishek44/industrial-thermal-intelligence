"""Tests for report-cache safety in the AI assistant."""

import unittest

from api.timesteps import _report_metadata_is_current
from api.ts_data_routes import _PROMPT_VERSION, _REPORT_SCHEMA_VERSION


class ReportCacheMetadataTests(unittest.TestCase):
    def test_accepts_current_report_metadata(self):
        self.assertTrue(_report_metadata_is_current({
            "schema_version": _REPORT_SCHEMA_VERSION,
            "prompt_version": _PROMPT_VERSION,
        }))

    def test_rejects_stale_prompt(self):
        self.assertFalse(_report_metadata_is_current({
            "schema_version": _REPORT_SCHEMA_VERSION,
            "prompt_version": "older-prompt",
        }))

    def test_rejects_missing_metadata(self):
        self.assertFalse(_report_metadata_is_current({}))


if __name__ == "__main__":
    unittest.main()
