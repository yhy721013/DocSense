import tempfile
import unittest
from unittest.mock import patch

from app.services.llm_progress_hub import LLMProgressHub
from app.services.llm_task_service import LLMTaskService


class LLMProgressAndCheckTaskTests(unittest.TestCase):
    def test_progress_hub_broadcasts_latest_message(self):
        hub = LLMProgressHub()
        sink = []
        hub.subscribe("file", "demo.pdf", sink.append)
        hub.publish("file", "demo.pdf", {"businessType": "file", "data": {"fileName": "demo.pdf", "progress": 0.35}})
        self.assertEqual(sink[-1]["data"]["progress"], 0.35)

    @patch("app.services.llm_task_service.post_callback_payload", return_value=True)
    def test_check_task_replays_failed_callback(self, _mock_callback):
        with tempfile.TemporaryDirectory() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            service.create_file_task("demo.pdf", {"businessType": "file"})
            service.mark_business_completed("file", "demo.pdf", {"fileName": "demo.pdf"}, status="2")
            service.mark_callback_failed("file", "demo.pdf", "timeout")
            replayed = service.replay_callback_if_needed("file", "demo.pdf", callback_url="http://callback.test/llm/callback", timeout=5)
            self.assertTrue(replayed)
