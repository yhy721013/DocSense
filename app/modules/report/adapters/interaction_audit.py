"""报告 RAG trace 到现有 SQLite 原子交互审计入口的适配器。"""

from __future__ import annotations

import logging

from app.modules.report.domain.errors import ReportAuditError
from app.modules.report.ports import (
    AppendReportLifecycleEvents,
    PersistReportRagTrace,
    ReportAuditReceipt,
    ReportRagAuditOutcome,
)
from app.ports.rag import RagAttempt, RagExecutionTrace, RagLifecycleEvent, RagSource
from app.services.llm_service.interaction_audit_service import (
    AUDIT_STATUS_SUCCEEDED,
)
from app.services.llm_service.task_service import LLMTaskService


logger = logging.getLogger(__name__)


class SQLiteReportInteractionAuditAdapter:
    """复用 ``LLMTaskService`` 的单事务审计写入口。

    Adapter 只做 DTO 映射，不直接执行 SQL。主交互、全部模型尝试和初始生命周期事件由
    TaskService 在一个 ``BEGIN IMMEDIATE`` 事务中提交；只有收到成功凭据，报告用例才可
    进入成功终态或清理 AnythingLLM 现场。
    """

    def __init__(self, task_service: LLMTaskService) -> None:
        if not isinstance(task_service, LLMTaskService):
            raise TypeError("task_service 必须是 LLMTaskService")
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


__all__ = ["SQLiteReportInteractionAuditAdapter"]
