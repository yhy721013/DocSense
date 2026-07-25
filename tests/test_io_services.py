from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
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
        response = Mock(status_code=200, headers={})
        response.iter_content.return_value = (b"de", b"mo")
        mock_get.return_value = response
        with workspace_tempdir() as tmp:
            path = download_to_temp_file("http://example.test/file.pdf", "demo.pdf", tmp, timeout=10)
            self.assertTrue(path.endswith("demo.pdf"))
            self.assertEqual(b"demo", Path(path).read_bytes())
        response.close.assert_called_once_with()

    @patch("app.services.utils.file_downloader.requests.get")
    def test_download_rejects_declared_oversize_before_writing(self, mock_get):
        response = Mock(status_code=200, headers={"Content-Length": "5"})
        response.iter_content.return_value = (b"12345",)
        mock_get.return_value = response

        with workspace_tempdir() as tmp:
            with self.assertRaisesRegex(RuntimeError, "大小上限"):
                download_to_temp_file(
                    "http://example.test/file.pdf",
                    "demo.pdf",
                    tmp,
                    timeout=10,
                    max_bytes=4,
                )
            self.assertFalse((Path(tmp) / "demo.pdf").exists())
            self.assertEqual([], list(Path(tmp).glob("*.part")))

    @patch("app.services.utils.file_downloader.requests.get")
    def test_download_rejects_streamed_oversize_without_content_length(self, mock_get):
        response = Mock(status_code=200, headers={})
        response.iter_content.return_value = (b"12", b"345")
        mock_get.return_value = response

        with workspace_tempdir() as tmp:
            with self.assertRaisesRegex(RuntimeError, "大小上限"):
                download_to_temp_file(
                    "http://example.test/file.pdf",
                    "demo.pdf",
                    tmp,
                    timeout=10,
                    max_bytes=4,
                )
            self.assertFalse((Path(tmp) / "demo.pdf").exists())
            self.assertEqual([], list(Path(tmp).glob("*.part")))

    @patch("app.services.utils.file_downloader.time.monotonic")
    @patch("app.services.utils.file_downloader.requests.get")
    def test_download_enforces_total_transfer_deadline(self, mock_get, monotonic):
        response = Mock(status_code=200, headers={})
        response.iter_content.return_value = (b"data",)
        mock_get.return_value = response
        # 启动、响应头检查、首个数据块检查；第三次已经超过 1 秒总期限。
        monotonic.side_effect = (0.0, 0.1, 1.1)

        with workspace_tempdir() as tmp:
            with self.assertRaisesRegex(TimeoutError, "总传输时限"):
                download_to_temp_file(
                    "http://example.test/file.pdf",
                    "demo.pdf",
                    tmp,
                    timeout=1,
                    max_bytes=1024,
                )
            self.assertFalse((Path(tmp) / "demo.pdf").exists())
            self.assertEqual([], list(Path(tmp).glob("*.part")))

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
        mock_post.return_value.close.assert_called_once_with()

    @patch("app.services.utils.callback_client.requests.post")
    def test_post_callback_payload_rejects_redirect_response(self, mock_post):
        """3xx 不是甲方已接收回调的证据，不能使用 Response.ok 放宽。"""

        mock_post.return_value = Mock(ok=True, status_code=302, text="redirect")
        with workspace_tempdir() as tmp:
            with patch(
                "app.services.utils.callback_client.CALLBACK_HISTORY_DIR",
                Path(tmp) / "callback",
            ):
                delivered = post_callback_payload(
                    "http://callback.test/llm/callback",
                    {"businessType": "file", "data": {}, "msg": "完成"},
                    timeout=5,
                )

        self.assertFalse(delivered)
        mock_post.return_value.close.assert_called_once_with()

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

    def test_concurrent_callback_history_writes_are_atomic_and_unique(self):
        with workspace_tempdir() as tmp:
            history_dir = Path(tmp) / "callback"
            timestamp = datetime(2026, 6, 22, 12, 0, 0, 123456)

            def save(index: int) -> Path:
                return save_callback_history_payload(
                    {
                        "businessType": "report",
                        "data": {"reportId": 0, "sequence": index},
                    },
                    history_dir=history_dir,
                    timestamp=timestamp,
                )

            with ThreadPoolExecutor(max_workers=16) as executor:
                paths = tuple(executor.map(save, range(50)))

            self.assertEqual(50, len(set(paths)))
            self.assertEqual(50, len(tuple(history_dir.glob("*.json"))))
            self.assertTrue(all(path.name.startswith("report-0-") for path in paths))
            sequences = {
                json.loads(path.read_text(encoding="utf-8"))["data"]["sequence"]
                for path in paths
            }
            self.assertEqual(set(range(50)), sequences)

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
