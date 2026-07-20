import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from app import create_app
from app.container import APPLICATION_SERVICES_EXTENSION
from app.services.llm_service.analysis_service import (
    MAX_ANALYSIS_PARAMS_PER_REQUEST,
    MAX_ANALYSIS_REQUEST_BYTES,
)
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


class LLMRouteValidationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        self._tempdir = workspace_tempdir()
        self.tmp = self._tempdir.__enter__()
        self.task_service = LLMTaskService(db_path=f"{self.tmp}/tasks.sqlite3")
        self.kb_service = MagicMock()
        services = self.app.extensions[APPLICATION_SERVICES_EXTENSION]
        self.app.extensions[APPLICATION_SERVICES_EXTENSION] = replace(
            services,
            task_service=self.task_service,
            kb_service=self.kb_service,
        )

    def tearDown(self):
        self._tempdir.__exit__(None, None, None)

    def test_analysis_rejects_invalid_business_type(self):
        response = self.client.post("/llm/analysis", json={"businessType": "wrong", "params": [{}]})
        self.assertEqual(response.status_code, 400)

    def test_analysis_rejects_non_object_json_root(self):
        for payload in (["file"], [], False, 1):
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/llm/analysis",
                    json=payload,
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.get_json()["error"],
                    "请求JSON必须是对象",
                )

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
        response_task = response.get_json()["task"]
        worker_kwargs = mock_thread.call_args.kwargs["kwargs"]
        self.assertEqual(
            worker_kwargs["execution_id"],
            response_task["execution_id"],
        )

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
        response_tasks = response.get_json()["tasks"]
        self.assertEqual(len(response_tasks), 2)
        mock_thread.assert_called_once()
        worker_kwargs = mock_thread.call_args.kwargs["kwargs"]
        self.assertEqual(
            worker_kwargs["execution_ids"],
            {
                task["business_key"]: task["execution_id"]
                for task in response_tasks
            },
        )

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
    def test_analysis_rejects_terminal_task_while_callback_is_pending(
        self,
        mock_thread,
    ):
        previous = self.task_service.create_file_task(
            "callback-window.txt",
            {"businessType": "file", "marker": "previous"},
        )
        self.task_service.mark_business_result(
            "file",
            "callback-window.txt",
            {"status": "2", "marker": "previous-result"},
            status="2",
            execution_id=previous["execution_id"],
        )

        response = self.client.post(
            "/llm/analysis",
            json={
                "businessType": "file",
                "params": [
                    {
                        "fileName": "callback-window.txt",
                        "filePath": (
                            "http://127.0.0.1:8000/"
                            "callback-window.txt"
                        ),
                    }
                ],
            },
        )

        current = self.task_service.get_task(
            "file",
            "callback-window.txt",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["error"],
            "上一次任务回调尚未结束",
        )
        self.assertEqual(
            current["execution_id"],
            previous["execution_id"],
        )
        self.assertEqual(
            current["result_payload"]["marker"],
            "previous-result",
        )
        mock_thread.assert_not_called()

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_rejects_failed_task_while_replay_lease_is_active(
        self,
        mock_thread,
    ):
        previous = self.task_service.create_file_task(
            "callback-replay.txt",
            {"businessType": "file", "marker": "previous"},
        )
        self.task_service.mark_business_result(
            "file",
            "callback-replay.txt",
            {"status": "2", "marker": "previous-result"},
            status="2",
            execution_id=previous["execution_id"],
        )
        self.task_service.mark_callback_failed(
            "file",
            "callback-replay.txt",
            "first callback failed",
            execution_id=previous["execution_id"],
        )
        self.assertIsNotNone(
            self.task_service.claim_callback_delivery(
                "file",
                "callback-replay.txt",
                timeout=5,
                execution_id=previous["execution_id"],
            )
        )

        response = self.client.post(
            "/llm/analysis",
            json={
                "businessType": "file",
                "params": [
                    {
                        "fileName": "callback-replay.txt",
                        "filePath": (
                            "http://127.0.0.1:8000/"
                            "callback-replay.txt"
                        ),
                    }
                ],
            },
        )

        current = self.task_service.get_task(
            "file",
            "callback-replay.txt",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["error"],
            "上一次任务回调尚未结束",
        )
        self.assertEqual(
            current["execution_id"],
            previous["execution_id"],
        )
        self.assertEqual(
            current["result_payload"]["marker"],
            "previous-result",
        )
        mock_thread.assert_not_called()

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_keeps_missing_null_and_empty_architecture_compatibility(
        self,
        mock_thread,
    ):
        for index, architecture_value in enumerate((None, []), start=1):
            params = {
                "fileName": f"compat-{index}.txt",
                "filePath": f"http://127.0.0.1:8000/compat-{index}.txt",
                "architectureList": architecture_value,
            }
            response = self.client.post(
                "/llm/analysis",
                json={"businessType": "file", "params": [params]},
            )
            self.assertEqual(response.status_code, 202)

        self.assertEqual(mock_thread.call_count, 2)

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_rejects_explicitly_malformed_architecture_ranges(
        self,
        mock_thread,
    ):
        invalid_ranges = (
            {"architectureList": {}},
            {"architectureList": [{}]},
            {
                "architectureList": [
                    {"id": 1, "name": "节点甲"},
                    {"id": 1, "name": "节点乙"},
                ]
            },
            {"architectureStandardList": {}},
            {"architectureStandardList": [{}]},
        )
        for index, invalid_range in enumerate(invalid_ranges, start=1):
            with self.subTest(invalid_range=invalid_range):
                file_name = f"invalid-tree-{index}.txt"
                response = self.client.post(
                    "/llm/analysis",
                    json={
                        "businessType": "file",
                        "params": [
                            {
                                "fileName": file_name,
                                "filePath": (
                                    "http://127.0.0.1:8000/"
                                    f"{file_name}"
                                ),
                                **invalid_range,
                            }
                        ],
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertIsNone(
                    self.task_service.get_task("file", file_name)
                )

        mock_thread.assert_not_called()

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_rejects_invalid_unicode_and_non_finite_json(
        self,
        mock_thread,
    ):
        invalid_values = (
            {"architectureList": [{"id": 1, "name": "\ud800"}]},
            {
                "architectureList": [
                    {
                        "id": 1,
                        "name": "节点",
                        "extension": "\ud800",
                    }
                ]
            },
            {"enableFullTranslation": float("nan")},
            {"enableFullTranslation": float("inf")},
        )
        for index, invalid_value in enumerate(invalid_values, start=1):
            with self.subTest(invalid_value=invalid_value):
                file_name = f"invalid-json-{index}.txt"
                response = self.client.post(
                    "/llm/analysis",
                    json={
                        "businessType": "file",
                        "params": [
                            {
                                "fileName": file_name,
                                "filePath": (
                                    "http://127.0.0.1:8000/"
                                    f"{file_name}"
                                ),
                                **invalid_value,
                            }
                        ],
                    },
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.get_json()["error"],
                    "请求JSON包含非法数值或Unicode字符",
                )
                self.assertIsNone(
                    self.task_service.get_task("file", file_name)
                )

        mock_thread.assert_not_called()

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_validates_every_batch_item_before_creating_tasks(
        self,
        mock_thread,
    ):
        response = self.client.post(
            "/llm/analysis",
            json={
                "businessType": "file",
                "params": [
                    {
                        "fileName": "valid-first.txt",
                        "filePath": (
                            "http://127.0.0.1:8000/valid-first.txt"
                        ),
                        "architectureList": [
                            {"id": 1, "name": "合法节点"}
                        ],
                    },
                    {
                        "fileName": "invalid-second.txt",
                        "filePath": (
                            "http://127.0.0.1:8000/invalid-second.txt"
                        ),
                        "architectureList": [{}],
                    },
                ],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(
            self.task_service.get_task("file", "valid-first.txt")
        )
        self.assertIsNone(
            self.task_service.get_task("file", "invalid-second.txt")
        )
        mock_thread.assert_not_called()

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_rejects_overdeep_tree_before_task_creation(
        self,
        mock_thread,
    ):
        architecture_list = [
            {
                "id": depth,
                "name": f"层级{depth}",
                "parentId": depth - 1 if depth > 1 else None,
            }
            for depth in range(1, 130)
        ]

        response = self.client.post(
            "/llm/analysis",
            json={
                "businessType": "file",
                "params": [
                    {
                        "fileName": "overdeep.txt",
                        "filePath": (
                            "http://127.0.0.1:8000/overdeep.txt"
                        ),
                        "architectureList": architecture_list,
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("可见深度不能超过", response.get_json()["error"])
        self.assertIsNone(
            self.task_service.get_task("file", "overdeep.txt")
        )
        mock_thread.assert_not_called()

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_rejects_non_object_params_without_silent_filtering(
        self,
        mock_thread,
    ):
        response = self.client.post(
            "/llm/analysis",
            json={
                "businessType": "file",
                "params": [
                    {
                        "fileName": "valid.txt",
                        "filePath": "http://127.0.0.1:8000/valid.txt",
                    },
                    "not-an-object",
                ],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("params[1]必须是对象", response.get_json()["error"])
        self.assertIsNone(
            self.task_service.get_task("file", "valid.txt")
        )
        mock_thread.assert_not_called()

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_rejects_too_many_files_before_task_creation(
        self,
        mock_thread,
    ):
        params = [
            {
                "fileName": f"batch-{index}.txt",
                "filePath": (
                    f"http://127.0.0.1:8000/batch-{index}.txt"
                ),
            }
            for index in range(MAX_ANALYSIS_PARAMS_PER_REQUEST + 1)
        ]

        response = self.client.post(
            "/llm/analysis",
            json={"businessType": "file", "params": params},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            str(MAX_ANALYSIS_PARAMS_PER_REQUEST),
            response.get_json()["error"],
        )
        self.assertIsNone(
            self.task_service.get_task("file", "batch-0.txt")
        )
        mock_thread.assert_not_called()

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_rejects_oversized_content_length_before_parsing(
        self,
        mock_thread,
    ):
        response = self.client.post(
            "/llm/analysis",
            data=b"{}",
            content_type="application/json",
            environ_overrides={
                "CONTENT_LENGTH": str(MAX_ANALYSIS_REQUEST_BYTES + 1)
            },
        )

        self.assertEqual(response.status_code, 413)
        mock_thread.assert_not_called()

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_bounds_chunked_body_without_content_length(
        self,
        mock_thread,
    ):
        with patch(
            "app.blueprints.llm.MAX_ANALYSIS_REQUEST_BYTES",
            8,
        ):
            response = self.client.post(
                "/llm/analysis",
                data=b'{"payload":"too-large"}',
                content_type="application/json",
                environ_overrides={
                    "CONTENT_LENGTH": None,
                    "wsgi.input_terminated": True,
                },
            )

        self.assertEqual(response.status_code, 413)
        mock_thread.assert_not_called()

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
        self.assertEqual(response.status_code, 200)
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
        self.assertEqual(response.status_code, 200)
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
        self.kb_service.update_document_architecture.assert_called_once_with("a.pdf", 2)
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
        self.kb_service.update_document_architecture.assert_called_once_with("a.pdf", 2)
        mock_client_instance.update_embeddings_batch.assert_called_once_with("ws_old", deletes=["custom-documents/test.pdf"], user_id=1)
        mock_client_instance.create_rag_workspace.assert_called_once_with("architectureId-2", user_id=1)
        self.kb_service.add_workspace.assert_called_once_with(2, "ws_created")
        mock_client_instance.update_embeddings.assert_called_once_with("custom-documents/test.pdf", "ws_created", user_id=1, metadata={"file_name": "a.pdf", "architecture_id": 2})
