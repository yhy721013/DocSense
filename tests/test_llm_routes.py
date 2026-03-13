import unittest
from unittest.mock import patch

from app import create_app


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
