import json
import pathlib
import unittest


class LLMTestAssetsTests(unittest.TestCase):
    def test_analysis_request_fixture_has_required_fields(self):
        payload = json.loads(pathlib.Path("tests/fixtures/llm/analysis_request.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["businessType"], "file")
        self.assertGreaterEqual(len(payload["params"]), 2)
        self.assertIn("filePath", payload["params"][0])
        self.assertNotIn("country", payload["params"][0])
        self.assertNotIn("channel", payload["params"][0])
        self.assertNotIn("format", payload["params"][0])
        self.assertNotIn("maturity", payload["params"][0])
        self.assertNotIn("architectureList", payload["params"][0])

    def test_check_task_fixture_can_query_multiple_files(self):
        payload = json.loads(pathlib.Path("tests/fixtures/llm/check_task_file_request.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["businessType"], "file")
        self.assertGreaterEqual(len(payload["params"]), 2)
        self.assertIn("fileName", payload["params"][0])

    def test_report_request_fixture_has_required_fields(self):
        payload = json.loads(pathlib.Path("tests/fixtures/llm/report_request.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["businessType"], "report")
        self.assertIn("filePathList", payload["params"][0])
