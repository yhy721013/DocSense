import unittest
from unittest.mock import patch

from app import create_app
from app.blueprints import llm as llm_module
from app.blueprints.llm import _handle_progress_command, _parse_progress_command
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


class LLMProgressAndCheckTaskTests(unittest.TestCase):
    def test_progress_hub_broadcasts_latest_message(self):
        hub = LLMProgressHub()
        sink = []
        hub.subscribe("file", "demo.pdf", sink.append)
        hub.publish("file", "demo.pdf", {"businessType": "file", "data": {"fileName": "demo.pdf", "progress": 0.35}})
        self.assertEqual(sink[-1]["data"]["progress"], 0.35)

    def test_progress_hub_keeps_latest_message_per_task(self):
        hub = LLMProgressHub()
        hub.publish("file", "a.pdf", {"businessType": "file", "data": {"fileName": "a.pdf", "progress": 0.15}})
        hub.publish("file", "b.pdf", {"businessType": "file", "data": {"fileName": "b.pdf", "progress": 0.35}})

        self.assertEqual(hub.get_latest("file", "a.pdf")["data"]["fileName"], "a.pdf")
        self.assertEqual(hub.get_latest("file", "b.pdf")["data"]["fileName"], "b.pdf")

    def test_parse_progress_command_supports_legacy_subscribe(self):
        command = _parse_progress_command(
            {
                "businessType": "file",
                "params": [{"fileName": "a.pdf"}],
            }
        )

        self.assertEqual(command["action"], "subscribe")
        self.assertEqual(command["business_type"], "file")
        self.assertEqual(command["keys"], [("file", "a.pdf")])

    def test_parse_progress_command_supports_query(self):
        command = _parse_progress_command(
            {
                "action": "query",
                "businessType": "file",
                "params": [{"fileName": "a.pdf"}, {"fileName": "b.pdf"}],
            }
        )

        self.assertEqual(command["action"], "query")
        self.assertEqual(command["keys"], [("file", "a.pdf"), ("file", "b.pdf")])

    def test_legacy_progress_message_replays_snapshot_without_ack_when_repeated(self):
        with workspace_tempdir() as tmp:
            with LLMTaskService(db_path=f"{tmp}/tasks.sqlite3") as service:
                service.create_file_task(
                    "demo.pdf",
                    {"businessType": "file", "params": [{"fileName": "demo.pdf"}]},
                    status="1",
                )
                service.update_task_progress("file", "demo.pdf", progress=0.65, message="处理中", status="1")
                hub = LLMProgressHub()
                sent_messages = []
                subscriptions = {}
                command = _parse_progress_command(
                    {
                        "businessType": "file",
                        "params": [{"fileName": "demo.pdf"}],
                    }
                )

                with patch.object(llm_module, "task_service", service), patch.object(llm_module, "progress_hub", hub):
                    _handle_progress_command(sent_messages.append, subscriptions, command, emit_ack=False)
                    _handle_progress_command(sent_messages.append, subscriptions, command, emit_ack=False)

        self.assertEqual(
            sent_messages,
            [
                {"businessType": "file", "data": {"progress": 0.65, "fileName": "demo.pdf"}},
                {"businessType": "file", "data": {"progress": 0.65, "fileName": "demo.pdf"}},
            ],
        )

    @patch("app.services.llm_service.task_service.post_callback_payload", return_value=True)
    def test_check_task_replays_failed_callback(self, _mock_callback):
        with workspace_tempdir() as tmp:
            with LLMTaskService(db_path=f"{tmp}/tasks.sqlite3") as service:
                service.create_file_task("demo.pdf", {"businessType": "file"})
                service.mark_business_completed("file", "demo.pdf", {"fileName": "demo.pdf"}, status="2")
                service.mark_callback_failed("file", "demo.pdf", "timeout")
                replayed = service.replay_callback_if_needed("file", "demo.pdf", callback_url="http://callback.test/llm/callback", timeout=5)
                self.assertTrue(replayed)

    def test_batch_check_task_returns_data_array(self):
        app = create_app()
        client = app.test_client()

        with workspace_tempdir() as tmp:
            with LLMTaskService(db_path=f"{tmp}/tasks.sqlite3") as service:
                service.create_file_task("a.pdf", {"businessType": "file"}, status="1")
                service.create_file_task("b.pdf", {"businessType": "file"}, status="0")

                with patch("app.blueprints.llm.task_service", service):
                    response = client.post(
                        "/llm/check-task",
                        json={
                            "businessType": "file",
                            "params": [{"fileName": "a.pdf"}, {"fileName": "b.pdf"}],
                        },
                    )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIsInstance(payload["data"], list)
        self.assertEqual([item["fileName"] for item in payload["data"]], ["a.pdf", "b.pdf"])
