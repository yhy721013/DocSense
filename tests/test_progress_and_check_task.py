import unittest
from unittest.mock import patch

from app import create_app
from app.adapters.web.flask import (
    ProgressRequestValidationError,
    parse_progress_subscription,
)
from app.modules.tasks.application import ProgressDeliveryBuffer
from app.presenters.task_progress import ProgressWebSocketPresenter
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir
from tests.offline_application import build_offline_application_services


class LLMProgressAndCheckTaskTests(unittest.TestCase):
    def test_progress_hub_broadcasts_latest_message(self):
        hub = LLMProgressHub()
        sink = []
        hub.subscribe("file", "demo.pdf", sink.append)
        hub.publish("file", "demo.pdf", {"businessType": "file", "data": {"fileName": "demo.pdf", "progress": 0.35}})
        self.assertEqual(sink[-1]["data"]["progress"], 0.35)

    def test_progress_hub_normalizes_floating_point_artifacts(self):
        hub = LLMProgressHub()
        sink = []
        hub.subscribe("weaponry", "1001", sink.append)

        hub.publish(
            "weaponry",
            "1001",
            {
                "businessType": "weaponry",
                "data": {"architectureId": "1001", "progress": 0.28000000004},
            },
        )

        self.assertEqual(sink[-1]["data"]["progress"], 0.28)
        self.assertEqual(hub.get_latest("weaponry", "1001")["data"]["progress"], 0.28)

    def test_progress_hub_keeps_latest_message_per_task(self):
        hub = LLMProgressHub()
        hub.publish("file", "a.pdf", {"businessType": "file", "data": {"fileName": "a.pdf", "progress": 0.15}})
        hub.publish("file", "b.pdf", {"businessType": "file", "data": {"fileName": "b.pdf", "progress": 0.35}})

        self.assertEqual(hub.get_latest("file", "a.pdf")["data"]["fileName"], "a.pdf")
        self.assertEqual(hub.get_latest("file", "b.pdf")["data"]["fileName"], "b.pdf")

    def test_parse_progress_request_supports_no_action_subscribe(self):
        request_model = parse_progress_subscription(
            {
                "businessType": "file",
                "params": [{"fileName": "a.pdf"}],
            }
        )

        self.assertEqual(request_model.business_type, "file")
        self.assertEqual(
            [(item.business_type, item.business_key) for item in request_model.ordered_keys],
            [("file", "a.pdf")],
        )

    def test_parse_progress_request_rejects_explicit_query(self):
        with self.assertRaisesRegex(ProgressRequestValidationError, "action"):
            parse_progress_subscription(
                {
                    "action": "query",
                    "businessType": "file",
                    "params": [{"fileName": "a.pdf"}, {"fileName": "b.pdf"}],
                }
            )

    def test_no_action_progress_message_replays_snapshot_without_duplicate_subscription(self):
        with workspace_tempdir() as tmp:
            services = build_offline_application_services(tmp)
            service = services.task_service
            service.create_file_task(
                "demo.pdf",
                {"businessType": "file", "params": [{"fileName": "demo.pdf"}]},
                status="1",
            )
            service.update_task_progress("file", "demo.pdf", progress=0.65, message="处理中", status="1")
            request_model = parse_progress_subscription(
                {
                    "businessType": "file",
                    "params": [{"fileName": "demo.pdf"}],
                }
            )
            presenter = ProgressWebSocketPresenter()
            delivery = ProgressDeliveryBuffer(
                delivery_id="repeated-progress-message",
                capacity=16,
            )
            first = services.progress_subscription_service.subscribe(
                request_model,
                delivery=delivery,
            )
            first_messages = [
                presenter.present_current(item) for item in first.current_items
            ]
            first.complete_initial_delivery()
            second = services.progress_subscription_service.subscribe(
                request_model,
                delivery=delivery,
                existing_subscriptions=first.active_subscriptions,
            )
            second_messages = [
                presenter.present_current(item) for item in second.current_items
            ]
            second.complete_initial_delivery()
            services.progress_subscription_service.release(
                second.active_subscriptions,
            )

        self.assertEqual(
            first_messages + second_messages,
            [
                {"businessType": "file", "data": {"progress": 0.65, "fileName": "demo.pdf"}},
                {"businessType": "file", "data": {"progress": 0.65, "fileName": "demo.pdf"}},
            ],
        )

    def test_task_progress_is_normalized_when_persisted(self):
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            service.create_weaponry_task(
                1001,
                {"businessType": "weaponry", "params": {"architectureId": 1001}},
            )
            service.update_task_progress(
                "weaponry",
                "1001",
                progress=0.28000000004,
                message="处理中",
                status="1",
            )

            task = service.get_task("weaponry", "1001")

        self.assertEqual(task["progress"], 0.28)

    @patch("app.services.llm_service.task_service.post_callback_payload", return_value=True)
    def test_check_task_replays_failed_callback(self, _mock_callback):
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            service.create_file_task("demo.pdf", {"businessType": "file"})
            service.mark_business_completed("file", "demo.pdf", {"fileName": "demo.pdf"}, status="2")
            service.mark_callback_failed("file", "demo.pdf", "timeout")
            replayed = service.replay_callback_if_needed("file", "demo.pdf", callback_url="http://callback.test/llm/callback", timeout=5)
            self.assertTrue(replayed)

    def test_batch_check_task_returns_empty_success_body(self):
        """批量检查仍执行既有任务读取，但不再向调用方泄露状态快照。"""
        with workspace_tempdir() as tmp:
            services = build_offline_application_services(tmp)
            service = services.task_service
            service.create_file_task("a.pdf", {"businessType": "file"}, status="1")
            service.create_file_task("b.pdf", {"businessType": "file"}, status="0")

            app = create_app(services=services)
            client = app.test_client()
            response = client.post(
                "/llm/check-task",
                json={
                    "businessType": "file",
                    "params": [{"fileName": "a.pdf"}, {"fileName": "b.pdf"}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"")
