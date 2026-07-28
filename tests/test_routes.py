import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import MagicMock, patch

from app import create_app
from app.container import APPLICATION_SERVICES_EXTENSION
from app.modules.analysis.domain.models import (
    MAX_ANALYSIS_PARAMS_PER_REQUEST,
    MAX_ANALYSIS_REQUEST_BYTES,
)
from app.services.llm_service.task_service import LLMTaskService
from app.modules.reassign.application import (
    DocumentReassignmentService,
    RecoverReassignmentOperation,
    ReassignmentExecutionSettings,
)
from app.modules.reassign.composition import ReassignApplicationServices
from app.modules.reassign.domain import (
    ReassignDocumentCommand,
    ReassignmentPublicMessage,
    ReassignmentResult,
    ReassignmentResultCategory,
)
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
from tests.fakes.reassign import (
    FakeReassignmentKnowledgePortFactory,
    FakeReassignmentRepository,
)
from tests.offline_application import build_offline_application_services


class _RouteReassignResultService(DocumentReassignmentService):
    """为通用路由回归注入强类型 Application 结果，不保留遗留蓝图 Mock。"""

    def __init__(
        self,
        repository: FakeReassignmentRepository,
        knowledge_factory: FakeReassignmentKnowledgePortFactory,
        settings: ReassignmentExecutionSettings,
        *,
        result: ReassignmentResult,
    ) -> None:
        super().__init__(repository, knowledge_factory, settings)
        self.result = result
        self.commands: list[ReassignDocumentCommand] = []

    def execute(self, command: ReassignDocumentCommand) -> ReassignmentResult:
        if not isinstance(command, ReassignDocumentCommand):
            raise TypeError("command 必须是 ReassignDocumentCommand")
        self.commands.append(command)
        return self.result


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

    def _configure_reassign_result(
        self,
        category: ReassignmentResultCategory,
        message: ReassignmentPublicMessage,
    ) -> _RouteReassignResultService:
        """以强类型 Application 外观重新构造本测试的离线 Flask 容器。

        分类节点变更已不允许路由直接 mock 数据库或 AnythingLLM Client。这里仅替换
        ``execute()`` 的确定结果，仍让路由经过真实 Parser、Application 类型门禁和 Presenter。
        """

        repository = FakeReassignmentRepository()
        knowledge_factory = FakeReassignmentKnowledgePortFactory()
        settings = ReassignmentExecutionSettings(
            lease_owner="route-test-reassign",
            lease_duration_seconds=1.2,
            remote_total_timeout_seconds=1.0,
            lease_safety_margin_seconds=0.2,
        )
        document_reassignment = _RouteReassignResultService(
            repository,
            knowledge_factory,
            settings,
            result=ReassignmentResult(category=category, public_message=message),
        )
        reassign_services = ReassignApplicationServices(
            document_reassignment=document_reassignment,
            recovery=RecoverReassignmentOperation(
                repository,
                knowledge_factory,
                settings,
            ),
        )
        self.services = replace(
            self.services,
            reassign_services=reassign_services,
        )
        self.app = create_app(services=self.services)
        self.client = self.app.test_client()
        return document_reassignment

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

    @patch("threading.Thread")
    def test_analysis_persists_new_execution_without_route_thread(self, mock_thread):
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
        # 受理成功体必须严格为空，内部 execution_id 只能留在任务库和 Dispatcher 中。
        self.assertEqual(response.data, b"")
        mock_thread.assert_not_called()
        persisted_task = self.task_service.get_task("file", "sample.txt")
        self.assertIsNotNone(persisted_task)
        assert persisted_task is not None
        execution = self.task_service.get_task_execution(
            persisted_task["execution_id"]
        )
        self.assertIsNotNone(execution)
        assert execution is not None
        self.assertEqual("accepted", execution["execution_state"])
        self.assertIsNotNone(execution["batch_id"])
        self.assertEqual(1, execution["batch_sequence"])

    @patch("threading.Thread")
    def test_analysis_accepts_multiple_files_without_route_thread(self, mock_thread):
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
        # 批量受理同样不再通过 HTTP 返回 task/tasks 内部快照。
        self.assertEqual(response.data, b"")
        persisted_tasks = {
            file_name: self.task_service.get_task("file", file_name)
            for file_name in ("a.txt", "b.txt")
        }
        self.assertTrue(all(task is not None for task in persisted_tasks.values()))
        mock_thread.assert_not_called()
        executions = {
            file_name: self.task_service.get_task_execution(task["execution_id"])
            for file_name, task in persisted_tasks.items()
        }
        self.assertTrue(all(execution is not None for execution in executions.values()))
        self.assertEqual(
            1,
            executions["a.txt"]["batch_sequence"],  # type: ignore[index]
        )
        self.assertEqual(
            2,
            executions["b.txt"]["batch_sequence"],  # type: ignore[index]
        )
        self.assertEqual(
            executions["a.txt"]["batch_id"],  # type: ignore[index]
            executions["b.txt"]["batch_id"],  # type: ignore[index]
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

    @patch("threading.Thread")
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

    @patch("threading.Thread")
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

    @patch("threading.Thread")
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

        self.assertEqual(mock_thread.call_count, 0)

    @patch("threading.Thread")
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

    @patch("threading.Thread")
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

    @patch("threading.Thread")
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

    @patch("threading.Thread")
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

    @patch("threading.Thread")
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

    @patch("threading.Thread")
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

    @patch("threading.Thread")
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

    @patch("threading.Thread")
    def test_analysis_bounds_chunked_body_without_content_length(
        self,
        mock_thread,
    ):
        with patch(
            "app.adapters.web.flask.analysis_requests.MAX_ANALYSIS_REQUEST_BYTES",
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

    @patch("threading.Thread")
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

    @patch("threading.Thread")
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

    @patch("threading.Thread")
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
        self._configure_reassign_result(
            ReassignmentResultCategory.FAILED,
            ReassignmentPublicMessage.DOCUMENT_NOT_FOUND,
        )
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
        self.assertEqual([], self.kb_service.mock_calls)

    def test_reassign_returns_error_when_inconsistent(self):
        self._configure_reassign_result(
            ReassignmentResultCategory.FAILED,
            ReassignmentPublicMessage.ARCHITECTURE_MISMATCH,
        )
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
        self.assertEqual([], self.kb_service.mock_calls)

    def test_reassign_success_uses_application_without_legacy_database_access(self):
        application = self._configure_reassign_result(
            ReassignmentResultCategory.SUCCEEDED,
            ReassignmentPublicMessage.SUCCEEDED,
        )
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
        self.assertEqual(1, len(application.commands))
        self.assertEqual("a.pdf", application.commands[0].file_name)
        self.assertEqual(1, application.commands[0].old_architecture_id_query_value)
        self.assertEqual([], self.kb_service.mock_calls)

    def test_reassign_preserves_non_strict_new_id_through_application_and_presenter(self):
        application = self._configure_reassign_result(
            ReassignmentResultCategory.SUCCEEDED,
            ReassignmentPublicMessage.SUCCEEDED,
        )
        response = self.client.post(
            "/llm/reassign",
            json={
                "businessType": "reassign",
                "params": {
                    "fileName": "a.pdf",
                    "oldArchitectureId": 1,
                    "newArchitectureId": False,
                }
            }
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["data"]["success"])
        self.assertIs(False, data["data"]["newArchitectureId"])
        self.assertIs(False, application.commands[0].new_architecture_id_raw.to_python())
        self.kb_service.add_workspace.assert_not_called()
