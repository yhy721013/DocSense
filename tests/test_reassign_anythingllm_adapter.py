"""阶段 1E-3：分类节点变更 AnythingLLM Adapter 的离线协议与预算测试。

本文件仅使用可控 Fake Client/Transport，不发起任何真实 HTTP 请求、不读取 `.env`，也不启动
``run.py``。测试重点是让外部副作用无法被 `false`、超时或不完整响应伪装成成功。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any
import unittest

from app.integrations.anythingllm import (
    AnythingLLMConnectionError,
    AnythingLLMDocument,
    AnythingLLMHTTPError,
    AnythingLLMTimeoutError,
    AnythingLLMWorkspace,
)
from app.modules.reassign.adapters.anythingllm_clients import (
    AnythingLLMReassignmentClientFactory,
)
from app.modules.reassign.adapters.anythingllm_knowledge import (
    AnythingLLMReassignmentKnowledgeAdapter,
    AnythingLLMReassignmentKnowledgeAdapterFactory,
)
from app.modules.reassign.adapters.infrastructure_config import (
    ReassignmentDeadlineExceededError,
    ReassignmentExecutionDeadline,
    ReassignmentInfrastructureConfig,
    ReassignmentInfrastructureConfigurationError,
    load_reassignment_infrastructure_config,
)
from app.modules.reassign.domain import ReassignmentStepName
from app.modules.reassign.ports import (
    ReassignmentDocumentMutationRequest,
    ReassignmentDocumentReference,
    ReassignmentKnowledgeOutcome,
    ReassignmentKnowledgePortFactory,
    ReassignmentMembershipProbeRequest,
    ReassignmentMembershipState,
    ReassignmentWorkspacePreparationRequest,
    ReassignmentWorkspaceOwnership,
    ReassignmentWorkspaceProbeState,
    ReassignmentWorkspaceReference,
    ReassignmentWorkspaceReferenceProbeRequest,
)
from app.services.core.config import AnythingLLMConfig


@dataclass
class _MutableClock:
    """可控单调时钟；测试可以显式推进，不依赖真实等待。"""

    current: float = 100.0

    def __call__(self) -> float:
        return self.current


class _FakeWorkspaceClient:
    """只实现 1E-3 所需的原子 Workspace API，并记录精确调用顺序。"""

    def __init__(self) -> None:
        self.list_results: list[object] = []
        self.create_results: list[object] = []
        self.find_results: list[object] = []
        self.update_results: list[object] = []
        self.pin_results: list[object] = []
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    @staticmethod
    def _next(results: list[object], default: object) -> object:
        result = results.pop(0) if results else default
        if isinstance(result, BaseException):
            raise result
        return result

    def list_workspaces(self, *, user_id: int | None = None) -> object:
        self.calls.append(("list_workspaces", (), {"user_id": user_id}))
        return self._next(self.list_results, [])

    def create_workspace(
        self,
        name: str,
        *,
        settings: object = None,
        user_id: int | None = None,
    ) -> object:
        self.calls.append(
            (
                "create_workspace",
                (name,),
                {"settings": settings, "user_id": user_id},
            )
        )
        return self._next(self.create_results, False)

    def find_document(
        self,
        workspace_slug: str,
        location: str,
        *,
        user_id: int | None = None,
    ) -> object:
        self.calls.append(
            (
                "find_document",
                (workspace_slug, location),
                {"user_id": user_id},
            )
        )
        return self._next(self.find_results, None)

    def update_embeddings(
        self,
        workspace_slug: str,
        *,
        adds: object = None,
        deletes: object = None,
        user_id: int | None = None,
    ) -> object:
        self.calls.append(
            (
                "update_embeddings",
                (workspace_slug,),
                {"adds": adds, "deletes": deletes, "user_id": user_id},
            )
        )
        return self._next(self.update_results, False)

    def update_pin(
        self,
        workspace_slug: str,
        location: str,
        *,
        pinned: bool = True,
        user_id: int | None = None,
    ) -> object:
        self.calls.append(
            (
                "update_pin",
                (workspace_slug, location),
                {"pinned": pinned, "user_id": user_id},
            )
        )
        return self._next(self.pin_results, None)


class _FakeClientLease(AbstractContextManager[object]):
    """模拟一次请求级 Client 租约，并验证每次 Adapter 调用都会结束。"""

    def __init__(self, factory: "_FakeClientFactory") -> None:
        self._factory = factory

    def __enter__(self) -> object:
        self._factory.enter_count += 1
        return type("Clients", (), {"workspaces": self._factory.workspaces})()

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> bool:
        self._factory.exit_count += 1
        if self._factory.close_error is not None and exc_type is None:
            raise self._factory.close_error
        return False


class _FakeClientFactory:
    """离线 Client Factory；只记录 deadline 计算后的超时，不拥有真实 Session。"""

    def __init__(self, workspaces: _FakeWorkspaceClient) -> None:
        self.workspaces = workspaces
        self.timeouts: list[float] = []
        self.enter_count = 0
        self.exit_count = 0
        self.close_error: Exception | None = None

    def create(self, *, timeout_seconds: float) -> _FakeClientLease:
        self.timeouts.append(timeout_seconds)
        return _FakeClientLease(self)


class _CloseSpyTransport:
    """用于生产 Client Factory 的构造/关闭测试，不提供网络方法。"""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.close_count = 0
        self.raise_on_close: Exception | None = None

    def close(self) -> None:
        self.close_count += 1
        if self.raise_on_close is not None:
            raise self.raise_on_close


def _workspace(
    *,
    slug: str = "archid-12",
    name: str = "archId-12",
) -> AnythingLLMWorkspace:
    return AnythingLLMWorkspace(id=slug, slug=slug, name=name)


def _document(
    *,
    location: str = "custom-documents/example-uuid.json",
) -> AnythingLLMDocument:
    return AnythingLLMDocument(
        id="uuid",
        location=location,
        title="example.pdf",
        document_ref="document:uuid",
    )


def _workspace_request() -> ReassignmentWorkspacePreparationRequest:
    return ReassignmentWorkspacePreparationRequest(
        operation_id="reassign-op-1",
        target_architecture_raw=12,
        desired_workspace_name="archId-12",
        idempotency_key="prepare-target-workspace-key",
    )


def _document_reference() -> ReassignmentDocumentReference:
    return ReassignmentDocumentReference(
        document_row_id=7,
        file_name="example.pdf",
        doc_path="custom-documents/example-uuid.json",
        anything_doc_id="uuid",
        original_file_name="example.pdf",
    )


def _mutation_request(
    step_name: ReassignmentStepName,
) -> ReassignmentDocumentMutationRequest:
    return ReassignmentDocumentMutationRequest(
        operation_id="reassign-op-1",
        step_name=step_name,
        workspace=ReassignmentWorkspaceReference("archid-12"),
        document=_document_reference(),
        architecture_raw=12,
        idempotency_key=f"{step_name.value}-key",
    )


class ReassignmentInfrastructureConfigTests(unittest.TestCase):
    """验证预算配置在任何 HTTP 调用前 fail-fast。"""

    def test_config_rejects_non_finite_non_positive_and_unreserved_values(self) -> None:
        invalid_cases = (
            {"http_timeout_seconds": None},
            {"http_timeout_seconds": True},
            {"http_timeout_seconds": float("nan")},
            {"total_timeout_seconds": float("inf")},
            {"compensation_reserve_seconds": 0},
            {"total_timeout_seconds": 10, "compensation_reserve_seconds": 10},
        )
        for kwargs in invalid_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ReassignmentInfrastructureConfigurationError):
                    ReassignmentInfrastructureConfig(**kwargs)

    def test_environment_loader_rejects_invalid_strings_and_keeps_defaults_explicit(self) -> None:
        config = load_reassignment_infrastructure_config(environment={})
        self.assertEqual("single_instance", config.runtime_mode)
        self.assertEqual(15.0, config.http_timeout_seconds)
        with self.assertRaises(ReassignmentInfrastructureConfigurationError):
            load_reassignment_infrastructure_config(
                environment={"DOCSENSE_REASSIGN_RUNTIME_MODE": "cluster"}
            )
        with self.assertRaises(ReassignmentInfrastructureConfigurationError):
            load_reassignment_infrastructure_config(
                environment={"DOCSENSE_REASSIGN_HTTP_TIMEOUT_SECONDS": "NaN"}
            )
        with self.assertRaises(ReassignmentInfrastructureConfigurationError):
            load_reassignment_infrastructure_config(
                environment={"DOCSENSE_REASSIGN_TOTAL_TIMEOUT_SECONDS": "  "}
            )

    def test_deadline_clips_forward_calls_and_preserves_recovery_window(self) -> None:
        clock = _MutableClock(current=100.0)
        deadline = ReassignmentExecutionDeadline(
            ReassignmentInfrastructureConfig(
                http_timeout_seconds=10.0,
                total_timeout_seconds=30.0,
                compensation_reserve_seconds=8.0,
            ),
            monotonic_clock=clock,
        )
        self.assertEqual(10.0, deadline.forward_http_timeout_seconds())
        clock.current = 115.0
        self.assertEqual(7.0, deadline.forward_http_timeout_seconds())
        self.assertEqual(10.0, deadline.recovery_http_timeout_seconds())
        clock.current = 122.0
        with self.assertRaises(ReassignmentDeadlineExceededError):
            deadline.forward_http_timeout_seconds()
        self.assertEqual(8.0, deadline.recovery_http_timeout_seconds())

    def test_backwards_fake_clock_cannot_extend_budget(self) -> None:
        clock = _MutableClock(current=20.0)
        deadline = ReassignmentExecutionDeadline(
            ReassignmentInfrastructureConfig(
                http_timeout_seconds=10.0,
                total_timeout_seconds=20.0,
                compensation_reserve_seconds=5.0,
            ),
            monotonic_clock=clock,
        )
        clock.current = 30.0
        self.assertEqual(10.0, deadline.remaining_seconds())
        clock.current = 21.0
        self.assertEqual(10.0, deadline.remaining_seconds())

    def test_deadline_deducts_application_elapsed_time_before_adapter_creation(self) -> None:
        """保留、状态推进和锁等待耗时不能在创建 Adapter 时重新获得满额预算。"""

        clock = _MutableClock(current=100.0)
        deadline = ReassignmentExecutionDeadline(
            ReassignmentInfrastructureConfig(
                http_timeout_seconds=10.0,
                total_timeout_seconds=30.0,
                compensation_reserve_seconds=8.0,
            ),
            monotonic_clock=clock,
            elapsed_seconds=15.0,
        )

        self.assertEqual(15.0, deadline.remaining_seconds())
        self.assertEqual(7.0, deadline.forward_http_timeout_seconds())
        with self.assertRaises(ReassignmentInfrastructureConfigurationError):
            ReassignmentExecutionDeadline(
                ReassignmentInfrastructureConfig(),
                monotonic_clock=clock,
                elapsed_seconds=-1.0,
            )


class ReassignmentAnythingLLMClientFactoryTests(unittest.TestCase):
    """验证生产 Factory 不复用 Session，且关闭异常不覆盖已有业务异常。"""

    @staticmethod
    def _config() -> AnythingLLMConfig:
        return AnythingLLMConfig(
            base_url="http://anythingllm.invalid/api/v1",
            api_key="test-key",
            timeout=None,
            storage_root=None,
        )

    def test_factory_creates_and_closes_a_new_transport_for_each_lease(self) -> None:
        transports: list[_CloseSpyTransport] = []

        def transport_factory(**kwargs: object) -> _CloseSpyTransport:
            transport = _CloseSpyTransport(**kwargs)
            transports.append(transport)
            return transport

        factory = AnythingLLMReassignmentClientFactory(
            self._config(),
            transport_factory=transport_factory,
        )
        with factory.create(timeout_seconds=3.5) as first:
            self.assertIsNotNone(first.workspaces)
        with factory.create(timeout_seconds=4.5) as second:
            self.assertIsNotNone(second.workspaces)

        self.assertEqual(2, len(transports))
        self.assertEqual(1, transports[0].close_count)
        self.assertEqual(1, transports[1].close_count)
        self.assertEqual(3.5, transports[0].kwargs["timeout"])
        self.assertEqual(4.5, transports[1].kwargs["timeout"])

    def test_close_failure_does_not_replace_original_business_exception(self) -> None:
        transport = _CloseSpyTransport()
        transport.raise_on_close = RuntimeError("close-secret-value")
        factory = AnythingLLMReassignmentClientFactory(
            self._config(),
            transport_factory=lambda **_: transport,
        )

        with self.assertLogs(
            "app.modules.reassign.adapters.anythingllm_clients",
            level="ERROR",
        ) as captured:
            with self.assertRaisesRegex(ValueError, "business-secret-value"):
                with factory.create(timeout_seconds=2.0):
                    raise ValueError("business-secret-value")
        self.assertEqual(1, transport.close_count)
        log_text = "\n".join(captured.output)
        self.assertNotIn("business-secret-value", log_text)
        self.assertNotIn("close-secret-value", log_text)
        self.assertIn("close_error_type=RuntimeError", log_text)


class ReassignmentAnythingLLMKnowledgeAdapterTests(unittest.TestCase):
    """覆盖 1E-3 的 workspace、成员关系、异常分类和请求隔离门禁。"""

    def setUp(self) -> None:
        self.clock = _MutableClock()
        self.workspaces = _FakeWorkspaceClient()
        self.clients = _FakeClientFactory(self.workspaces)
        self.adapter = AnythingLLMReassignmentKnowledgeAdapter(
            self.clients,
            ReassignmentInfrastructureConfig(
                http_timeout_seconds=10.0,
                total_timeout_seconds=60.0,
                compensation_reserve_seconds=20.0,
            ),
            monotonic_clock=self.clock,
        )

    def test_existing_target_workspace_is_reused_without_create(self) -> None:
        self.workspaces.list_results = [[_workspace()]]

        result = self.adapter.prepare_target_workspace(_workspace_request())

        self.assertEqual(ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE, result.outcome)
        self.assertEqual("archid-12", result.workspace.slug)
        self.assertIs(
            ReassignmentWorkspaceOwnership.PREEXISTING,
            result.ownership,
        )
        self.assertEqual(["list_workspaces"], [call[0] for call in self.workspaces.calls])

    def test_target_workspace_is_created_only_after_exact_lookup_misses(self) -> None:
        self.workspaces.list_results = [[]]
        self.workspaces.create_results = [_workspace()]

        result = self.adapter.prepare_target_workspace(_workspace_request())

        self.assertEqual(ReassignmentKnowledgeOutcome.APPLIED, result.outcome)
        self.assertIs(
            ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            result.ownership,
        )
        self.assertEqual(
            ["list_workspaces", "create_workspace"],
            [call[0] for call in self.workspaces.calls],
        )
        self.assertEqual(2, self.clients.enter_count)
        self.assertEqual(2, self.clients.exit_count)
        self.assertTrue(all(timeout <= 10.0 for timeout in self.clients.timeouts))

    def test_multiple_exact_workspace_matches_are_unknown_not_arbitrarily_selected(self) -> None:
        self.workspaces.list_results = [
            [
                _workspace(slug="archid-12-a"),
                _workspace(slug="archid-12-b"),
            ]
        ]

        result = self.adapter.prepare_target_workspace(_workspace_request())

        self.assertEqual(ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN, result.outcome)
        self.assertEqual("workspace_identity_ambiguous", result.error_code)
        self.assertFalse(any(call[0] == "create_workspace" for call in self.workspaces.calls))

    def test_false_create_is_known_failure_and_never_returns_workspace(self) -> None:
        self.workspaces.list_results = [[]]
        self.workspaces.create_results = [False]

        result = self.adapter.prepare_target_workspace(_workspace_request())

        self.assertEqual(ReassignmentKnowledgeOutcome.KNOWN_FAILURE, result.outcome)
        self.assertIsNone(result.workspace)
        self.assertIsNone(result.ownership)

    def test_create_response_missing_slug_never_enters_success_path(self) -> None:
        self.workspaces.list_results = [[], []]
        self.workspaces.create_results = [
            AnythingLLMWorkspace(
                id="workspace-id",
                slug="",
                name="archId-12",
            )
        ]

        result = self.adapter.prepare_target_workspace(_workspace_request())

        self.assertEqual(ReassignmentKnowledgeOutcome.KNOWN_FAILURE, result.outcome)
        self.assertIsNone(result.workspace)
        self.assertEqual(
            ["list_workspaces", "create_workspace", "list_workspaces"],
            [call[0] for call in self.workspaces.calls],
        )

    def test_timeout_after_create_uses_single_readback_but_preserves_unknown_ownership(self) -> None:
        self.workspaces.list_results = [[], [_workspace()]]
        self.workspaces.create_results = [AnythingLLMTimeoutError("timeout")]

        result = self.adapter.prepare_target_workspace(_workspace_request())

        self.assertEqual(
            ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE,
            result.outcome,
        )
        self.assertEqual("archid-12", result.workspace.slug)
        self.assertIs(ReassignmentWorkspaceOwnership.UNKNOWN, result.ownership)
        self.assertEqual(
            ["list_workspaces", "create_workspace", "list_workspaces"],
            [call[0] for call in self.workspaces.calls],
        )

    def test_create_conflict_uses_readback_and_keeps_unknown_ownership(self) -> None:
        """409 可能来自并发创建成功，不能未经查回直接判定为无副作用失败。"""

        self.workspaces.list_results = [[], [_workspace()]]
        self.workspaces.create_results = [
            AnythingLLMHTTPError("conflict-secret", status_code=409)
        ]

        result = self.adapter.prepare_target_workspace(_workspace_request())

        self.assertEqual(
            ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE,
            result.outcome,
        )
        self.assertIs(ReassignmentWorkspaceOwnership.UNKNOWN, result.ownership)
        self.assertEqual("archid-12", result.workspace.slug)
        self.assertEqual(
            ["list_workspaces", "create_workspace", "list_workspaces"],
            [call[0] for call in self.workspaces.calls],
        )

    def test_timeout_after_create_with_absent_readback_is_known_failure_without_recreate(self) -> None:
        self.workspaces.list_results = [[], []]
        self.workspaces.create_results = [AnythingLLMConnectionError("connection")]

        result = self.adapter.prepare_target_workspace(_workspace_request())

        self.assertEqual(ReassignmentKnowledgeOutcome.KNOWN_FAILURE, result.outcome)
        self.assertEqual("anythingllm_connection_error", result.error_code)
        self.assertEqual(1, sum(call[0] == "create_workspace" for call in self.workspaces.calls))

    def test_read_only_workspace_probe_never_claims_creation_ownership(self) -> None:
        self.workspaces.list_results = [[_workspace()]]

        result = self.adapter.probe_target_workspace(_workspace_request())

        self.assertEqual(ReassignmentWorkspaceProbeState.PRESENT, result.state)
        self.assertIs(ReassignmentWorkspaceOwnership.UNKNOWN, result.ownership)
        self.assertEqual("archid-12", result.workspace.slug)

    def test_reference_probe_uses_persisted_slug_instead_of_current_name_rule(
        self,
    ) -> None:
        """远端展示名被修改后，既有 mapping 仍可按 slug 大小写无关地查回。"""

        self.workspaces.list_results = [
            [
                _workspace(
                    slug="Legacy-Target-Slug",
                    name="管理员重命名后的 workspace",
                )
            ]
        ]

        result = self.adapter.probe_workspace_reference(
            ReassignmentWorkspaceReferenceProbeRequest(
                operation_id="reassign-op-1",
                workspace=ReassignmentWorkspaceReference("legacy-target-slug"),
            )
        )

        self.assertEqual(ReassignmentWorkspaceProbeState.PRESENT, result.state)
        self.assertIs(ReassignmentWorkspaceOwnership.UNKNOWN, result.ownership)
        self.assertEqual("Legacy-Target-Slug", result.workspace.slug)
        self.assertEqual(
            ["list_workspaces"],
            [call[0] for call in self.workspaces.calls],
        )

    def test_membership_probe_requires_full_doc_path_not_same_basename(self) -> None:
        self.workspaces.find_results = [_document(location="custom-documents/other-uuid.json")]
        request = ReassignmentMembershipProbeRequest(
            operation_id="reassign-op-1",
            workspace=ReassignmentWorkspaceReference("archid-12"),
            document=_document_reference(),
        )

        result = self.adapter.probe_document_membership(request)

        self.assertEqual(ReassignmentMembershipState.OUTCOME_UNKNOWN, result.state)
        self.assertEqual("membership_probe_identity_conflict", result.error_code)

    def test_detach_success_must_be_followed_by_absence_probe(self) -> None:
        self.workspaces.update_results = [_workspace()]
        self.workspaces.find_results = [None]

        result = self.adapter.detach_document(
            _mutation_request(ReassignmentStepName.DETACH_SOURCE_DOCUMENT)
        )

        self.assertEqual(ReassignmentKnowledgeOutcome.APPLIED, result.outcome)
        self.assertEqual(
            ["update_embeddings", "find_document"],
            [call[0] for call in self.workspaces.calls],
        )
        update_call = self.workspaces.calls[0]
        self.assertEqual(["custom-documents/example-uuid.json"], update_call[2]["deletes"])
        self.assertIsNone(update_call[2]["adds"])
        self.assertEqual(2, self.clients.enter_count)
        self.assertEqual(2, self.clients.exit_count)

    def test_wrong_step_is_rejected_before_any_remote_call(self) -> None:
        """生产 Adapter 必须与严格 Fake 一致，不能让错误编排触发真实删除。"""

        with self.assertRaisesRegex(ValueError, "不接受步骤"):
            self.adapter.detach_document(
                _mutation_request(ReassignmentStepName.ATTACH_TARGET_DOCUMENT)
            )

        self.assertEqual([], self.workspaces.calls)
        self.assertEqual([], self.clients.timeouts)

    def test_normal_write_and_confirmation_do_not_consume_recovery_reserve(self) -> None:
        """正常成功写与写后确认均使用前向窗口，给后续补偿完整保留预算。"""

        self.clock.current = 135.0
        self.workspaces.update_results = [_workspace()]
        self.workspaces.find_results = [_document()]

        result = self.adapter.attach_document(
            _mutation_request(ReassignmentStepName.ATTACH_TARGET_DOCUMENT)
        )

        self.assertEqual(ReassignmentKnowledgeOutcome.APPLIED, result.outcome)
        self.assertEqual([5.0, 5.0], self.clients.timeouts)

    def test_compensation_write_uses_recovery_reserve_after_forward_budget_exhausted(
        self,
    ) -> None:
        """补偿步骤由 step_name 自动切换预算，不能误走已经耗尽的前向窗口。"""

        self.clock.current = 141.0
        self.workspaces.update_results = [_workspace()]
        self.workspaces.find_results = [_document()]

        result = self.adapter.attach_document(
            _mutation_request(ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT)
        )

        self.assertEqual(ReassignmentKnowledgeOutcome.APPLIED, result.outcome)
        self.assertEqual([10.0, 10.0], self.clients.timeouts)
        self.assertEqual(
            ["update_embeddings", "find_document"],
            [call[0] for call in self.workspaces.calls],
        )

    def test_false_attach_with_present_probe_converges_to_already_desired_state(self) -> None:
        self.workspaces.update_results = [False]
        self.workspaces.find_results = [_document()]

        result = self.adapter.attach_document(
            _mutation_request(ReassignmentStepName.ATTACH_TARGET_DOCUMENT)
        )

        self.assertEqual(ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE, result.outcome)
        self.assertEqual("archid-12", result.external_reference)

    def test_timeout_after_detach_and_unknown_probe_never_blindly_replays_write(self) -> None:
        self.workspaces.update_results = [AnythingLLMTimeoutError("timeout")]
        self.workspaces.find_results = [AnythingLLMConnectionError("connection")]

        result = self.adapter.detach_document(
            _mutation_request(ReassignmentStepName.DETACH_SOURCE_DOCUMENT)
        )

        self.assertEqual(ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN, result.outcome)
        self.assertEqual("anythingllm_timeout", result.error_code)
        self.assertEqual(1, sum(call[0] == "update_embeddings" for call in self.workspaces.calls))

    def test_successful_write_with_failed_post_probe_preserves_probe_error_code(self) -> None:
        self.workspaces.update_results = [_workspace()]
        self.workspaces.find_results = [AnythingLLMConnectionError("connection")]

        result = self.adapter.attach_document(
            _mutation_request(ReassignmentStepName.ATTACH_TARGET_DOCUMENT)
        )

        self.assertEqual(ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN, result.outcome)
        self.assertEqual("anythingllm_connection_error", result.error_code)

    def test_explicit_http_rejection_and_confirmed_unchanged_membership_is_known_failure(self) -> None:
        self.workspaces.update_results = [
            AnythingLLMHTTPError("rejected", status_code=400)
        ]
        self.workspaces.find_results = [None]

        result = self.adapter.attach_document(
            _mutation_request(ReassignmentStepName.ATTACH_TARGET_DOCUMENT)
        )

        self.assertEqual(ReassignmentKnowledgeOutcome.KNOWN_FAILURE, result.outcome)
        self.assertEqual("anythingllm_http_client_error", result.error_code)

    def test_pin_remains_best_effort_and_does_not_report_false_as_success(self) -> None:
        self.workspaces.pin_results = [False]

        result = self.adapter.pin_document_best_effort(
            _mutation_request(ReassignmentStepName.ATTACH_TARGET_DOCUMENT)
        )

        self.assertEqual(ReassignmentKnowledgeOutcome.KNOWN_FAILURE, result.outcome)
        self.assertEqual("pin_returned_false", result.error_code)

    def test_adapter_factory_creates_independent_deadline_instances(self) -> None:
        factory = AnythingLLMReassignmentKnowledgeAdapterFactory(
            self.clients,
            ReassignmentInfrastructureConfig(),
            monotonic_clock=self.clock,
        )

        first = factory.create()
        second = factory.create()

        self.assertIsInstance(factory, ReassignmentKnowledgePortFactory)
        self.assertIsNot(first, second)
        self.assertIsInstance(first, AnythingLLMReassignmentKnowledgeAdapter)
        self.assertIsInstance(second, AnythingLLMReassignmentKnowledgeAdapter)


if __name__ == "__main__":
    unittest.main()
