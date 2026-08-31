import unittest

from app.modules.analysis.domain.ranges import build_effective_analysis_ranges


class LLMRangeDefaultTests(unittest.TestCase):
    def test_missing_ranges_use_field_specific_defaults(self):
        ranges = build_effective_analysis_ranges({"fileName": "demo.txt"})
        self.assertEqual(ranges["channel"], [])
        self.assertEqual([item["value"] for item in ranges["format"]], ["音频类", "文档类", "图片类"])
        self.assertEqual([item["value"] for item in ranges["security"]], ["公开"])
        self.assertTrue(ranges["architectureList"])
        self.assertEqual(ranges["architectureStandardList"], [])

    def test_explicit_ranges_override_defaults(self):
        ranges = build_effective_analysis_ranges(
            {
                "fileName": "demo.txt",
                "country": [{"key": "99", "value": "德国"}],
                "channel": [{"key": "98", "value": "公开发布"}],
                "security": [{"key": "01", "value": "秘密"}],
                "architectureStandardList": [{"id": 202, "name": "数据标准"}],
            }
        )
        self.assertEqual([item["value"] for item in ranges["country"]], ["德国"])
        self.assertEqual([item["value"] for item in ranges["channel"]], ["公开发布"])
        self.assertEqual([item["value"] for item in ranges["security"]], ["秘密"])
        self.assertEqual(ranges["architectureStandardList"], [{"id": 202, "name": "数据标准"}])

    def test_empty_or_invalid_channel_does_not_use_server_defaults(self):
        for raw_channel in (None, [], "装发", [{}, None]):
            with self.subTest(raw_channel=raw_channel):
                params = {"fileName": "demo.txt", "channel": raw_channel}

                ranges = build_effective_analysis_ranges(params)

                self.assertEqual(ranges["channel"], [])
