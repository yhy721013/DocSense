import json
import os
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
        self.history_dir = self.tmp / "callback"
        self.path_patcher = patch(
            "app.services.utils.callback_preview.CALLBACK_HISTORY_DIR",
            self.history_dir,
        )
        self.path_patcher.start()

    def tearDown(self):
        self.path_patcher.stop()
        self._tempdir.__exit__(None, None, None)

    def write_callback_record(self, name, payload, *, mtime=1000):
        return self.write_raw_callback_record(
            name,
            json.dumps(payload, ensure_ascii=False),
            mtime=mtime,
        )

    def write_raw_callback_record(self, name, content, *, mtime=1000):
        self.history_dir.mkdir(parents=True, exist_ok=True)
        path = self.history_dir / name
        path.write_text(content, encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_callback_api_returns_missing_state_when_history_is_empty_and_ignores_legacy_file(self):
        legacy_path = self.tmp / "call_back.json"
        legacy_path.write_text(
            json.dumps({"businessType": "file", "data": {"fileName": "legacy.txt"}}),
            encoding="utf-8",
        )

        response = self.client.get("/debug/api/callback")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["message"], "当前还没有新版回调历史文件")
        self.assertIsNone(data["payload"])
        self.assertEqual(data["records"], [])
        self.assertIsNone(data["selectedRecord"])

    def test_callback_api_returns_latest_payload_and_record_list_for_file_callback(self):
        old_payload = {
            "businessType": "file",
            "data": {"fileName": "old.txt", "status": "2", "fileDataItem": {}},
            "msg": "旧回调",
        }
        latest_payload = {
            "businessType": "file",
            "data": {
                "fileName": "demo.txt",
                "status": "2",
                "security": "公开",
                "fileDataItem": {
                    "originalText": "原文第一行\n原文第二行",
                    "documentTranslationOne": "<p>单语翻译</p>",
                    "documentTranslationTwo": "<p>双语翻译</p>",
                },
            },
            "msg": "解析成功",
        }
        self.write_callback_record("old.json", old_payload, mtime=1000)
        self.write_callback_record("latest.json", latest_payload, mtime=2000)

        response = self.client.get("/debug/api/callback")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["message"], "读取成功")
        self.assertEqual(data["payload"]["businessType"], "file")
        self.assertEqual(data["payload"]["data"]["fileName"], "demo.txt")
        self.assertEqual(data["payload"]["data"]["security"], "公开")
        self.assertEqual(
            data["payload"]["data"]["fileDataItem"]["originalText"],
            "原文第一行\n原文第二行",
        )
        self.assertEqual([record["id"] for record in data["records"]], ["latest.json", "old.json"])
        self.assertEqual(data["selectedRecord"]["id"], "latest.json")
        self.assertIn("modifiedAt", data["selectedRecord"])
        self.assertGreater(data["selectedRecord"]["sizeBytes"], 0)

    def test_callback_api_returns_selected_payload_for_report_callback(self):
        payload = {
            "businessType": "report",
            "data": {
                "reportId": 132,
                "status": "1",
                "details": "<h1>报告正文</h1>",
            },
            "msg": "生成成功",
        }
        self.write_callback_record(
            "latest-file.json",
            {"businessType": "file", "data": {"fileName": "latest.txt"}, "msg": "解析成功"},
            mtime=2000,
        )
        self.write_callback_record("report-132.json", payload, mtime=1000)

        response = self.client.get("/debug/api/callback?record=report-132.json")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["payload"]["businessType"], "report")
        self.assertEqual(data["payload"]["data"]["reportId"], 132)
        self.assertEqual(data["selectedRecord"]["id"], "report-132.json")

    def test_callback_api_returns_payload_for_weaponry_callback(self):
        payload = {
            "businessType": "weaponry",
            "data": {
                "status": "2",
                "architectureId": 10502,
                "weaponryTemplateFieldList": [
                    {
                        "fieldName": "舰级名称",
                        "fieldType": "INPUT",
                        "fieldDescription": "根据文档提取舰级名称",
                        "analyseData": "尼米兹级",
                        "analyseDataSource": [
                            {
                                "content": "舰级名称为尼米兹级",
                                "source": "CVN 装备资料.pdf",
                                "time": "2026-04-07 12:00:00",
                                "fileName": "3199b401658d49e781469534e8613913.pdf",
                                "rows": ["CVN 文档片段"],
                                "translate": "舰级名称为尼米兹级",
                            }
                        ],
                    }
                ],
            },
            "msg": "解析成功",
        }
        self.write_callback_record("weaponry-10502.json", payload, mtime=1000)

        response = self.client.get("/debug/api/callback?record=weaponry-10502.json")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["payload"]["businessType"], "weaponry")
        self.assertEqual(data["payload"]["data"]["architectureId"], 10502)
        source = data["payload"]["data"]["weaponryTemplateFieldList"][0]["analyseDataSource"][0]
        self.assertEqual(source["source"], "CVN 装备资料.pdf")
        self.assertEqual(source["fileName"], "3199b401658d49e781469534e8613913.pdf")
        self.assertEqual(source["rows"], ["CVN 文档片段"])

    def test_callback_api_rejects_invalid_or_missing_record_parameter(self):
        self.write_callback_record(
            "valid.json",
            {"businessType": "file", "data": {"fileName": "valid.txt"}, "msg": "解析成功"},
        )

        for record_name in ["missing.json", "../valid.json", "nested/valid.json", "valid.txt"]:
            with self.subTest(record_name=record_name):
                response = self.client.get(f"/debug/api/callback?record={record_name}")
                data = response.get_json()

                self.assertEqual(response.status_code, 200)
                self.assertFalse(data["ok"])
                self.assertEqual(data["message"], "指定的回调历史记录不存在")
                self.assertIsNone(data["payload"])
                self.assertIsNone(data["selectedRecord"])
                self.assertEqual([record["id"] for record in data["records"]], ["valid.json"])

    def test_callback_api_returns_invalid_json_state(self):
        self.write_raw_callback_record("invalid.json", "{invalid")

        response = self.client.get("/debug/api/callback")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["message"], "回调文件不是合法 JSON")
        self.assertIsNone(data["payload"])
        self.assertEqual(data["selectedRecord"]["id"], "invalid.json")

    def test_callback_api_returns_non_object_root_state(self):
        self.write_raw_callback_record("list.json", "[]")

        response = self.client.get("/debug/api/callback")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["message"], "回调文件根节点必须为对象")
        self.assertIsNone(data["payload"])
        self.assertEqual(data["selectedRecord"]["id"], "list.json")

    def test_callback_api_returns_read_failure_state_when_read_text_raises(self):
        self.write_raw_callback_record("unreadable.json", "{}")

        with patch("pathlib.Path.read_text", side_effect=OSError("boom")):
            response = self.client.get("/debug/api/callback")
            data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["message"], "回调文件读取失败")
        self.assertIsNone(data["payload"])
        self.assertEqual(data["selectedRecord"]["id"], "unreadable.json")

    def test_callback_page_renders_debug_shell(self):
        response = self.client.get("/debug/callback")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("回调结果调试页", html)
        self.assertIn('id="refresh-button"', html)
        self.assertIn('id="record-select"', html)
        self.assertIn('id="callback-summary"', html)
        self.assertIn("/debug/api/callback", html)
        self.assertIn("function renderRecordOptions(records, selectedRecord, requestedRecordId)", html)
        self.assertIn("function buildApiUrl(recordId)", html)
        self.assertIn("encodeURIComponent(recordId)", html)

    def test_callback_page_contains_renderer_hooks_for_file_and_report(self):
        response = self.client.get("/debug/callback")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("function renderFilePayload(payload)", html)
        self.assertIn("function renderReportPayload(payload)", html)
        self.assertIn("function renderHtmlPreview(title, content)", html)
        self.assertIn("function sanitizePreviewMarkup(content)", html)
        self.assertIn("function renderUnsupportedPayload(payload)", html)
        self.assertIn('if (result.payload.businessType === "file")', html)
        self.assertIn('if (result.payload.businessType === "report")', html)
        self.assertIn("renderUnsupportedPayload(result.payload)", html)
        self.assertIn('iframe.setAttribute("sandbox", "")', html)
        self.assertIn("Content-Security-Policy", html)
        self.assertIn("default-src 'none'", html)
        self.assertNotIn("DOMParser", html)
        self.assertIn('"script"', html)
        self.assertIn('"iframe"', html)
        self.assertIn('"img"', html)
        self.assertIn('"link"', html)
        self.assertIn('"href"', html)
        self.assertIn('"xlink:href"', html)
        self.assertIn('"action"', html)
        self.assertIn('"formaction"', html)
        self.assertIn('"data"', html)
        self.assertIn('return url.protocol === "http:" || url.protocol === "https:";', html)
        self.assertIn('document.createElement("a")', html)
        self.assertIn('<pre>${escapeHtml(displayValue(content))}</pre>', html)
        self.assertIn('renderPlainTextPreview("原文"', html)
        self.assertIn('renderHtmlPreview("单语翻译预览"', html)
        self.assertIn('renderHtmlPreview("双语翻译预览"', html)
        self.assertIn('renderFieldGrid("数据标准信息"', html)
        self.assertIn("data.security", html)
        self.assertNotIn("data.secrets", html)
        self.assertIn('"militaryName"', html)
        self.assertIn('"approvalDept"', html)
        self.assertIn('renderHtmlPreview("报告预览"', html)
        self.assertIn('id="preview-sections"', html)
        self.assertIn('id="structured-content"', html)
        self.assertIn('id="raw-json"', html)

    def test_callback_page_contains_renderer_hooks_for_weaponry(self):
        response = self.client.get("/debug/callback")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("function countWeaponryStats(fields)", html)
        self.assertIn("function renderWeaponrySources(sources)", html)
        self.assertIn("function renderWeaponryField(field)", html)
        self.assertIn("function renderWeaponryPayload(payload)", html)
        self.assertIn("displayValue(item.fileName)", html)
        self.assertIn("JSON.stringify(item.rows, null, 2)", html)
        self.assertIn('if (result.payload.businessType === "weaponry")', html)
        self.assertIn("renderWeaponryPayload(result.payload)", html)
