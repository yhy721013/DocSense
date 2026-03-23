import unittest

from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


class LLMTaskServiceTests(unittest.TestCase):
    def test_create_file_task_defaults_to_processing(self):
        with workspace_tempdir() as tmp:
            with LLMTaskService(db_path=f"{tmp}/tasks.sqlite3") as service:
                task = service.create_file_task(file_name="demo.pdf", request_payload={"businessType": "file"})
                self.assertEqual(task["business_key"], "demo.pdf")
                self.assertEqual(task["status"], "1")
                self.assertEqual(task["callback_status"], "pending")

    def test_create_file_task_can_start_as_pending(self):
        with workspace_tempdir() as tmp:
            with LLMTaskService(db_path=f"{tmp}/tasks.sqlite3") as service:
                task = service.create_file_task(
                    file_name="demo-2.pdf",
                    request_payload={"businessType": "file"},
                    status="0",
                )
                self.assertEqual(task["status"], "0")
                self.assertEqual(task["progress"], 0.0)

    def test_get_tasks_returns_snapshots_in_request_order(self):
        with workspace_tempdir() as tmp:
            with LLMTaskService(db_path=f"{tmp}/tasks.sqlite3") as service:
                service.create_file_task("a.pdf", {"businessType": "file"}, status="1")
                service.create_file_task("b.pdf", {"businessType": "file"}, status="0")

                tasks = service.get_tasks("file", ["a.pdf", "b.pdf"])

                self.assertEqual([item["business_key"] for item in tasks], ["a.pdf", "b.pdf"])

    def test_completed_task_with_failed_callback_should_replay(self):
        with workspace_tempdir() as tmp:
            with LLMTaskService(db_path=f"{tmp}/tasks.sqlite3") as service:
                service.create_report_task(report_id=7, request_payload={"businessType": "report"})
                service.mark_business_completed("report", "7", {"details": "<div>ok</div>"}, status="1")
                service.mark_callback_failed("report", "7", "timeout")
                self.assertTrue(service.should_replay_callback("report", "7"))
