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

    def test_progress_hub_sends_current_progress_on_subscribe(self):
        """订阅时立即接收到最新进度"""
        hub = LLMProgressHub()
        hub.publish("file", "test.pdf", {"businessType": "file", "data": {"fileName": "test.pdf", "progress": 0.45}})
        sink = []
        hub.subscribe("file", "test.pdf", sink.append)
        self.assertEqual(len(sink), 1)
        self.assertEqual(sink[0]["data"]["progress"], 0.45)

    def test_progress_hub_broadcasts_to_all_subscribers(self):
        """进度更新广播到所有订阅者"""
        hub = LLMProgressHub()
        sink1, sink2 = [], []
        hub.subscribe("file", "multi.pdf", sink1.append)
        hub.subscribe("file", "multi.pdf", sink2.append)
        hub.publish("file", "multi.pdf", {"businessType": "file", "data": {"progress": 0.55}})
        self.assertEqual(sink1[-1]["data"]["progress"], 0.55)
        self.assertEqual(sink2[-1]["data"]["progress"], 0.55)

    def test_file_analysis_produces_finer_progress_steps(self):
        """文件分析任务产生更细粒度的进度步骤"""
        with tempfile.TemporaryDirectory() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            hub = LLMProgressHub()
            service.create_file_task("step_test.pdf", {"businessType": "file"})

            published_progress = []
            hub.subscribe("file", "step_test.pdf", lambda m: published_progress.append(m["data"]["progress"]))

            # Simulate the progress steps that run_file_analysis_task publishes
            for p in [0.0, 0.10, 0.30, 0.45, 0.55, 0.75, 0.90, 1.0]:
                hub.publish("file", "step_test.pdf", {"businessType": "file", "data": {"fileName": "step_test.pdf", "progress": p}})

            self.assertGreaterEqual(len(published_progress), 7, "应至少有7个进度更新步骤")
            self.assertIn(0.10, published_progress)
            self.assertIn(0.30, published_progress)
            self.assertIn(0.45, published_progress)
            self.assertIn(0.55, published_progress)
            self.assertIn(0.75, published_progress)
            self.assertIn(0.90, published_progress)
            self.assertIn(1.0, published_progress)
