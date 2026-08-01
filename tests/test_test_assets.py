import json
import pathlib
import unittest


class LLMTestAssetsTests(unittest.TestCase):
    def _load_fixture(self, relative_path: str):
        """联调夹具未纳入版本控制；缺失时跳过，避免把环境资产误报为代码错误。"""

        path = pathlib.Path(relative_path)
        if not path.is_file():
            self.skipTest(f"本地联调夹具未提供: {relative_path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_analysis_request_fixture_has_required_fields(self):
        payload = self._load_fixture("tests/fixtures/llm/analysis_request.json")
        self.assertEqual(payload["businessType"], "file")
        self.assertGreaterEqual(len(payload["params"]), 2)
        self.assertIn("filePath", payload["params"][0])
        self.assertNotIn("country", payload["params"][0])
        self.assertNotIn("channel", payload["params"][0])
        self.assertNotIn("format", payload["params"][0])
        self.assertNotIn("maturity", payload["params"][0])
        self.assertNotIn("architectureList", payload["params"][0])

    def test_check_task_fixture_can_query_multiple_files(self):
        payload = self._load_fixture(
            "tests/fixtures/llm/check_task_file_request.json"
        )
        self.assertEqual(payload["businessType"], "file")
        self.assertGreaterEqual(len(payload["params"]), 2)
        self.assertIn("fileName", payload["params"][0])

    def test_report_request_fixture_has_required_fields(self):
        payload = self._load_fixture("tests/fixtures/llm/report_request.json")
        self.assertEqual(payload["businessType"], "report")
        self.assertIn("filePathList", payload["params"][0])
        self.assertIn("templateOutline", payload["params"][0])
        self.assertTrue(payload["params"][0]["templateOutline"].endswith(".docx"))

    def test_weaponry_request_fixture_uses_ship_fields(self):
        payload = self._load_fixture("tests/fixtures/llm/weaponry_request.json")
        self.assertEqual(payload["businessType"], "weaponry")

        params = payload["params"]
        self.assertEqual(params["architectureId"], 10502)

        field_names = [field["fieldName"] for field in params["weaponryTemplateFieldList"]]
        self.assertEqual(
            field_names,
            [
                "舰级名称",
                "单舰名称",
                "舷号",
                "建造厂",
                "开工时间",
                "下水时间",
                "服役时间",
                "状态",
                "标准排水量",
                "满载排水量",
                "舰长",
                "舰宽",
                "吃水",
                "甲板长度",
                "甲板宽度",
                "航速",
                "编制",
                "动力系统",
                "武器系统",
                "传感器系统",
            ],
        )

        for field in params["weaponryTemplateFieldList"]:
            self.assertEqual(field["fieldType"], "INPUT")
            self.assertIn("fieldDescription", field)
            self.assertNotIn("analyseData", field)
            self.assertNotIn("analyseDataSource", field)

    def test_check_task_weaponry_fixture_matches_request_architecture_id(self):
        request_payload = self._load_fixture(
            "tests/fixtures/llm/weaponry_request.json"
        )
        check_payload = self._load_fixture(
            "tests/fixtures/llm/check_task_weaponry_request.json"
        )

        self.assertEqual(check_payload["businessType"], "weaponry")
        self.assertEqual(check_payload["params"][0]["architectureId"], request_payload["params"]["architectureId"])
