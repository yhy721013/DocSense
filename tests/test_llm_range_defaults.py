import unittest

from app.services.llm_analysis_service import build_effective_analysis_ranges


class LLMRangeDefaultTests(unittest.TestCase):
    def test_missing_ranges_use_default_test_values(self):
        ranges = build_effective_analysis_ranges({"fileName": "demo.txt"})
        self.assertEqual([item["value"] for item in ranges["format"]], ["音频类", "文档类", "图片类"])
        self.assertTrue(ranges["architectureList"])

    def test_explicit_ranges_override_defaults(self):
        ranges = build_effective_analysis_ranges(
            {
                "fileName": "demo.txt",
                "country": [{"key": "99", "value": "德国"}],
            }
        )
        self.assertEqual([item["value"] for item in ranges["country"]], ["德国"])
