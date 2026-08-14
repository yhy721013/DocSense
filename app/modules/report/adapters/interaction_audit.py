"""报告 RAG trace 到现有 SQLite 原子交互审计入口的适配器。"""

from __future__ import annotations

import logging
from pathlib import Path
import sqlite3
from typing import Protocol, runtime_checkable

from app.modules.report.domain.errors import ReportAuditError
from app.modules.report.ports import (
    AppendReportLifecycleEvents,
    PersistReportRagTrace,
    ReportAuditReceipt,
    ReportRagAuditOutcome,
)
from app.ports.rag import RagAttempt, RagExecutionTrace, RagLifecycleEvent, RagSource
from app.infrastructure.observability.llm_interaction_store import (
    AUDIT_STATUS_SUCCEEDED,
    LLMInteractionStore,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import TaskReadPort


logger = logging.getLogger(__name__)


@runtime_checkable
class _InteractionAuditBackend(Protocol):
    """迁移期最小审计边界；旧 Service 只能单向委托到共享 Store。"""

    def create_llm_interaction_with_trace(self, **kwargs): ...
    def append_llm_interaction_lifecycle_events(self, *args, **kwargs): ...


class SQLiteReportInteractionAuditAdapter:
    """复用 ``LLMTaskService`` 的单事务审计写入口。

    Adapter 只做 DTO 映射，不直接执行 SQL。主交互、全部模型尝试和初始生命周期事件由
    TaskService 在一个 ``BEGIN IMMEDIATE`` 事务中提交；只有收到成功凭据，报告用例才可
    进入成功终态或清理 AnythingLLM 现场。
    """

    def __init__(self, task_service: _InteractionAuditBackend) -> None:
        if not isinstance(task_service, _InteractionAuditBackend):
            raise TypeError("task_service 必须实现共享交互审计边界")
        self._task_service = task_service

    def persist_trace(self, command: PersistReportRagTrace) -> ReportAuditReceipt:
        """无损保存 report trace_id/call_id、来源统计与资源生命周期。"""

        if not isinstance(command, PersistReportRagTrace):
            raise TypeError("command 必须是 PersistReportRagTrace")
        trace = self._to_shared_trace(command)
        status = command.outcome.value
        error_message = (
            trace.error_message or command.error_code
            if command.outcome is ReportRagAuditOutcome.FAILED
            else ""
        )
        try:
            result = self._task_service.create_llm_interaction_with_trace(
                business_type=command.business_ref.business_type,
                business_key=command.business_ref.business_key,
                execution_id=command.task_id.value,
                prompt=command.prompt,
                trace=trace,
                status=status,
                error_message=error_message,
                audit_idempotency_key=command.idempotency_key,
            )
        except Exception as exc:
            logger.critical(
                "报告 RAG 交互审计事务失败: task_id=%s trace_id=%s error_type=%s",
                command.task_id,
                command.trace.trace_id,
                type(exc).__name__,
                exc_info=True,
            )
            raise ReportAuditError("报告RAG交互审计未能原子提交") from exc
        if result.audit_status != AUDIT_STATUS_SUCCEEDED:
            raise ReportAuditError("报告RAG交互审计未返回成功门禁")
        logger.info(
            "报告 RAG 交互审计已提交: task_id=%s trace_id=%s audit_id=%s "
            "created=%s reused=%s",
            command.task_id,
            command.trace.trace_id,
            result.interaction_id,
            result.created,
            result.reused,
        )
        return ReportAuditReceipt(
            task_id=command.task_id,
            idempotency_key=command.idempotency_key,
            audit_id=str(result.interaction_id),
        )

    def append_lifecycle_events(
        self,
        command: AppendReportLifecycleEvents,
    ) -> None:
        """幂等追加审计后发生的清理事件，并同步收敛 cleanup 状态。"""

        if not isinstance(command, AppendReportLifecycleEvents):
            raise TypeError("command 必须是 AppendReportLifecycleEvents")
        try:
            interaction_id = int(command.receipt.audit_id)
        except (TypeError, ValueError) as exc:
            raise ReportAuditError("报告审计凭据 ID 无效") from exc
        if interaction_id < 1:
            raise ReportAuditError("报告审计凭据 ID 无效")

        shared_events = tuple(
            RagLifecycleEvent(
                sequence_no=event.sequence_no,
                operation=event.operation,
                attempt=event.attempt_no,
                success=event.success,
                external_ref=event.external_ref,
                failure_stage=event.failure_stage,
                error_message=event.error_message,
            )
            for event in command.events
        )
        deletion_events = tuple(
            event
            for event in shared_events
            if event.operation
            in {
                "conversation_delete",
                "context_delete",
                "global_document_delete",
            }
        )
        if not deletion_events:
            raise ReportAuditError("报告清理审计缺少资源删除事件")
        failed_deletions = tuple(event for event in deletion_events if not event.success)
        cleanup_status = "failed" if failed_deletions else "deleted"
        cleanup_error = (
            "；".join(
                dict.fromkeys(
                    event.error_message or "外部资源删除失败"
                    for event in failed_deletions
                )
            )
            if failed_deletions
            else ""
        )
        try:
            self._task_service.append_llm_interaction_lifecycle_events(
                interaction_id,
                shared_events,
                cleanup_status=cleanup_status,
                cleanup_error=cleanup_error,
                expected_execution_id=command.receipt.task_id.value,
                expected_audit_idempotency_key=(
                    command.receipt.idempotency_key
                ),
            )
        except Exception as exc:
            logger.error(
                "报告 RAG 清理生命周期审计追加失败: task_id=%s audit_id=%s "
                "event_count=%d error_type=%s",
                command.receipt.task_id,
                interaction_id,
                len(shared_events),
                type(exc).__name__,
                exc_info=True,
            )
            raise ReportAuditError("报告RAG清理生命周期审计追加失败") from exc
        logger.info(
            "报告 RAG 清理生命周期审计已追加: task_id=%s audit_id=%s "
            "event_count=%d cleanup_status=%s",
            command.receipt.task_id,
            interaction_id,
            len(shared_events),
            cleanup_status,
        )

    @staticmethod
    def _to_shared_trace(command: PersistReportRagTrace) -> RagExecutionTrace:
        report_trace = command.trace
        attempts = tuple(
            RagAttempt(
                operation=attempt.operation,
                attempt=attempt.attempt_no,
                prompt_kind=attempt.prompt_kind,
                raw_response=attempt.raw_response,
                sources=tuple(
                    RagSource(
                        document_ref=source.document_ref,
                        text=source.text,
                        id=source.source_id,
                        title=source.title,
                        url=source.url,
                        score=source.score,
                    )
                    for source in attempt.sources
                ),
                failure_stage=attempt.failure_stage,
                error_message=attempt.error_message,
                prompt_digest=attempt.prompt_digest,
                query_mode=attempt.query_mode,
                source_count=attempt.source_count or 0,
                verified_source_count=attempt.verified_source_count or 0,
                missing_marker_count=attempt.missing_marker_count,
                mismatched_marker_count=attempt.mismatched_marker_count,
                source_marker_status=attempt.source_marker_status,
                call_id=attempt.call_id,
            )
            for attempt in report_trace.attempts
        )
        lifecycle_events = tuple(
            RagLifecycleEvent(
                sequence_no=event.sequence_no,
                operation=event.operation,
                attempt=event.attempt_no,
                success=event.success,
                external_ref=event.external_ref,
                failure_stage=event.failure_stage,
                error_message=event.error_message,
            )
            for event in report_trace.lifecycle_events
        )
        return RagExecutionTrace(
            context_name=report_trace.context_name,
            context_ref=report_trace.context_ref,
            conversation_ref=report_trace.conversation_ref,
            attempts=attempts,
            failure_stage=report_trace.failure_stage,
            error_message=report_trace.error_message,
            lifecycle_events=lifecycle_events,
            trace_id=report_trace.trace_id,
        )


def build_report_v2_interaction_audit_adapter(
    database_path: str | Path,
    *,
    task_reader: TaskReadPort,
) -> SQLiteReportInteractionAuditAdapter:
    """构造共享审计 Writer，并用 v2 TaskId 精确校验 Report 身份。

    审计与 Task Control 位于两个 SQLite 文件，因此这里只做执行前身份门禁；Runner
    已用持久 Step intent/checkpoint 隔离跨库提交结果未知，不能把本检查描述成原子事务。
    """

    if not isinstance(task_reader, TaskReadPort):
        raise TypeError("task_reader 必须实现 TaskReadPort")
    resolved = Path(database_path).resolve()

    def connection_factory(timeout_seconds: float) -> sqlite3.Connection:
        connection = sqlite3.connect(resolved, timeout=max(0.0, timeout_seconds))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute(
            f"PRAGMA busy_timeout = {int(max(0.0, timeout_seconds) * 1000)}"
        )
        return connection

    def validate_identity(
        business_type: str,
        business_key: str,
        execution_id: str,
    ) -> bool:
        if business_type != "report":
            return False
        try:
            snapshot = task_reader.get_by_id(TaskId(execution_id))
        except (TypeError, ValueError):
            return False
        return (
            snapshot is not None
            and snapshot.business_ref == TaskBusinessRef(business_type, business_key)
        )

    return SQLiteReportInteractionAuditAdapter(
        LLMInteractionStore(
            connection_factory,
            task_identity_validator=validate_identity,
        )
    )


__all__ = [
    "SQLiteReportInteractionAuditAdapter",
    "build_report_v2_interaction_audit_adapter",
]
