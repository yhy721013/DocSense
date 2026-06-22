from datetime import datetime
import json
from http.server import HTTPServer
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from app.services.utils.callback_client import (
    build_callback_history_stem,
    post_callback_payload,
    save_callback_history_payload,
)
from app.services.utils.file_downloader import download_to_temp_file
from scripts.mock_callback_server import CallbackHandler
from tests import workspace_tempdir


class LLMIOServicesTests(unittest.TestCase):
    @patch("app.services.utils.file_downloader.requests.get")
    def test_download_to_temp_file_saves_content(self, mock_get):
        mock_get.return_value = Mock(ok=True, content=b"demo", headers={})
        with workspace_tempdir() as tmp:
            path = download_to_temp_file("http://example.test/file.pdf", "demo.pdf", tmp, timeout=10)
            self.assertTrue(path.endswith("demo.pdf"))

    @patch("app.services.utils.callback_client.requests.post")
    def test_post_callback_payload_returns_true_on_200(self, mock_post):
        mock_post.return_value = Mock(ok=True, status_code=200, text="ok")
        with workspace_tempdir() as tmp:
            history_dir = Path(tmp) / "callback"
            with patch("app.services.utils.callback_client.CALLBACK_HISTORY_DIR", history_dir):
                self.assertTrue(
                    post_callback_payload(
                        "http://callback.test/llm/callback",
                        {
                            "businessType": "file",
                            "data": {"fileName": "bded228dc94440519d87f97cfb6b520b.pdf"},
                            "msg": "解析成功",
                        },
                        timeout=5,
                        callback_context={
                            "businessType": "file",
                            "fileName": "bded228dc94440519d87f97cfb6b520b.pdf",
                            "originalFileName": "GJB 9001C-2017 质量管理体系要求.pdf",
                        },
                    )
                )

            files = list(history_dir.glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertRegex(
                files[0].name,
                r"^GJB 9001C-2017 质量管理体系要求-bded228dc94440519d87f97cfb6b520b-\d{8}T\d{12}\.json$",
            )
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["msg"], "解析成功")
        mock_post.assert_called_once()

    def test_build_callback_history_stem_uses_file_original_and_internal_names(self):
        stem = build_callback_history_stem(
            {"businessType": "file", "data": {"fileName": "bded228dc94440519d87f97cfb6b520b.pdf"}},
            {
                "businessType": "file",
                "fileName": "bded228dc94440519d87f97cfb6b520b.pdf",
                "originalFileName": "GJB 9001C-2017 质量管理体系要求.pdf",
            },
            timestamp=datetime(2026, 6, 22, 12, 0, 0, 123456),
        )

        self.assertEqual(
            stem,
            "GJB 9001C-2017 质量管理体系要求-bded228dc94440519d87f97cfb6b520b-20260622T120000123456",
        )

    def test_build_callback_history_stem_sanitizes_and_falls_back_by_business_type(self):
        timestamp = datetime(2026, 6, 22, 12, 0, 0, 123456)

        file_stem = build_callback_history_stem(
            {"businessType": "file", "data": {"fileName": r"nested\abc:def?.txt"}},
            {"businessType": "file", "originalFileName": "dir/GJB:9001C?.pdf"},
            timestamp=timestamp,
        )
        report_stem = build_callback_history_stem(
            {"businessType": "report", "data": {"reportId": 132}},
            timestamp=timestamp,
        )
        weaponry_stem = build_callback_history_stem(
            {"businessType": "weaponry", "data": {"architectureId": 621103438000}},
            timestamp=timestamp,
        )

        self.assertEqual(file_stem, "GJB-9001C-abc-def-20260622T120000123456")
        self.assertEqual(report_stem, "report-132-20260622T120000123456")
        self.assertEqual(weaponry_stem, "weaponry-621103438000-20260622T120000123456")

    def test_save_callback_history_payload_does_not_overwrite_same_stem(self):
        with workspace_tempdir() as tmp:
            history_dir = Path(tmp) / "callback"
            payload = {"businessType": "report", "data": {"reportId": 7}, "msg": "生成成功"}
            timestamp = datetime(2026, 6, 22, 12, 0, 0, 123456)

            first = save_callback_history_payload(payload, history_dir=history_dir, timestamp=timestamp)
            second = save_callback_history_payload(payload, history_dir=history_dir, timestamp=timestamp)

            self.assertEqual(first.name, "report-7-20260622T120000123456.json")
            self.assertEqual(second.name, "report-7-20260622T120000123456-2.json")
            self.assertEqual(len(list(history_dir.glob("*.json"))), 2)

    def test_mock_callback_server_writes_callback_history_file(self):
        with workspace_tempdir() as tmp:
            history_dir = Path(tmp) / "callback"
            with patch("app.services.utils.callback_client.CALLBACK_HISTORY_DIR", history_dir):
                server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f"http://127.0.0.1:{server.server_address[1]}/llm/callback"
                    response = requests.post(
                        url,
                        json={
                            "businessType": "weaponry",
                            "data": {"architectureId": 621103438000, "status": "2"},
                            "msg": "解析成功",
                        },
                        timeout=5,
                    )
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

            self.assertTrue(response.ok)
            files = list(history_dir.glob("weaponry-621103438000-*.json"))
            self.assertEqual(len(files), 1)
