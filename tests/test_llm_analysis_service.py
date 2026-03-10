import unittest

from app.services.llm_analysis_service import build_file_callback_payload, map_analysis_result


class LLMAnalysisServiceTests(unittest.TestCase):
    def test_map_analysis_result_keeps_translation_fields_blank(self):
        result = map_analysis_result(
            parsed_result={"summary": "摘要", "language": "中文", "score": 3.6},
            request_params={
                "fileName": "demo.pdf",
                "country": [{"key": "02", "value": "美国"}],
                "channel": [{"key": "01", "value": "装发"}],
                "maturity": [{"key": "02", "value": "阶段成果"}],
                "format": [{"key": "03", "value": "文档类"}],
                "architectureList": [{"id": 10, "name": "测试"}],
            },
        )
        self.assertEqual(result["fileDataItem"]["documentTranslationOne"], "")
        self.assertEqual(result["fileDataItem"]["documentTranslationTwo"], "")

    def test_build_file_callback_payload_uses_fixed_success_message(self):
        payload = build_file_callback_payload("demo.pdf", {"summary": "摘要"}, status="2")
        self.assertEqual(payload["msg"], "解析成功")
