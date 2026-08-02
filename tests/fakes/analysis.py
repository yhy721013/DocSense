"""阶段 1F-2 文件分析 Ports 的严格、零 I/O Fake。

Fake 不提供默认成功结果。除全局顺序脚本外，还支持按 execution 建立独立期望队列；不同
任务可以并发交错，但同一任务内的副作用顺序仍必须精确匹配。每个返回 DTO 都会再次与请求
身份关联，禁止跨任务、跨阶段结果被测试误当成成功。
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from typing import Any, Iterator

from app.modules.analysis.domain.task_inputs import (
    AnalysisTaskInputV1,
    FrozenJsonObject,
)
from app.modules.analysis.ports import (
    AppendAnalysisLifecycleEvents,
    AnalysisBatchAdmission,
    AnalysisBatchAdmissionOutcome,
    AnalysisBatchCommand,
    AnalysisCallbackAcquireResult,
    AnalysisCallbackDelivery,
    AnalysisCallbackDeliveryRequest,
    AnalysisCallbackGuardLease,
    AnalysisCallbackGuardSweepResult,
    AnalysisCallbackRequest,
    AnalysisCallbackWaitResult,
    AnalysisExecutionRef,
    AnalysisFilePreparationRequest,
    AnalysisInteractionAuditReceipt,
    AnalysisInteractionAuditRecord,
    AnalysisKnowledgeWriteRequest,
    AnalysisKnowledgeWriteResult,
    AnalysisRagCloseRequest,
    AnalysisRagCloseResult,
    AnalysisRagPort,
    AnalysisRagRequest,
    AnalysisRagResult,
    AnalysisRagSessionOpenRequest,
    AnalysisRagSessionOpenResult,
    AnalysisRecallAuditReceipt,
    AnalysisRecallAuditRecord,
    AnalysisResourceCommand,
    AnalysisResourceRecord,
    AnalysisResourceScanBatch,
    AnalysisTaskClaim,
    AnalysisTaskWorkspace,
    AnalysisTranslationRequest,
    AnalysisTranslationResult,
    FinalizeAnalysisRecallAudit,
    LoadAnalysisInteraction,
    PreparedAnalysisDocument,
    WaitForAnalysisCallbackRelease,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskExecutionSnapshot, TaskId
from app.modules.tasks.ports import (
    ExpectedProgressUpdate,
    ExpectedTaskCompletion,
    ProgressPublication,
    TaskClaimResult,
    TaskSubmissionCommand,
    TaskSubmissionResult,
)


_UNSET = object()


@dataclass(frozen=True)
class AnalysisFakeExpectation:
    """一项必须按所属队列顺序发生的 Port 调用。"""

    operation: str
    result: object
    expected_argument: object = _UNSET

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str) or not self.operation.strip():
            raise ValueError("operation 必须是非空 str")


class StrictAnalysisFakeScript:
    """线程安全的全局/按 execution 期望队列。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._expectations: deque[AnalysisFakeExpectation] = deque()
        self._expectations_by_key: dict[
            str,
            deque[AnalysisFakeExpectation],
        ] = {}
        self.calls: list[tuple[str, object | None]] = []

    def expect(
        self,
        operation: str,
        result: object = None,
        *,
        argument: object = _UNSET,
    ) -> None:
        """追加全局顺序期望，兼容单任务和生命周期测试。"""

        expectation = AnalysisFakeExpectation(operation, result, argument)
        with self._lock:
            self._expectations.append(expectation)

    def expect_for(
        self,
        correlation_key: str,
        operation: str,
        result: object = None,
        *,
        argument: object = _UNSET,
    ) -> None:
        """追加按任务隔离的期望；不同 key 的调用允许任意线程交错。"""

        if not isinstance(correlation_key, str) or not correlation_key.strip():
            raise ValueError("correlation_key 必须是非空 str")
        expectation = AnalysisFakeExpectation(operation, result, argument)
        with self._lock:
            queue = self._expectations_by_key.setdefault(
                correlation_key.strip(),
                deque(),
            )
            queue.append(expectation)

    def invoke(
        self,
        operation: str,
        argument: object | None = None,
        *,
        correlation_key: str | None = None,
    ) -> object:
        """消费所属队列的一项期望，并严格核验操作和可选参数。"""

        with self._lock:
            self.calls.append((operation, argument))
            queue = self._expectations
            normalized_key = str(correlation_key or "").strip()
            if normalized_key and normalized_key in self._expectations_by_key:
                queue = self._expectations_by_key[normalized_key]
            if not queue:
                raise AssertionError(
                    "StrictAnalysisFakeScript 收到未配置调用: "
                    f"operation={operation} correlation_key={normalized_key or '-'}"
                )
            expected = queue[0]
            if expected.operation != operation:
                raise AssertionError(
                    "StrictAnalysisFakeScript 调用顺序不匹配: "
                    f"expected={expected.operation} actual={operation} "
                    f"correlation_key={normalized_key or '-'}"
                )
            if (
                expected.expected_argument is not _UNSET
                and expected.expected_argument != argument
            ):
                raise AssertionError(
                    "StrictAnalysisFakeScript 调用参数不匹配: "
                    f"operation={operation} correlation_key={normalized_key or '-'}"
                )
            queue.popleft()
            if normalized_key and not queue:
                self._expectations_by_key.pop(normalized_key, None)
            if isinstance(expected.result, BaseException):
                raise expected.result
            return expected.result

    def assert_exhausted(self) -> None:
        """确保全局和全部任务队列都没有遗漏交互。"""

        with self._lock:
            remaining = [item.operation for item in self._expectations]
            keyed_remaining = {
                key: [item.operation for item in queue]
                for key, queue in self._expectations_by_key.items()
                if queue
            }
            if remaining or keyed_remaining:
                raise AssertionError(
                    "StrictAnalysisFakeScript 仍有未消费期望: "
                    f"global={remaining} by_key={keyed_remaining}"
                )


class StrictAnalysisPortFake:
    """一次性实现全部 Analysis Port，并对每个结果执行请求关联校验。"""

    def __init__(self, script: StrictAnalysisFakeScript | None = None) -> None:
        self.script = script or StrictAnalysisFakeScript()

    @staticmethod
    def _key_from_execution(execution: object) -> str:
        return str(execution.task_id)  # type: ignore[attr-defined]

    def create_batch_if_allowed(
        self,
        command: AnalysisBatchCommand,
    ) -> AnalysisBatchAdmission:
        self._require_argument(command, AnalysisBatchCommand, "command")
        result = self._require_result(
            self.script.invoke(
                "batch.create",
                command,
                correlation_key=f"batch:{command.trace_id}",
            ),
            AnalysisBatchAdmission,
            "batch.create",
        )
        if result.outcome is AnalysisBatchAdmissionOutcome.ACCEPTED:
            if len(result.executions) != len(command.submissions):
                raise AssertionError("batch.create execution 数量与 submissions 不一致")
            if tuple(item.file_name for item in result.executions) != tuple(
                item.file_name for item in command.submissions
            ):
                raise AssertionError("batch.create execution 文件顺序与 submissions 不一致")
        return result

    def load_input(self, task_id: TaskId) -> AnalysisTaskInputV1 | None:
        self._require_argument(task_id, TaskId, "task_id")
        result = self.script.invoke(
            "batch.load_input",
            task_id,
            correlation_key=str(task_id),
        )
        if result is not None and not isinstance(result, AnalysisTaskInputV1):
            raise AssertionError("batch.load_input 结果必须是 AnalysisTaskInputV1 或 None")
        if result is not None and result.task_id != task_id.value:
            raise AssertionError("batch.load_input 结果不属于请求 task_id")
        return result

    def claim_if_accepted(self, task_id: TaskId) -> AnalysisTaskClaim:
        self._require_argument(task_id, TaskId, "task_id")
        result = self._require_result(
            self.script.invoke(
                "batch.claim",
                task_id,
                correlation_key=str(task_id),
            ),
            AnalysisTaskClaim,
            "batch.claim",
        )
        if result.execution is not None and result.execution.task_id != task_id:
            raise AssertionError("batch.claim 结果不属于请求 task_id")
        return result

    def prepare(
        self,
        request: AnalysisFilePreparationRequest,
    ) -> PreparedAnalysisDocument:
        self._require_argument(request, AnalysisFilePreparationRequest, "request")
        result = self._require_result(
            self.script.invoke(
                "file.prepare",
                request,
                correlation_key=self._key_from_execution(request.execution),
            ),
            PreparedAnalysisDocument,
            "file.prepare",
        )
        self._require_same_execution(request.execution, result.execution, "file.prepare")
        return result

    def open_session(
        self,
        request: AnalysisRagSessionOpenRequest,
    ) -> AnalysisRagSessionOpenResult:
        self._require_argument(request, AnalysisRagSessionOpenRequest, "request")
        result = self._require_result(
            self.script.invoke(
                "rag.open_session",
                request,
                correlation_key=self._key_from_execution(request.execution),
            ),
            AnalysisRagSessionOpenResult,
            "rag.open_session",
        )
        self._require_same_execution(
            request.execution,
            result.session.execution,
            "rag.open_session",
        )
        return result

    def execute(self, request: AnalysisRagRequest) -> AnalysisRagResult:
        self._require_argument(request, AnalysisRagRequest, "request")
        result = self._require_result(
            self.script.invoke(
                "rag.execute",
                request,
                correlation_key=self._key_from_execution(request.execution),
            ),
            AnalysisRagResult,
            "rag.execute",
        )
        self._require_same_execution(request.execution, result.execution, "rag.execute")
        if result.operation is not request.operation or result.attempt_number != request.attempt_number:
            raise AssertionError("rag.execute 结果与请求 session/operation/attempt 不一致")
        # 首次 execute 会把打开阶段尚未绑定文档的 SessionRef 升级为已绑定引用；身份
        # 重选/抽取也可能切换 Conversation。严格 Fake 因此校验 execution、Context 与
        # 已绑定文档不漂移，而不是错误要求整个不可变 DTO 字节相等。
        if result.session.execution != request.session.execution:
            raise AssertionError("rag.execute 结果 Session execution 不一致")
        if result.session.context_ref != request.session.context_ref:
            raise AssertionError("rag.execute 结果 Session context 不一致")
        if request.session.document_bound:
            expected_document = (
                request.session.document_ref,
                request.session.document_location,
                request.session.content_sha256,
                request.session.ingested_file_name,
            )
            actual_document = (
                result.session.document_ref,
                result.session.document_location,
                result.session.content_sha256,
                result.session.ingested_file_name,
            )
            if actual_document != expected_document:
                raise AssertionError("rag.execute 结果 Session 文档身份不一致")
        return result

    def close_session(self, request: AnalysisRagCloseRequest) -> AnalysisRagCloseResult:
        self._require_argument(request, AnalysisRagCloseRequest, "request")
        result = self._require_result(
            self.script.invoke(
                "rag.close_session",
                request,
                correlation_key=self._key_from_execution(request.execution),
            ),
            AnalysisRagCloseResult,
            "rag.close_session",
        )
        self._require_same_execution(request.execution, result.execution, "rag.close_session")
        if result.session != request.session:
            raise AssertionError("rag.close_session 结果与请求 session 不一致")
        return result

    def persist(
        self,
        request: AnalysisKnowledgeWriteRequest,
    ) -> AnalysisKnowledgeWriteResult:
        self._require_argument(request, AnalysisKnowledgeWriteRequest, "request")
        result = self._require_result(
            self.script.invoke(
                "knowledge.persist",
                request,
                correlation_key=self._key_from_execution(request.execution),
            ),
            AnalysisKnowledgeWriteResult,
            "knowledge.persist",
        )
        self._require_same_execution(request.execution, result.execution, "knowledge.persist")
        if result.idempotency_key != request.idempotency_key:
            raise AssertionError("knowledge.persist 结果幂等键与请求不一致")
        return result

    def reserve_recall(
        self,
        record: AnalysisRecallAuditRecord,
    ) -> AnalysisRecallAuditReceipt:
        self._require_argument(record, AnalysisRecallAuditRecord, "record")
        result = self._require_result(
            self.script.invoke(
                "audit.reserve_recall",
                record,
                correlation_key=self._key_from_execution(record.execution),
            ),
            AnalysisRecallAuditReceipt,
            "audit.reserve_recall",
        )
        self._require_same_execution(record.execution, result.execution, "audit.reserve_recall")
        if result.idempotency_key != record.idempotency_key or result.finalized:
            raise AssertionError("audit.reserve_recall Receipt 与请求不一致")
        return result

    def finalize_recall(
        self,
        command: FinalizeAnalysisRecallAudit,
    ) -> AnalysisRecallAuditReceipt:
        self._require_argument(command, FinalizeAnalysisRecallAudit, "command")
        result = self._require_result(
            self.script.invoke(
                "audit.finalize_recall",
                command,
                correlation_key=self._key_from_execution(command.receipt.execution),
            ),
            AnalysisRecallAuditReceipt,
            "audit.finalize_recall",
        )
        self._require_same_execution(
            command.receipt.execution,
            result.execution,
            "audit.finalize_recall",
        )
        if (
            result.idempotency_key != command.receipt.idempotency_key
            or not result.finalized
            or result.version <= command.expected_version
        ):
            raise AssertionError("audit.finalize_recall Receipt 未正确推进")
        return result

    def persist_interaction(
        self,
        record: AnalysisInteractionAuditRecord,
    ) -> AnalysisInteractionAuditReceipt:
        self._require_argument(record, AnalysisInteractionAuditRecord, "record")
        result = self._require_result(
            self.script.invoke(
                "audit.persist_interaction",
                record,
                correlation_key=self._key_from_execution(record.execution),
            ),
            AnalysisInteractionAuditReceipt,
            "audit.persist_interaction",
        )
        self._require_same_execution(
            record.execution,
            result.execution,
            "audit.persist_interaction",
        )
        if result.idempotency_key != record.idempotency_key:
            raise AssertionError("audit.persist_interaction Receipt 幂等键不一致")
        return result

    def load_interaction(
        self,
        query: LoadAnalysisInteraction,
    ) -> AnalysisInteractionAuditReceipt | None:
        self._require_argument(query, LoadAnalysisInteraction, "query")
        result = self.script.invoke(
            "audit.load_interaction",
            query,
            correlation_key=self._key_from_execution(query.execution),
        )
        if result is not None and not isinstance(result, AnalysisInteractionAuditReceipt):
            raise AssertionError(
                "audit.load_interaction 结果必须是 AnalysisInteractionAuditReceipt 或 None"
            )
        if result is not None:
            self._require_same_execution(
                query.execution,
                result.execution,
                "audit.load_interaction",
            )
            if result.idempotency_key != query.idempotency_key:
                raise AssertionError("audit.load_interaction Receipt 幂等键不一致")
        return result

    def append_lifecycle_events(
        self,
        command: AppendAnalysisLifecycleEvents,
    ) -> None:
        self._require_argument(command, AppendAnalysisLifecycleEvents, "command")
        result = self.script.invoke(
            "audit.append_lifecycle_events",
            command,
            correlation_key=self._key_from_execution(command.receipt.execution),
        )
        if result is not None:
            raise AssertionError("audit.append_lifecycle_events 结果必须是 None")

    def translate(
        self,
        request: AnalysisTranslationRequest,
    ) -> AnalysisTranslationResult:
        self._require_argument(request, AnalysisTranslationRequest, "request")
        result = self._require_result(
            self.script.invoke(
                "translation.translate",
                request,
                correlation_key=self._key_from_execution(request.execution),
            ),
            AnalysisTranslationResult,
            "translation.translate",
        )
        self._require_same_execution(
            request.execution,
            result.execution,
            "translation.translate",
        )
        return result

    def create(self, command: AnalysisResourceCommand) -> AnalysisResourceRecord:
        return self._resource_write("resource.create", command)

    def get(
        self,
        execution: AnalysisExecutionRef,
    ) -> AnalysisResourceRecord | None:
        self._require_argument(execution, AnalysisExecutionRef, "execution")
        result = self.script.invoke(
            "resource.get",
            execution,
            correlation_key=self._key_from_execution(execution),
        )
        if result is not None and not isinstance(result, AnalysisResourceRecord):
            raise AssertionError("resource.get 结果必须是 AnalysisResourceRecord 或 None")
        if result is not None:
            self._require_same_execution(execution, result.execution, "resource.get")
        return result

    def advance(self, command: AnalysisResourceCommand) -> AnalysisResourceRecord:
        return self._resource_write("resource.advance", command)

    def _resource_write(
        self,
        operation: str,
        command: AnalysisResourceCommand,
    ) -> AnalysisResourceRecord:
        self._require_argument(command, AnalysisResourceCommand, "command")
        result = self._require_result(
            self.script.invoke(
                operation,
                command,
                correlation_key=self._key_from_execution(command.execution),
            ),
            AnalysisResourceRecord,
            operation,
        )
        self._require_same_execution(command.execution, result.execution, operation)
        if result.state is not command.target_state:
            raise AssertionError(f"{operation} 结果状态与 target_state 不一致")
        if command.expected_version is None:
            if result.version != 0:
                raise AssertionError("resource.create 初始 version 必须为 0")
        elif result.version <= command.expected_version:
            raise AssertionError(f"{operation} 结果 version 未推进")
        return result

    def list_recoverable(self, *, limit: int) -> AnalysisResourceScanBatch:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        result = self.script.invoke("resource.list_recoverable", limit)
        if not isinstance(result, AnalysisResourceScanBatch):
            raise AssertionError(
                "resource.list_recoverable 结果必须是 AnalysisResourceScanBatch"
            )
        if (
            len(result.records)
            + result.quarantined_count
            + result.pending_count
            > limit
        ):
            raise AssertionError("resource.list_recoverable 结果超过 limit")
        return result

    def defer_recovery(
        self,
        execution: AnalysisExecutionRef,
        *,
        expected_version: int,
        retry_at: str,
        reason: str,
    ) -> AnalysisResourceRecord:
        self._require_argument(execution, AnalysisExecutionRef, "execution")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError("expected_version 必须是非负整数")
        if not isinstance(retry_at, str) or not retry_at.strip():
            raise ValueError("retry_at 必须是非空 str")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须是非空 str")
        argument = (execution, expected_version, retry_at, reason)
        result = self._require_result(
            self.script.invoke(
                "resource.defer_recovery",
                argument,
                correlation_key=self._key_from_execution(execution),
            ),
            AnalysisResourceRecord,
            "resource.defer_recovery",
        )
        self._require_same_execution(execution, result.execution, "resource.defer_recovery")
        if result.version <= expected_version:
            raise AssertionError("resource.defer_recovery 结果 version 未推进")
        return result

    def quarantine_recovery_record(
        self,
        execution: AnalysisExecutionRef,
        *,
        expected_state: AnalysisResourceState,
        expected_version: int,
        reason: str,
    ) -> bool:
        self._require_argument(execution, AnalysisExecutionRef, "execution")
        argument = (execution, expected_state, expected_version, reason)
        result = self.script.invoke(
            "resource.quarantine_recovery_record",
            argument,
            correlation_key=self._key_from_execution(execution),
        )
        if not isinstance(result, bool):
            raise AssertionError(
                "resource.quarantine_recovery_record 结果必须是 bool"
            )
        return result

    def acquire(self, request: AnalysisCallbackRequest) -> AnalysisCallbackAcquireResult:
        self._require_argument(request, AnalysisCallbackRequest, "request")
        result = self._require_result(
            self.script.invoke(
                "callback.acquire",
                request,
                correlation_key=self._key_from_execution(request.execution),
            ),
            AnalysisCallbackAcquireResult,
            "callback.acquire",
        )
        self._require_same_execution(
            request.execution,
            result.execution,
            "callback.acquire",
        )
        if result.lease is not None:
            self._require_same_execution(
                request.execution,
                result.lease.execution,
                "callback.acquire",
            )
        return result

    def wait_until_released(
        self,
        request: WaitForAnalysisCallbackRelease,
    ) -> AnalysisCallbackWaitResult:
        self._require_argument(request, WaitForAnalysisCallbackRelease, "request")
        result = self._require_result(
            self.script.invoke(
                "callback.wait",
                request,
                correlation_key=self._key_from_execution(request.execution),
            ),
            AnalysisCallbackWaitResult,
            "callback.wait",
        )
        self._require_same_execution(request.execution, result.execution, "callback.wait")
        return result

    def deliver(
        self,
        request: AnalysisCallbackDeliveryRequest,
    ) -> AnalysisCallbackDelivery:
        self._require_argument(request, AnalysisCallbackDeliveryRequest, "request")
        result = self._require_result(
            self.script.invoke(
                "callback.deliver",
                request,
                correlation_key=self._key_from_execution(request.lease.execution),
            ),
            AnalysisCallbackDelivery,
            "callback.deliver",
        )
        self._require_same_execution(
            request.lease.execution,
            result.execution,
            "callback.deliver",
        )
        if (
            result.lease_token != request.lease.lease_token
            or result.lease_version != request.lease.lease_version
        ):
            raise AssertionError("callback.deliver 结果 lease 与请求不一致")
        return result

    def complete(
        self,
        lease: AnalysisCallbackGuardLease,
        delivery: AnalysisCallbackDelivery,
        payload: FrozenJsonObject,
    ) -> bool:
        self._require_argument(lease, AnalysisCallbackGuardLease, "lease")
        self._require_argument(delivery, AnalysisCallbackDelivery, "delivery")
        self._require_argument(payload, FrozenJsonObject, "payload")
        self._require_same_execution(lease.execution, delivery.execution, "callback.complete")
        return self._require_result(
            self.script.invoke(
                "callback.complete",
                (lease, delivery, payload),
                correlation_key=self._key_from_execution(lease.execution),
            ),
            bool,
            "callback.complete",
        )

    def freeze_expired(self, *, limit: int) -> AnalysisCallbackGuardSweepResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        return self._require_result(
            self.script.invoke("callback.freeze_expired", limit),
            AnalysisCallbackGuardSweepResult,
            "callback.freeze_expired",
        )

    def wake_up(self) -> None:
        self._require_none_result(self.script.invoke("dispatcher.wake_up"), "dispatcher.wake_up")

    def start(self) -> None:
        self._require_none_result(self.script.invoke("dispatcher.start"), "dispatcher.start")

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or timeout_seconds != timeout_seconds
            or timeout_seconds in (float("inf"), float("-inf"))
        ):
            raise ValueError("timeout_seconds 必须是有限正数或 None")
        return self._require_result(
            self.script.invoke("dispatcher.stop", timeout_seconds),
            bool,
            "dispatcher.stop",
        )

    def close(self) -> None:
        self._require_none_result(self.script.invoke("dispatcher.close"), "dispatcher.close")

    @staticmethod
    def _require_same_execution(expected, actual, operation: str) -> None:
        if expected != actual:
            raise AssertionError(f"StrictAnalysisPortFake {operation} execution 不一致")

    @staticmethod
    def _require_argument(argument: object, expected_type: type[Any], name: str) -> None:
        if not isinstance(argument, expected_type):
            raise TypeError(f"{name} 必须是 {expected_type.__name__}")

    @staticmethod
    def _require_result(result: object, expected_type: type[Any], operation: str):
        if not isinstance(result, expected_type):
            raise AssertionError(
                f"StrictAnalysisPortFake {operation} 结果必须是 {expected_type.__name__}"
            )
        return result

    @staticmethod
    def _require_none_result(result: object, operation: str) -> None:
        if result is not None:
            raise AssertionError(f"StrictAnalysisPortFake {operation} 结果必须是 None")


class StrictAnalysisTaskWorkspaceFake:
    """供 RunAnalysisTask 使用的严格任务目录 Fake，不访问真实文件系统。"""

    def __init__(self, script: StrictAnalysisFakeScript) -> None:
        self.script = script

    def create(self, execution: AnalysisExecutionRef) -> AnalysisTaskWorkspace:
        if not isinstance(execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        result = self.script.invoke(
            "workspace.create",
            execution,
            correlation_key=str(execution.task_id),
        )
        if not isinstance(result, AnalysisTaskWorkspace):
            raise AssertionError("workspace.create 结果必须是 AnalysisTaskWorkspace")
        if result.execution != execution:
            raise AssertionError("workspace.create 结果 execution 不一致")
        return result


class StrictAnalysisRagFactoryFake:
    """为每个 execution 显式发放同一严格 RAG Fake 的短生命周期租约。"""

    def __init__(
        self,
        script: StrictAnalysisFakeScript,
        rag: StrictAnalysisPortFake,
    ) -> None:
        if not isinstance(rag, AnalysisRagPort):
            raise TypeError("rag 必须实现 AnalysisRagPort")
        self.script = script
        self._rag = rag

    @contextmanager
    def create(self, execution: AnalysisExecutionRef) -> Iterator[AnalysisRagPort]:
        if not isinstance(execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        result = self.script.invoke(
            "rag.factory.create",
            execution,
            correlation_key=str(execution.task_id),
        )
        if result is not None:
            raise AssertionError("rag.factory.create 结果必须是 None")
        yield self._rag


class StrictAnalysisTaskCommandFake:
    """RunAnalysisTask 所需通用 TaskCommandPort 的严格零 I/O Fake。"""

    def __init__(self, script: StrictAnalysisFakeScript) -> None:
        self.script = script

    def create_if_allowed(self, command: TaskSubmissionCommand[object]) -> TaskSubmissionResult[AnalysisTaskInputV1]:
        result = self.script.invoke("task.create", command)
        if not isinstance(result, TaskSubmissionResult):
            raise AssertionError("task.create 结果必须是 TaskSubmissionResult")
        return result

    def get_execution(self, task_id: TaskId) -> TaskExecutionSnapshot[AnalysisTaskInputV1] | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        result = self.script.invoke("task.get", task_id, correlation_key=str(task_id))
        if result is not None and not isinstance(result, TaskExecutionSnapshot):
            raise AssertionError("task.get 结果必须是 TaskExecutionSnapshot 或 None")
        if result is not None and result.task_id != task_id:
            raise AssertionError("task.get 结果 task_id 不一致")
        return result

    def claim(self, task_id: TaskId) -> TaskClaimResult[AnalysisTaskInputV1]:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        result = self.script.invoke("task.claim", task_id, correlation_key=str(task_id))
        if not isinstance(result, TaskClaimResult):
            raise AssertionError("task.claim 结果必须是 TaskClaimResult")
        if result.execution is not None and result.execution.task_id != task_id:
            raise AssertionError("task.claim 结果 task_id 不一致")
        return result

    def update_progress_if_current(self, update: ExpectedProgressUpdate) -> bool:
        if not isinstance(update, ExpectedProgressUpdate):
            raise TypeError("update 必须是 ExpectedProgressUpdate")
        result = self.script.invoke(
            "task.progress",
            update,
            correlation_key=str(update.expected_task_id),
        )
        if not isinstance(result, bool):
            raise AssertionError("task.progress 结果必须是 bool")
        return result

    def finish_if_current(self, completion: ExpectedTaskCompletion[object]) -> bool:
        if not isinstance(completion, ExpectedTaskCompletion):
            raise TypeError("completion 必须是 ExpectedTaskCompletion")
        result = self.script.invoke(
            "task.finish",
            completion,
            correlation_key=str(completion.expected_task_id),
        )
        if not isinstance(result, bool):
            raise AssertionError("task.finish 结果必须是 bool")
        return result

    def is_latest(self, task_id: TaskId, business_ref: TaskBusinessRef) -> bool:
        if not isinstance(task_id, TaskId) or not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("task_id 与 business_ref 类型无效")
        result = self.script.invoke(
            "task.is_latest",
            (task_id, business_ref),
            correlation_key=str(task_id),
        )
        if not isinstance(result, bool):
            raise AssertionError("task.is_latest 结果必须是 bool")
        return result

    def list_accepted(self, task_type: str, *, limit: int) -> tuple[TaskId, ...]:
        result = self.script.invoke("task.list_accepted", (task_type, limit))
        if not isinstance(result, tuple) or any(not isinstance(item, TaskId) for item in result):
            raise AssertionError("task.list_accepted 结果必须是 TaskId tuple")
        return result

    def defer_accepted(self, task_id: TaskId, *, retry_at: str, reason: str) -> bool:
        result = self.script.invoke(
            "task.defer_accepted",
            (task_id, retry_at, reason),
            correlation_key=str(task_id),
        )
        if not isinstance(result, bool):
            raise AssertionError("task.defer_accepted 结果必须是 bool")
        return result


class StrictAnalysisGuardedProgressFake:
    """严格模拟 Guarded Progress 发布，真实调用 Application 注入的 latest 复核。"""

    def __init__(self, script: StrictAnalysisFakeScript) -> None:
        self.script = script

    def publish_guarded(self, publication: ProgressPublication, *, is_current) -> bool:  # type: ignore[no-untyped-def]
        if not isinstance(publication, ProgressPublication):
            raise TypeError("publication 必须是 ProgressPublication")
        if not callable(is_current):
            raise TypeError("is_current 必须可调用")
        configured = self.script.invoke(
            "progress.publish",
            publication,
            correlation_key=str(publication.expected_task_id),
        )
        if not isinstance(configured, bool):
            raise AssertionError("progress.publish 结果必须是 bool")
        current = is_current()
        if not isinstance(current, bool):
            raise AssertionError("Progress is_current 必须返回 bool")
        return configured and current


__all__ = (
    "AnalysisFakeExpectation",
    "StrictAnalysisGuardedProgressFake",
    "StrictAnalysisRagFactoryFake",
    "StrictAnalysisFakeScript",
    "StrictAnalysisPortFake",
    "StrictAnalysisTaskCommandFake",
    "StrictAnalysisTaskWorkspaceFake",
)
