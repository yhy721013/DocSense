"""分类节点变更公开黄金资产与 1E-6 薄路由黑盒回归。

文件名保留 1E-0 历史资产名称，测试语义已在 1E-6 切换为生产 Parser → Application →
Presenter 链路。所有依赖均为内存严格 Fake，不启动 ``run.py``、不创建 SQLite 文件、不连接
AnythingLLM 或后台服务。
"""

from __future__ import annotations

import ast
import json
import math
from dataclasses import replace
from pathlib import Path
import unittest

from app import create_app
from app.modules.reassign.adapters import ReassignmentInfrastructureConfig
from app.modules.reassign.application import (
    DocumentReassignmentService,
    RecoverReassignmentOperation,
    ReassignmentExecutionSettings,
)
from app.modules.reassign.composition import (
    ReassignApplicationServices,
    compose_reassign_application_services,
)
from app.modules.reassign.domain import (
    ReassignDocumentCommand,
    ReassignmentDocumentSnapshot,
    ReassignmentPublicMessage,
    ReassignmentResult,
    ReassignmentResultCategory,
)
from tests import workspace_tempdir
from tests.fakes.reassign import (
    FakeReassignmentKnowledgePortFactory,
    FakeReassignmentRepository,
)
from tests.offline_application import build_offline_application_services


_CONTRACT_PATH = (
    Path(__file__).with_name("contracts") / "stage1e0_reassign_contracts.json"
)
_INTERFACE_DOCUMENT_PATH = (
    Path(__file__).parents[1] / "docs" / "接口文档" / "分类节点变更.md"
)
_ROUTE_PATH = Path(__file__).parents[1] / "app" / "blueprints" / "llm.py"


def _walk_public_keys(value: object) -> set[str]:
    """递归收集公开 JSON 键，防止内部 Saga 字段意外从 Presenter 泄漏。"""

    if isinstance(value, dict):
        keys = set(value)
        for child in value.values():
            keys.update(_walk_public_keys(child))
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for child in value:
            keys.update(_walk_public_keys(child))
        return keys
    return set()


def _legacy_json_bytes(payload: dict[str, object]) -> bytes:
    """复现切换前 Flask ``jsonify`` 的排序、ASCII 转义和尾随换行。"""

    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


class _RecordingDocumentReassignmentService(DocumentReassignmentService):
    """供路由黑盒测试注入确定 Application 结果的最小类型化替身。

    继承真实 Application 类型并通过父类构造器接收严格 Port，确保 Container 不会因为测试
    使用任意 Mock 而绕过组合根类型门禁。它只替换 ``execute`` 的业务结果，不执行网络 I/O。
    """

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


class Stage1E0ReassignContractAssetTests(unittest.TestCase):
    """以真实 Flask 路由确认 1E-6 不改变已批准的公开契约。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract: dict[str, object] = json.loads(
            _CONTRACT_PATH.read_text(encoding="utf-8")
        )

    def setUp(self) -> None:
        self._tempdir = workspace_tempdir()
        self.runtime_directory = self._tempdir.__enter__()
        self.repository = FakeReassignmentRepository()
        self.knowledge_factory = FakeReassignmentKnowledgePortFactory()
        self.infrastructure_config = ReassignmentInfrastructureConfig(
            http_timeout_seconds=0.25,
            total_timeout_seconds=1.0,
            compensation_reserve_seconds=0.2,
        )
        self.settings = ReassignmentExecutionSettings(
            lease_owner="stage1e6-route-test",
            lease_duration_seconds=1.2,
            remote_total_timeout_seconds=1.0,
            lease_safety_margin_seconds=0.2,
        )
        self.application = _RecordingDocumentReassignmentService(
            self.repository,
            self.knowledge_factory,
            self.settings,
            result=self._result(
                ReassignmentResultCategory.SUCCEEDED,
                ReassignmentPublicMessage.SUCCEEDED,
            ),
        )
        self.reassign_services = ReassignApplicationServices(
            document_reassignment=self.application,
            recovery=RecoverReassignmentOperation(
                self.repository,
                self.knowledge_factory,
                self.settings,
            ),
        )
        offline_services = build_offline_application_services(self.runtime_directory)
        self.app = create_app(
            services=replace(
                offline_services,
                reassign_services=self.reassign_services,
            )
        )
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    @staticmethod
    def _result(
        category: ReassignmentResultCategory,
        message: ReassignmentPublicMessage,
    ) -> ReassignmentResult:
        return ReassignmentResult(category=category, public_message=message)

    @staticmethod
    def _case_by_name(cases: list[object], name: str) -> dict[str, object]:
        for item in cases:
            if isinstance(item, dict) and item.get("case") == name:
                return item
        raise AssertionError(f"未找到测试资产 case={name}")

    def _post(self, payload: dict[str, object]):
        return self.client.post("/llm/reassign", json=payload)

    def _set_result(
        self,
        category: ReassignmentResultCategory,
        message: ReassignmentPublicMessage,
    ) -> None:
        self.application.result = self._result(category, message)

    def _assert_json_response(
        self,
        response,
        *,
        status: int,
        body: dict[str, object],
    ) -> None:
        """同时验证状态码、JSON 类型、完整对象和值以及切换前的字节格式。"""

        self.assertEqual(status, response.status_code)
        self.assertEqual("application/json", response.mimetype)
        self.assertEqual(body, response.get_json())
        self.assertEqual(_legacy_json_bytes(body), response.data)

    def test_asset_marks_route_switch_without_interface_parameter_change(self) -> None:
        """历史资产可以更新实现状态，但不得借机改变任何公开参数。"""

        contract = self.contract
        self.assertEqual(1, contract["schemaVersion"])
        self.assertEqual("1E-0", contract["stage"])
        self.assertEqual("docs/接口文档/分类节点变更.md", contract["authority"])
        self.assertFalse(contract["interfaceParametersChanged"])
        self.assertEqual(
            "synchronous-saga-route-active-1e6",
            contract["productionCodeSwitch"],
        )
        self.assertEqual(
            "synchronous-saga-route-active",
            contract["contractState"]["current"],
        )

    def test_documented_400_golden_responses_are_byte_equivalent(self) -> None:
        """参数错误仍由 Parser 按相同顺序、相同状态码和字节体返回。"""

        golden = self.contract["goldenResponses"]["http400"]
        for case in golden:
            with self.subTest(case=case["case"]):
                response = self._post(case["request"])
                self._assert_json_response(
                    response,
                    status=case["status"],
                    body=case["body"],
                )
        self.assertEqual([], self.application.commands)

    def test_documented_500_golden_responses_are_byte_equivalent(self) -> None:
        """Presenter 对 Application 稳定消息保持既有 500 JSON 结构。"""

        expected_messages = {
            "missing-record": ReassignmentPublicMessage.DOCUMENT_NOT_FOUND,
            "architecture-mismatch": ReassignmentPublicMessage.ARCHITECTURE_MISMATCH,
            "remote-exception": ReassignmentPublicMessage.REMOTE_MIGRATION_FAILED,
        }
        golden = self.contract["goldenResponses"]["http500"]
        for case in golden:
            with self.subTest(case=case["case"]):
                self._set_result(
                    ReassignmentResultCategory.FAILED,
                    expected_messages[case["setup"]],
                )
                response = self._post(case["request"])
                self._assert_json_response(
                    response,
                    status=case["status"],
                    body=case["body"],
                )

    def test_documented_200_golden_responses_are_byte_equivalent(self) -> None:
        """完整成功不携带 Operation、步骤、lease 或 fencing 信息。"""

        self._set_result(
            ReassignmentResultCategory.SUCCEEDED,
            ReassignmentPublicMessage.SUCCEEDED,
        )
        golden = self.contract["goldenResponses"]["http200"]
        for case in golden:
            with self.subTest(case=case["case"]):
                response = self._post(case["request"])
                self._assert_json_response(
                    response,
                    status=case["status"],
                    body=case["body"],
                )

    def test_actual_composed_saga_keeps_empty_doc_path_local_only(self) -> None:
        """真实组合根经公开路由执行空路径兼容分支，且不创建远端端口。

        该用例不以替身覆盖 ``DocumentReassignmentService.execute``，专门防止薄路由虽然
        形式上已切换、实际却绕开 Saga 或在本地兼容分支错误创建 AnythingLLM 客户端。
        """

        repository = FakeReassignmentRepository(
            documents=(
                ReassignmentDocumentSnapshot(
                    document_row_id=101,
                    file_name="example.pdf",
                    source_architecture_id=11,
                    anything_doc_id="anything-doc-101",
                    doc_path="",
                    original_file_name="示例文档.pdf",
                ),
            ),
        )
        knowledge_factory = FakeReassignmentKnowledgePortFactory()
        composed_services = compose_reassign_application_services(
            repository=repository,
            knowledge_factory=knowledge_factory,
            settings=self.settings,
            infrastructure_config=self.infrastructure_config,
        )
        offline_services = build_offline_application_services(self.runtime_directory)
        app = create_app(
            services=replace(
                offline_services,
                reassign_services=composed_services,
            )
        )

        response = app.test_client().post(
            "/llm/reassign",
            json={
                "businessType": "reassign",
                "params": {
                    "fileName": "example.pdf",
                    "oldArchitectureId": 11,
                    "newArchitectureId": 12,
                },
            },
        )

        expected_body = {
            "businessType": "reassign",
            "msg": "变更成功",
            "data": {
                "fileName": "example.pdf",
                "oldArchitectureId": 11,
                "newArchitectureId": 12,
                "success": True,
                "message": "变更成功",
            },
        }
        self._assert_json_response(response, status=200, body=expected_body)
        self.assertEqual((), knowledge_factory.ports)
        with repository.unit_of_work(read_only=True) as unit_of_work:
            moved = unit_of_work.get_document_snapshot(
                file_name="example.pdf",
                source_architecture_id=12,
            )
        self.assertIsNotNone(moved)

    def test_raw_id_compatibility_and_frozen_new_id_whitelist_are_preserved(self) -> None:
        """路由只解析旧 ID，并仅保留已确认的新 ID 历史兼容白名单。"""

        matrix = self.contract["legacyCompatibilityMatrix"]
        raw_type_case = self._case_by_name(
            matrix,
            "raw-number-and-string-are-not-equal",
        )
        response = self._post(raw_type_case["request"])
        self.assertEqual(raw_type_case["approvedTarget"]["status"], response.status_code)
        self.assertEqual("11", response.get_json()["data"]["newArchitectureId"])
        first_command = self.application.commands[-1]
        self.assertEqual(11, first_command.old_architecture_id_query_value)
        self.assertEqual("11", first_command.new_architecture_id_raw.to_python())

        compatibility_case = self._case_by_name(
            matrix,
            "new-id-frozen-compatibility-whitelist",
        )
        response = self._post(compatibility_case["request"])
        self.assertEqual(
            compatibility_case["approvedTarget"]["status"],
            response.status_code,
        )
        self.assertIs(False, response.get_json()["data"]["newArchitectureId"])
        second_command = self.application.commands[-1]
        self.assertIs(False, second_command.new_architecture_id_raw.to_python())

    def test_old_id_conversion_failure_remains_unwrapped_500(self) -> None:
        """Parser 不得把遗留 ``int(oldArchitectureId)`` 异常收紧为新的 400。"""

        matrix = self.contract["legacyCompatibilityMatrix"]
        case = self._case_by_name(matrix, "old-id-conversion-failure-is-unwrapped")
        response = self._post(case["request"])
        self.assertEqual(case["approvedTarget"]["status"], response.status_code)
        self.assertIsNone(response.get_json(silent=True))
        self.assertEqual([], self.application.commands)

    def test_all_approved_failure_messages_keep_500_shape_and_hide_internal_fields(self) -> None:
        """补偿、并发和恢复待处理均不能改变前端结构或泄漏内部状态。"""

        forbidden = set(self.contract["publicEndpoint"]["forbiddenPublicFields"])
        public_messages = self.contract["publicMessages"]
        for key, message in public_messages.items():
            if key == "success":
                continue
            with self.subTest(message_key=key):
                matched_enum = next(
                    candidate
                    for candidate in ReassignmentPublicMessage
                    if candidate.value == message
                )
                category = (
                    ReassignmentResultCategory.RECOVERY_REQUIRED
                    if matched_enum
                    in {
                        ReassignmentPublicMessage.COMPENSATION_FAILED,
                        ReassignmentPublicMessage.RECOVERY_PENDING,
                    }
                    else ReassignmentResultCategory.FAILED
                )
                self._set_result(category, matched_enum)
                response = self._post(
                    {
                        "businessType": "reassign",
                        "params": {
                            "fileName": "example.pdf",
                            "oldArchitectureId": 11,
                            "newArchitectureId": 12,
                        },
                    }
                )
                self.assertEqual(500, response.status_code)
                body = response.get_json()
                self.assertFalse(forbidden & _walk_public_keys(body))
                self.assertEqual(message, body["data"]["message"])

    def test_approved_fault_matrix_is_consumed_and_linked_to_executable_scenarios(self) -> None:
        """批准故障矩阵不能只停留在 JSON；每一行都必须绑定一个真实用例。"""

        matrix = self.contract["approvedTargetFaultMatrix"]
        coverage = {
            "source-detach-explicit-false": (
                "test_known_source_detach_failure_releases_document_protection"
            ),
            "source-detach-outcome-unknown": (
                "test_timeout_after_detach_and_unknown_probe_never_blindly_replays_write"
            ),
            "target-workspace-created-without-slug": (
                "test_create_response_missing_slug_never_enters_success_path"
            ),
            "target-attach-explicit-false": (
                "test_known_target_attach_failure_restores_source_synchronously"
            ),
            "local-cas-affects-zero-rows": (
                "test_local_cas_conflict_detaches_target_then_restores_source"
            ),
            "same-document-operation-is-active": (
                "test_fifty_concurrent_same_document_requests_have_exactly_one_owner"
            ),
            "compensation-itself-fails": (
                "test_explicit_source_restore_failure_uses_compensation_failed_message"
            ),
            "hard-budget-exhausted-with-unknown-result": (
                "test_deadline_clips_forward_calls_and_preserves_recovery_window"
            ),
        }
        self.assertEqual(set(coverage), {item["case"] for item in matrix})
        test_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                Path(__file__).with_name("test_reassign_application.py"),
                Path(__file__).with_name("test_reassign_sqlite_adapter.py"),
                Path(__file__).with_name("test_reassign_anythingllm_adapter.py"),
            )
        )
        for item in matrix:
            with self.subTest(case=item["case"]):
                self.assertEqual(500, item["expectedStatus"])
                self.assertIn(
                    item["expectedMessageKey"],
                    self.contract["publicMessages"],
                )
                self.assertIn(f"def {coverage[item['case']]}(", test_sources)

    def test_invalid_target_id_is_rejected_before_application_without_new_400(self) -> None:
        """契约外新 ID 保持 HTTP 500 边界，且不得创建 Operation 或远端调用。"""

        for invalid_target in (True, 12.0, "12.0", "1e2", [], {}, 2**63):
            with self.subTest(invalid_target=invalid_target):
                response = self._post(
                    {
                        "businessType": "reassign",
                        "params": {
                            "fileName": "example.pdf",
                            "oldArchitectureId": 11,
                            "newArchitectureId": invalid_target,
                        },
                    }
                )
                self.assertEqual(500, response.status_code)
        self.assertEqual([], self.application.commands)
        self.assertEqual((), self.knowledge_factory.ports)

    def test_public_message_registry_matches_domain_and_interface_document(self) -> None:
        """接口文档仍是唯一权威，领域枚举只能使用已批准的稳定文案。"""

        expected = {
            "success": ReassignmentPublicMessage.SUCCEEDED.value,
            "documentNotFound": ReassignmentPublicMessage.DOCUMENT_NOT_FOUND.value,
            "architectureMismatch": (
                ReassignmentPublicMessage.ARCHITECTURE_MISMATCH.value
            ),
            "remoteMigrationFailed": (
                ReassignmentPublicMessage.REMOTE_MIGRATION_FAILED.value
            ),
            "localStateConflict": ReassignmentPublicMessage.LOCAL_STATE_CONFLICT.value,
            "concurrentOperation": ReassignmentPublicMessage.CONCURRENT_OPERATION.value,
            "compensationFailed": ReassignmentPublicMessage.COMPENSATION_FAILED.value,
            "recoveryPending": ReassignmentPublicMessage.RECOVERY_PENDING.value,
        }
        self.assertEqual(expected, self.contract["publicMessages"])
        interface_document = _INTERFACE_DOCUMENT_PATH.read_text(encoding="utf-8")
        for message in expected.values():
            with self.subTest(message=message):
                self.assertIn(message, interface_document)

    def test_route_contains_no_legacy_database_or_anythingllm_orchestration(self) -> None:
        """AST 与源码文本同时锁住蓝图只做 Parser → Application → Presenter。"""

        source = _ROUTE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(_ROUTE_PATH))
        route = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "llm_reassign"
        )
        route_source = ast.get_source_segment(source, route) or ""
        for forbidden in (
            "AnythingLLMClient",
            "update_document_architecture",
            "update_embeddings",
            "update_embeddings_batch",
            "SQLiteReassignmentRepository",
            "threading.Thread",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, route_source)
        self.assertIn("parse_reassign_request", route_source)
        self.assertIn("document_reassignment.execute", route_source)
        self.assertIn("presenter.present_result", route_source)

    def test_offline_timeout_budget_is_finite_but_not_a_production_recommendation(self) -> None:
        """离线注入预算只能证明边界，不得被误述为真实容量校准。"""

        budget = self.contract["offlineTimeoutBudget"]
        http_timeout = float(budget["httpTimeoutSeconds"])
        total_timeout = float(budget["totalTimeoutSeconds"])
        compensation_reserve = float(budget["compensationReserveSeconds"])
        for value in (http_timeout, total_timeout, compensation_reserve):
            self.assertTrue(math.isfinite(value))
            self.assertGreater(value, 0)
        self.assertLess(compensation_reserve, total_timeout)
        self.assertFalse(budget["productionCalibrated"])
        self.assertIn("不得据此推导生产秒数", budget["fakeDelayPolicy"])


if __name__ == "__main__":
    unittest.main()
