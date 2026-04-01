import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from tests import workspace_tempdir


class CallbackDebugRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        self._tempdir = workspace_tempdir()
        self.tmp = Path(self._tempdir.__enter__())
        self.callback_path = self.tmp / "call_back.json"
        self.path_patcher = patch(
            "app.services.utils.callback_preview.CALLBACK_PREVIEW_PATH",
            self.callback_path,
        )
        self.path_patcher.start()

    def tearDown(self):
        self.path_patcher.stop()
        self._tempdir.__exit__(None, None, None)

    def test_callback_api_returns_missing_state_when_file_does_not_exist(self):
        response = self.client.get("/debug/api/callback")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "ok": False,
                "message": "当前还没有回调结果文件",
                "payload": None,
            },
        )

    def test_callback_api_returns_payload_for_file_callback(self):
        payload = {
            "businessType": "file",
            "data": {
                "fileName": "demo.txt",
                "status": "2",
                "fileDataItem": {
                    "originalText": "原文第一行\n原文第二行",
                    "documentTranslationOne": "<p>单语翻译</p>",
                    "documentTranslationTwo": "<p>双语翻译</p>",
                },
            },
            "msg": "解析成功",
        }
        self.callback_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        response = self.client.get("/debug/api/callback")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["message"], "读取成功")
        self.assertEqual(data["payload"]["businessType"], "file")
        self.assertEqual(
            data["payload"]["data"]["fileDataItem"]["originalText"],
            "原文第一行\n原文第二行",
        )

    def test_callback_api_returns_payload_for_report_callback(self):
        payload = {
            "businessType": "report",
            "data": {
                "reportId": 132,
                "status": "1",
                "details": "<h1>报告正文</h1>",
            },
            "msg": "生成成功",
        }
        self.callback_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        response = self.client.get("/debug/api/callback")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["payload"]["businessType"], "report")
        self.assertEqual(data["payload"]["data"]["reportId"], 132)

    def test_callback_api_returns_invalid_json_state(self):
        self.callback_path.write_text("{invalid", encoding="utf-8")

        response = self.client.get("/debug/api/callback")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "ok": False,
                "message": "回调文件不是合法 JSON",
                "payload": None,
            },
        )

    def test_callback_api_returns_non_object_root_state(self):
        self.callback_path.write_text("[]", encoding="utf-8")

        response = self.client.get("/debug/api/callback")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "ok": False,
                "message": "回调文件根节点必须为对象",
                "payload": None,
            },
        )

    def test_callback_api_returns_read_failure_state_when_read_text_raises(self):
        self.callback_path.write_text("{}", encoding="utf-8")

        with patch("pathlib.Path.read_text", side_effect=OSError("boom")):
            response = self.client.get("/debug/api/callback")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "ok": False,
                "message": "回调文件读取失败",
                "payload": None,
            },
        )

    def test_callback_page_renders_debug_shell(self):
        response = self.client.get("/debug/callback")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("回调结果调试页", html)
        self.assertIn('id="refresh-button"', html)
        self.assertIn('id="callback-summary"', html)
        self.assertIn("/debug/api/callback", html)

    def test_callback_page_contains_renderer_hooks_for_file_and_report(self):
        response = self.client.get("/debug/callback")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("function renderFilePayload(payload)", html)
        self.assertIn("function renderReportPayload(payload)", html)
        self.assertIn("function renderHtmlPreview(title, content)", html)
        self.assertIn('id="preview-sections"', html)
        self.assertIn('id="structured-content"', html)
