import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import MagicMock, patch

from app import create_app
from app.modules.weaponry.domain import (
    DOCUMENT_SCOPE_CATEGORY,
    DOCUMENT_SCOPE_EXPLICIT,
    WeaponryDocumentScope,
    WeaponryDocumentSnapshot,
)
from app.modules.weaponry.ports import (
    WeaponryDocumentScopeAmbiguityError,
    WeaponryDocumentScopeNotFoundError,
)
from tests import workspace_tempdir
from tests.fakes import FakeWeaponryDocumentScopePort
from tests.offline_application import build_offline_application_services


class LLMRouteValidationTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = workspace_tempdir()
        self.tmp = self._tempdir.__enter__()
        self.kb_service = MagicMock()
        offline_services = build_offline_application_services(self.tmp)
        self.task_service = offline_services.task_service
        self.progress_hub = offline_services.progress_hub
        self.weaponry_document_scope = FakeWeaponryDocumentScopePort()
        weaponry_services = replace(
            offline_services.weaponry_services,
            document_scope=self.weaponry_document_scope,
        )
        services = replace(
            offline_services,
            kb_service=self.kb_service,
            weaponry_services=weaponry_services,
        )
        self.services = services
        # 路由测试必须显式注入离线容器。禁止调用无参 ``create_app()``，否则会
        # 读取生产配置并初始化共享运行目录，既拖慢测试，也破坏阶段 0 的隔离基线。
        self.app = create_app(services=services)
        self.client = self.app.test_client()

    @staticmethod
    def _weaponry_document(
        *,
        file_name: str = "abc123.pdf",
        original_name: str = "跨分类来源.pdf",
        source_architecture_id: int = 99999,
        external_document_ref: str = "custom-documents/abc123.json",
    ) -> WeaponryDocumentSnapshot:
        """构造受理时冻结的文档身份，避免路由测试依赖真实知识库。"""

        return WeaponryDocumentSnapshot(
            sequence_no=1,
            document_key=f"document:{source_architecture_id}:{file_name}",
            file_name=file_name,
            original_name=original_name,
            ingested_file_name=file_name,
            source_architecture_id=source_architecture_id,
            external_document_ref=external_document_ref,
        )

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
    def test_generate_report_persists_and_wakes_without_route_thread(self, mock_thread):
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
        self.assertEqual(b"", response.get_data())
        mock_thread.assert_not_called()
        self.assertEqual(1, len(self.services.report_dispatcher.task_ids))

    def test_generate_report_normalizes_large_integer_string_id(self):
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
        public_task = self.task_service.get_task("report", str(report_id))
        execution = self.task_service.get_task_execution(
            public_task["execution_id"]
        )
        normalized_value = execution["input_payload"]["public_report_id"]
        self.assertEqual(report_id, normalized_value)
        self.assertIs(int, type(normalized_value))

    def test_generate_report_rejects_more_than_128_report_id_digits(self):
        response = self.client.post(
            "/llm/generate-report",
            json={
                "businessType": "report",
                "params": [
                    {
                        "reportId": "9" * 129,
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
            {"error": "reportId不能超过128位十进制数字"},
            response.get_json(),
        )

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
        # 限制非空 filePathList 的来源类别。文档解析由 DocumentScope Port 负责，
        # Web 路由不得重新访问 DatabaseService 或自行拼装 Worker 参数。
        original_file_paths = [
            "https://host/download/abc%31%32%33.pdf?token=secret#page=2",
            "abc123.pdf",
        ]
        selected_document = self._weaponry_document()
        self.weaponry_document_scope.scopes[(10502, ("abc123.pdf",))] = (
            WeaponryDocumentScope(
                mode=DOCUMENT_SCOPE_EXPLICIT,
                requested_file_names=("abc123.pdf",),
                documents=(selected_document,),
            )
        )

        response = self.client.post(
            "/llm/weaponry",
            json={
                "businessType": "weaponry",
                "params": {
                    "architectureId": 10502,
                    "filePathList": original_file_paths,
                    "weaponryTemplateFieldList": [
                        {
                            "templateClassifyId": 7001,
                            "fieldName": "舰级名称",
                            "fieldType": "INPUT",
                        }
                    ],
                },
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_data(), b"")
        mock_thread.assert_not_called()
        self.kb_service.list_document_records.assert_not_called()
        self.assertEqual(
            self.weaponry_document_scope.calls,
            [(10502, ("abc123.pdf",))],
        )
        task = self.task_service.get_task("weaponry", "10502")
        self.assertEqual(task["request_payload"]["params"]["filePathList"], original_file_paths)
        execution = self.task_service.get_task_execution(task["execution_id"])
        frozen_scope = execution["input_payload"]["document_scope"]
        self.assertEqual(frozen_scope["mode"], DOCUMENT_SCOPE_EXPLICIT)
        self.assertEqual(frozen_scope["requested_file_names"], ["abc123.pdf"])
        self.assertEqual(frozen_scope["documents"][0]["file_name"], "abc123.pdf")
        self.assertEqual(frozen_scope["documents"][0]["source_architecture_id"], 99999)

    @patch("app.blueprints.llm.threading.Thread")
    def test_weaponry_empty_file_path_list_keeps_full_category_scope(
        self,
        mock_thread,
    ):
        self.weaponry_document_scope.scopes[(10502, ())] = WeaponryDocumentScope(
            mode=DOCUMENT_SCOPE_CATEGORY,
            requested_file_names=(),
            documents=(),
        )
        response = self.client.post(
            "/llm/weaponry",
            json={
                "businessType": "weaponry",
                "params": {
                    "architectureId": 10502,
                    "filePathList": [],
                    "weaponryTemplateFieldList": [
                        {
                            "templateClassifyId": 7001,
                            "fieldName": "舰级名称",
                            "fieldType": "INPUT",
                        }
                    ],
                },
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_data(), b"")
        mock_thread.assert_not_called()
        self.kb_service.list_document_records.assert_not_called()
        self.assertEqual(self.weaponry_document_scope.calls, [(10502, ())])

    def test_weaponry_rejects_invalid_file_path_list_type(self):
        response = self.client.post(
            "/llm/weaponry",
            json={
                "businessType": "weaponry",
                "params": {
                    "architectureId": 10502,
                    "filePathList": "abc123.pdf",
                    "weaponryTemplateFieldList": [
                        {
                            "templateClassifyId": 7001,
                            "fieldName": "舰级名称",
                            "fieldType": "INPUT",
                        }
                    ],
                },
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("filePathList必须为数组", response.get_json()["error"])

    def test_weaponry_route_rejects_unapproved_architecture_id_forms(self):
        for invalid in (True, 1.0, " 1 ", "+1", 0, -1, [], {}):
            with self.subTest(invalid=invalid):
                response = self.client.post(
                    "/llm/weaponry",
                    json={
                        "businessType": "weaponry",
                        "params": {
                            "architectureId": invalid,
                            "filePathList": [],
                            "weaponryTemplateFieldList": [
                                {
                                    "templateClassifyId": 7001,
                                    "fieldName": "舰级名称",
                                    "fieldType": "INPUT",
                                }
                            ],
                        },
                    },
                )

                self.assertEqual(400, response.status_code)
                self.assertEqual(
                    {
                        "error": "architectureId必须为1到9223372036854775807之间的正整数"
                    },
                    response.get_json(),
                )

    def test_weaponry_rejects_unknown_selected_file(self):
        self.weaponry_document_scope.errors[(10502, ("missing.pdf",))] = (
            WeaponryDocumentScopeNotFoundError("文件尚未解析")
        )
        response = self.client.post(
            "/llm/weaponry",
            json={
                "businessType": "weaponry",
                "params": {
                    "architectureId": 10502,
                    "filePathList": ["missing.pdf"],
                    "weaponryTemplateFieldList": [
                        {
                            "templateClassifyId": 7001,
                            "fieldName": "舰级名称",
                            "fieldType": "INPUT",
                        }
                    ],
                },
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn("尚未解析", response.get_json()["error"])

    def test_weaponry_rejects_ambiguous_file_name_across_categories(self):
        """同名记录无法由既有请求字段消歧，不能随机选择任一分类。"""
        self.weaponry_document_scope.errors[(10502, ("abc123.pdf",))] = (
            WeaponryDocumentScopeAmbiguityError("文件名无法唯一确定文档")
        )
        response = self.client.post(
            "/llm/weaponry",
            json={
                "businessType": "weaponry",
                "params": {
                    "architectureId": 10502,
                    "filePathList": ["abc123.pdf"],
                    "weaponryTemplateFieldList": [
                        {
                            "templateClassifyId": 7001,
                            "fieldName": "舰级名称",
                            "fieldType": "INPUT",
                        }
                    ],
                },
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("无法唯一", response.get_json()["error"])
        self.assertIsNone(self.task_service.get_task("weaponry", "10502"))

    def test_weaponry_fifty_distinct_submissions_persist_without_route_threads(
        self,
    ):
        """50 个不同业务键只形成持久任务，Web 层不得按请求创建后台线程。"""

        architecture_ids = tuple(range(11001, 11051))
        for architecture_id in architecture_ids:
            self.weaponry_document_scope.scopes[(architecture_id, ())] = (
                WeaponryDocumentScope(
                    mode=DOCUMENT_SCOPE_CATEGORY,
                    requested_file_names=(),
                    documents=(),
                )
            )

        def submit(architecture_id: int):
            with self.app.test_client() as client:
                return client.post(
                    "/llm/weaponry",
                    json={
                        "businessType": "weaponry",
                        "params": {
                            "architectureId": architecture_id,
                            "filePathList": [],
                            "weaponryTemplateFieldList": [
                                {
                                    "templateClassifyId": 7001,
                                    "fieldName": "舰级名称",
                                    "fieldType": "INPUT",
                                }
                            ],
                        },
                    },
                )

        with ThreadPoolExecutor(max_workers=50) as executor:
            responses = tuple(executor.map(submit, architecture_ids))

        self.assertEqual({202}, {response.status_code for response in responses})
        self.assertTrue(all(response.get_data() == b"" for response in responses))
        for architecture_id in architecture_ids:
            self.assertIsNotNone(
                self.task_service.get_task("weaponry", str(architecture_id))
            )

    def test_weaponry_fifty_same_key_submissions_accept_one_and_reject_49(
        self,
    ):
        """同一 architectureId 的原子受理在 HTTP 边界稳定映射为 1×202、49×409。"""

        architecture_id = 11100
        self.weaponry_document_scope.scopes[(architecture_id, ())] = (
            WeaponryDocumentScope(
                mode=DOCUMENT_SCOPE_CATEGORY,
                requested_file_names=(),
                documents=(),
            )
        )

        def submit(_: int):
            with self.app.test_client() as client:
                return client.post(
                    "/llm/weaponry",
                    json={
                        "businessType": "weaponry",
                        "params": {
                            "architectureId": architecture_id,
                            "filePathList": [],
                            "weaponryTemplateFieldList": [
                                {
                                    "templateClassifyId": 7001,
                                    "fieldName": "舰级名称",
                                    "fieldType": "INPUT",
                                }
                            ],
                        },
                    },
                )

        with ThreadPoolExecutor(max_workers=50) as executor:
            responses = tuple(executor.map(submit, range(50)))

        statuses = [response.status_code for response in responses]
        self.assertEqual(1, statuses.count(202))
        self.assertEqual(49, statuses.count(409))
        for response in responses:
            if response.status_code == 202:
                self.assertEqual(b"", response.get_data())
            else:
                self.assertEqual({"error": "任务正在处理中"}, response.get_json())
        self.assertIsNotNone(
            self.task_service.get_task("weaponry", str(architecture_id))
        )

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

