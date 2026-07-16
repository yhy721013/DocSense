import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from app import create_app
from tests import workspace_tempdir
from tests.offline_application import build_offline_application_services


class LLMRouteValidationTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = workspace_tempdir()
        self.tmp = self._tempdir.__enter__()
        self.kb_service = MagicMock()
        offline_services = build_offline_application_services(self.tmp)
        self.task_service = offline_services.task_service
        self.progress_hub = offline_services.progress_hub
        services = replace(
            offline_services,
            kb_service=self.kb_service,
        )
        # 路由测试必须显式注入离线容器。禁止调用无参 ``create_app()``，否则会
        # 读取生产配置并初始化共享运行目录，既拖慢测试，也破坏阶段 0 的隔离基线。
        self.app = create_app(services=services)
        self.client = self.app.test_client()

    def tearDown(self):
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
                        "templateOutline": "http://127.0.0.1:8000/template.docx",
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 202)
        mock_thread.assert_called_once()

    @patch("app.blueprints.llm.threading.Thread")
    def test_generate_report_normalizes_unbounded_integer_string_id(self, mock_thread):
        report_id = 10**80 + 132

        response = self.client.post(
            "/llm/generate-report",
            json={
                "businessType": "report",
                "params": [
                    {
                        "reportId": f"+000{report_id}",
                        "filePathList": ["http://127.0.0.1:8000/sample.txt"],
                        "templateOutline": "http://127.0.0.1:8000/template.docx",
                    }
                ],
            },
        )

        self.assertEqual(202, response.status_code)
        self.assertIsNotNone(self.task_service.get_task("report", str(report_id)))
        latest = self.progress_hub.get_latest("report", str(report_id))
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(report_id, latest["data"]["reportId"])
        worker_payload = mock_thread.call_args.kwargs["kwargs"]["request_payload"]
        normalized_value = worker_payload["params"][0]["reportId"]
        self.assertEqual(report_id, normalized_value)
        self.assertIs(int, type(normalized_value))

    def test_generate_report_rejects_non_integer_report_id(self):
        for invalid in (True, 132.0, "132.0", "not-an-integer"):
            with self.subTest(invalid=invalid):
                response = self.client.post(
                    "/llm/generate-report",
                    json={
                        "businessType": "report",
                        "params": [
                            {
                                "reportId": invalid,
                                "filePathList": [
                                    "http://127.0.0.1:8000/sample.txt"
                                ],
                                "templateOutline": (
                                    "http://127.0.0.1:8000/template.docx"
                                ),
                            }
                        ],
                    },
                )

                self.assertEqual(400, response.status_code)
                self.assertEqual(
                    {"error": "reportId必须是整数或整数字符串"},
                    response.get_json(),
                )

    def test_generate_report_rejects_missing_template_outline(self):
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

        self.assertEqual(response.status_code, 400)

    @patch("app.blueprints.llm.threading.Thread")
    def test_weaponry_normalizes_selected_file_urls_and_preserves_original_request(
        self,
        mock_thread,
    ):
        # 选中文件来自其他分类时也应允许提交；architectureId 只描述结果归属，不再
        # 限制非空 filePathList 的来源类别。
        self.kb_service.list_document_records.return_value = [
            {
                "file_name": "abc123.pdf",
                "original_name": "跨分类来源.pdf",
                "ingested_file_name": "abc123.pdf",
                "architecture_id": 99999,
                "doc_path": "custom-documents/abc123.json",
            }
        ]
        original_file_paths = [
            "https://host/download/abc%31%32%33.pdf?token=secret#page=2",
            "abc123.pdf",
        ]

        response = self.client.post(
            "/llm/weaponry",
            json={
                "businessType": "weaponry",
                "params": {
                    "architectureId": 10502,
                    "filePathList": original_file_paths,
                    "weaponryTemplateFieldList": [
                        {"fieldName": "舰级名称", "fieldType": "INPUT"}
                    ],
                },
            },
        )

        self.assertEqual(response.status_code, 202)
        self.kb_service.list_document_records.assert_called_once_with()
        worker_kwargs = mock_thread.call_args.kwargs["kwargs"]
        self.assertEqual(len(worker_kwargs["selected_documents"]), 1)
        selected_document = worker_kwargs["selected_documents"][0]
        self.assertEqual(selected_document.file_name, "abc123.pdf")
        self.assertEqual(selected_document.source_architecture_id, 99999)
        self.assertEqual(selected_document.doc_path, "custom-documents/abc123.json")
        self.assertEqual(selected_document.ingested_file_name, "abc123.pdf")
        task = self.task_service.get_task("weaponry", "10502")
        self.assertEqual(task["request_payload"]["params"]["filePathList"], original_file_paths)
        self.assertEqual(worker_kwargs["execution_id"], task["execution_id"])
        self.assertEqual(
            self.task_service.get_weaponry_task_document_snapshots(
                architecture_id=10502,
                execution_id=task["execution_id"],
            ),
            [
                {
                    "file_name": "abc123.pdf",
                    "original_name": "跨分类来源.pdf",
                    "ingested_file_name": "abc123.pdf",
                    "source_architecture_id": 99999,
                    "doc_path": "custom-documents/abc123.json",
                    "anything_doc_id": "",
                }
            ],
        )

    @patch("app.blueprints.llm.threading.Thread")
    def test_weaponry_empty_file_path_list_keeps_full_category_scope(
        self,
        mock_thread,
    ):
        response = self.client.post(
            "/llm/weaponry",
            json={
                "businessType": "weaponry",
                "params": {
                    "architectureId": 10502,
                    "filePathList": [],
                    "weaponryTemplateFieldList": [
                        {"fieldName": "舰级名称", "fieldType": "INPUT"}
                    ],
                },
            },
        )

        self.assertEqual(response.status_code, 202)
        self.kb_service.list_document_records.assert_not_called()
        self.assertEqual(mock_thread.call_args.kwargs["kwargs"]["selected_documents"], ())

    def test_weaponry_rejects_invalid_file_path_list_type(self):
        response = self.client.post(
            "/llm/weaponry",
            json={
                "businessType": "weaponry",
                "params": {
                    "architectureId": 10502,
                    "filePathList": "abc123.pdf",
                    "weaponryTemplateFieldList": [
                        {"fieldName": "舰级名称", "fieldType": "INPUT"}
                    ],
                },
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("filePathList必须为数组", response.get_json()["error"])

    def test_weaponry_rejects_unknown_selected_file(self):
        self.kb_service.list_document_records.return_value = []
        response = self.client.post(
            "/llm/weaponry",
            json={
                "businessType": "weaponry",
                "params": {
                    "architectureId": 10502,
                    "filePathList": ["missing.pdf"],
                    "weaponryTemplateFieldList": [
                        {"fieldName": "舰级名称", "fieldType": "INPUT"}
                    ],
                },
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn("尚未解析", response.get_json()["error"])

    def test_weaponry_rejects_ambiguous_file_name_across_categories(self):
        """同名记录无法由既有请求字段消歧，不能随机选择任一分类。"""
        self.kb_service.list_document_records.return_value = [
            {
                "file_name": "abc123.pdf",
                "architecture_id": 99999,
                "doc_path": "custom-documents/abc123-v1.json",
            },
            {
                "file_name": "abc123.pdf",
                "architecture_id": 88888,
                "doc_path": "custom-documents/abc123-v2.json",
            },
        ]
        response = self.client.post(
            "/llm/weaponry",
            json={
                "businessType": "weaponry",
                "params": {
                    "architectureId": 10502,
                    "filePathList": ["abc123.pdf"],
                    "weaponryTemplateFieldList": [
                        {"fieldName": "舰级名称", "fieldType": "INPUT"}
                    ],
                },
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("无法唯一", response.get_json()["error"])
        self.assertIsNone(self.task_service.get_task("weaponry", "10502"))

    def test_reassign_rejects_invalid_business_type(self):
        response = self.client.post("/llm/reassign", json={"businessType": "wrong", "params": {}})
        self.assertEqual(response.status_code, 400)

    def test_reassign_rejects_missing_params(self):
        response = self.client.post("/llm/reassign", json={"businessType": "reassign"})
        self.assertEqual(response.status_code, 400)

    def test_reassign_rejects_same_architecture_id(self):
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

    def test_reassign_returns_error_when_record_not_found(self):
        self.kb_service.get_document_record.return_value = None
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
        self.assertEqual(response.status_code, 500)
        data = response.get_json()
        self.assertFalse(data["data"]["success"])
        self.assertEqual(data["data"]["message"], "文档记录不存在")

    def test_reassign_returns_error_when_inconsistent(self):
        self.kb_service.get_document_record.return_value = {"architecture_id": 3}
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
        self.assertEqual(response.status_code, 500)
        data = response.get_json()
        self.assertFalse(data["data"]["success"])
        self.assertIn("分类不一致", data["data"]["message"])
        self.kb_service.update_document_architecture.assert_not_called()

    @patch("app.blueprints.llm.AnythingLLMClient")
    def test_reassign_success(self, MockClient):
        self.kb_service.get_document_record.return_value = {
            "architecture_id": 1,
            "doc_path": "custom-documents/test.pdf"
        }
        self.kb_service.get_workspace_slug.side_effect = (
            lambda value: "ws_old" if value == 1 else "ws_new"
        )
        
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
        self.kb_service.update_document_architecture.assert_called_once_with(
            "a.pdf",
            2,
            current_architecture_id=1,
        )
        mock_client_instance.update_embeddings_batch.assert_called_once_with("ws_old", deletes=["custom-documents/test.pdf"], user_id=1)
        mock_client_instance.update_embeddings.assert_called_once_with("custom-documents/test.pdf", "ws_new", user_id=1, metadata={"file_name": "a.pdf", "architecture_id": 2})

    @patch("app.blueprints.llm.AnythingLLMClient")
    def test_reassign_creates_workspace_if_missing(self, MockClient):
        self.kb_service.get_document_record.return_value = {
            "architecture_id": 1,
            "doc_path": "custom-documents/test.pdf"
        }
        self.kb_service.get_workspace_slug.side_effect = (
            lambda value: "ws_old" if value == 1 else None
        )
        
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
        self.kb_service.update_document_architecture.assert_called_once_with(
            "a.pdf",
            2,
            current_architecture_id=1,
        )
        mock_client_instance.update_embeddings_batch.assert_called_once_with("ws_old", deletes=["custom-documents/test.pdf"], user_id=1)
        mock_client_instance.create_rag_workspace.assert_called_once_with("architectureId-2", user_id=1)
        self.kb_service.add_workspace.assert_called_once_with(2, "ws_created")
        mock_client_instance.update_embeddings.assert_called_once_with("custom-documents/test.pdf", "ws_created", user_id=1, metadata={"file_name": "a.pdf", "architecture_id": 2})

