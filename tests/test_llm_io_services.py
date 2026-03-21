import unittest
from unittest.mock import Mock, patch

from app.clients.callback_client import post_callback_payload
from app.utils.file_downloader import download_to_temp_file
from tests import workspace_tempdir


class LLMIOServicesTests(unittest.TestCase):
    @patch("app.services.llm_download_service.requests.get")
    def test_download_to_temp_file_saves_content(self, mock_get):
        mock_get.return_value = Mock(ok=True, content=b"demo", headers={})
        with workspace_tempdir() as tmp:
            path = download_to_temp_file("http://example.test/file.pdf", "demo.pdf", tmp, timeout=10)
            self.assertTrue(path.endswith("demo.pdf"))

    @patch("app.services.llm_callback_service.requests.post")
    def test_post_callback_payload_returns_true_on_200(self, mock_post):
        mock_post.return_value = Mock(ok=True, status_code=200, text="ok")
        self.assertTrue(post_callback_payload("http://callback.test/llm/callback", {"msg": "解析成功"}, timeout=5))
