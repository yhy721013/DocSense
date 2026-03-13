import tempfile
import unittest
from unittest.mock import call, patch

from app import create_app
from app.services.llm_task_service import LLMTaskService


class LLMRouteValidationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def test_analysis_rejects_invalid_business_type(self):
        response = self.client.post("/llm/analysis", json={"businessType": "wrong", "params": [{}]})
        self.assertEqual(response.status_code, 400)

    def test_generate_report_rejects_missing_params(self):
        response = self.client.post("/llm/generate-report", json={"businessType": "report"})
        self.assertEqual(response.status_code, 400)

    def test_progress_route_is_registered(self):
        response = self.client.get("/llm/progress")
        self.assertNotEqual(response.status_code, 404)

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_starts_background_task_for_valid_request(self, mock_thread):
        response = self.client.post(
            "/llm/analysis",
            json={
                "businessType": "file",
                "params": [
                    {
                        "fileName": "sample.txt",
                        "filePath": "http://127.0.0.1:8000/sample.txt",
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 202)
        mock_thread.assert_called_once()

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_starts_one_thread_per_file(self, mock_thread):
        """多文件上传时每个文件启动一个独立的后台线程"""
        response = self.client.post(
            "/llm/analysis",
            json={
                "businessType": "file",
                "params": [
                    {"fileName": "file1.txt", "filePath": "http://127.0.0.1:8000/file1.txt"},
                    {"fileName": "file2.txt", "filePath": "http://127.0.0.1:8000/file2.txt"},
                    {"fileName": "file3.txt", "filePath": "http://127.0.0.1:8000/file3.txt"},
                ],
            },
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(mock_thread.call_count, 3)
        data = response.get_json()
        self.assertIn("tasks", data)
        self.assertEqual(len(data["tasks"]), 3)

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_skips_invalid_params_and_accepts_valid(self, mock_thread):
        """params中无效条目被跳过，有效条目仍然被处理"""
        response = self.client.post(
            "/llm/analysis",
            json={
                "businessType": "file",
                "params": [
                    {"fileName": "", "filePath": "http://127.0.0.1:8000/file.txt"},
                    {"fileName": "valid.txt", "filePath": "http://127.0.0.1:8000/valid.txt"},
                ],
            },
        )
        self.assertEqual(response.status_code, 202)
        mock_thread.assert_called_once()

    def test_analysis_rejects_all_invalid_params(self):
        """所有params条目无效时返回400"""
        response = self.client.post(
            "/llm/analysis",
            json={
                "businessType": "file",
                "params": [
                    {"fileName": "", "filePath": ""},
                ],
            },
        )
        self.assertEqual(response.status_code, 400)

    @patch("app.blueprints.llm.threading.Thread")
    def test_generate_report_starts_background_task_for_valid_request(self, mock_thread):
        response = self.client.post(
            "/llm/generate-report",
            json={
                "businessType": "report",
                "params": [
                    {
                        "reportId": 132,
                        "filePathList": ["http://127.0.0.1:8000/sample.txt"],
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 202)
        mock_thread.assert_called_once()

    def test_check_task_returns_batch_results(self):
        """check-task 支持批量查询多个文件任务"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/tasks.sqlite3"
            service = LLMTaskService(db_path=db_path)
            service.create_file_task("a.txt", {"businessType": "file"})
            service.create_file_task("b.txt", {"businessType": "file"})

            import app.blueprints.llm as llm_module
            original_service = llm_module.task_service
            llm_module.task_service = service
            try:
                response = self.client.post(
                    "/llm/check-task",
                    json={
                        "businessType": "file",
                        "params": [
                            {"fileName": "a.txt"},
                            {"fileName": "b.txt"},
                        ],
                    },
                )
            finally:
                llm_module.task_service = original_service

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["businessType"], "file")
        self.assertEqual(len(data["data"]), 2)
        file_names = {item["fileName"] for item in data["data"]}
        self.assertEqual(file_names, {"a.txt", "b.txt"})

    def test_check_task_returns_404_when_no_tasks_found(self):
        """check-task 所有任务不存在时返回404"""
        response = self.client.post(
            "/llm/check-task",
            json={
                "businessType": "file",
                "params": [{"fileName": "nonexistent.txt"}],
            },
        )
        self.assertEqual(response.status_code, 404)
