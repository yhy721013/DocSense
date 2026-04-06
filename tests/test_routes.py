import unittest
from unittest.mock import patch

from app import create_app
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


class LLMRouteValidationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        self._tempdir = workspace_tempdir()
        self.tmp = self._tempdir.__enter__()
        self.task_service = LLMTaskService(db_path=f"{self.tmp}/tasks.sqlite3")
        self.task_service_patcher = patch("app.blueprints.llm.task_service", self.task_service)
        self.task_service_patcher.start()

    def tearDown(self):
        self.task_service_patcher.stop()
        self._tempdir.__exit__(None, None, None)

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
    def test_analysis_accepts_multiple_files_and_starts_one_batch_thread(self, mock_thread):
        response = self.client.post(
            "/llm/analysis",
            json={
                "businessType": "file",
                "params": [
                    {
                        "fileName": "a.txt",
                        "filePath": "http://127.0.0.1:8000/a.txt",
                    },
                    {
                        "fileName": "b.txt",
                        "filePath": "http://127.0.0.1:8000/b.txt",
                    },
                ],
            },
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(response.get_json()["tasks"]), 2)
        mock_thread.assert_called_once()

    def test_analysis_rejects_duplicate_file_names_in_same_batch(self):
        response = self.client.post(
            "/llm/analysis",
            json={
                "businessType": "file",
                "params": [
                    {
                        "fileName": "dup.txt",
                        "filePath": "http://127.0.0.1:8000/a.txt",
                    },
                    {
                        "fileName": "dup.txt",
                        "filePath": "http://127.0.0.1:8000/b.txt",
                    },
                ],
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_analysis_rejects_when_task_is_already_in_progress(self):
        self.task_service.create_file_task("busy.txt", {"businessType": "file"}, status="1")
        response = self.client.post(
            "/llm/analysis",
            json={
                "businessType": "file",
                "params": [
                    {
                        "fileName": "busy.txt",
                        "filePath": "http://127.0.0.1:8000/busy.txt",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 409)

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

    @patch("app.blueprints.llm.kb_service")
    def test_reassign_rejects_invalid_business_type(self, mock_kb_service):
        response = self.client.post("/llm/reassign", json={"businessType": "wrong", "params": {}})
        self.assertEqual(response.status_code, 400)

    @patch("app.blueprints.llm.kb_service")
    def test_reassign_rejects_missing_params(self, mock_kb_service):
        response = self.client.post("/llm/reassign", json={"businessType": "reassign"})
        self.assertEqual(response.status_code, 400)

    @patch("app.blueprints.llm.kb_service")
    def test_reassign_rejects_same_architecture_id(self, mock_kb_service):
        response = self.client.post(
            "/llm/reassign",
            json={
                "businessType": "reassign",
                "params": {
                    "fileName": "a.pdf",
                    "oldArchitectureId": 1,
                    "newArchitectureId": 1
                }
            }
        )
        self.assertEqual(response.status_code, 400)

    @patch("app.blueprints.llm.kb_service")
    def test_reassign_returns_error_when_record_not_found(self, mock_kb_service):
        mock_kb_service.get_document_record.return_value = None
        response = self.client.post(
            "/llm/reassign",
            json={
                "businessType": "reassign",
                "params": {
                    "fileName": "a.pdf",
                    "oldArchitectureId": 1,
                    "newArchitectureId": 2
                }
            }
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data["data"]["success"])
        self.assertEqual(data["data"]["message"], "文档记录不存在")

    @patch("app.blueprints.llm.kb_service")
    def test_reassign_returns_error_when_inconsistent(self, mock_kb_service):
        mock_kb_service.get_document_record.return_value = {"architecture_id": 3}
        response = self.client.post(
            "/llm/reassign",
            json={
                "businessType": "reassign",
                "params": {
                    "fileName": "a.pdf",
                    "oldArchitectureId": 1,
                    "newArchitectureId": 2
                }
            }
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data["data"]["success"])
        self.assertIn("分类不一致", data["data"]["message"])
        mock_kb_service.update_document_architecture.assert_not_called()

    @patch("app.blueprints.llm.AnythingLLMClient")
    @patch("app.blueprints.llm.kb_service")
    def test_reassign_success(self, mock_kb_service, MockClient):
        mock_kb_service.get_document_record.return_value = {
            "architecture_id": 1,
            "doc_path": "custom-documents/test.pdf"
        }
        mock_kb_service.get_workspace_slug.side_effect = lambda x: "ws_old" if x == 1 else "ws_new"
        
        mock_client_instance = MockClient.return_value
        
        response = self.client.post(
            "/llm/reassign",
            json={
                "businessType": "reassign",
                "params": {
                    "fileName": "a.pdf",
                    "oldArchitectureId": 1,
                    "newArchitectureId": 2
                }
            }
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["data"]["success"])
        mock_kb_service.update_document_architecture.assert_called_once_with("a.pdf", 2)
        mock_client_instance.update_embeddings_batch.assert_called_once_with("ws_old", deletes=["custom-documents/test.pdf"], user_id=1)
        mock_client_instance.update_embeddings.assert_called_once_with("custom-documents/test.pdf", "ws_new", user_id=1, metadata={"file_name": "a.pdf", "architecture_id": 2})

    @patch("app.blueprints.llm.AnythingLLMClient")
    @patch("app.blueprints.llm.kb_service")
    def test_reassign_creates_workspace_if_missing(self, mock_kb_service, MockClient):
        mock_kb_service.get_document_record.return_value = {
            "architecture_id": 1,
            "doc_path": "custom-documents/test.pdf"
        }
        mock_kb_service.get_workspace_slug.side_effect = lambda x: "ws_old" if x == 1 else None
        
        mock_client_instance = MockClient.return_value
        mock_client_instance.create_rag_workspace.return_value = {"slug": "ws_created"}
        
        response = self.client.post(
            "/llm/reassign",
            json={
                "businessType": "reassign",
                "params": {
                    "fileName": "a.pdf",
                    "oldArchitectureId": 1,
                    "newArchitectureId": 2
                }
            }
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["data"]["success"])
        mock_kb_service.update_document_architecture.assert_called_once_with("a.pdf", 2)
        mock_client_instance.update_embeddings_batch.assert_called_once_with("ws_old", deletes=["custom-documents/test.pdf"], user_id=1)
        mock_client_instance.create_rag_workspace.assert_called_once_with("architectureId-2", user_id=1)
        mock_kb_service.add_workspace.assert_called_once_with(2, "ws_created")
        mock_client_instance.update_embeddings.assert_called_once_with("custom-documents/test.pdf", "ws_created", user_id=1, metadata={"file_name": "a.pdf", "architecture_id": 2})

