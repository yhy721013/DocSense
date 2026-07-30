"""将既有 ``LLMTaskService`` 审计能力适配为 Analysis 任务级 Port。

Analysis Application 只看到不可变 DTO，不知道 SQLite 表、旧 Trace 结构或供应商 RAG
对象。本适配器负责把新旧 DTO 一次性映射到既有原子审计入口；任何映射或持久化失败都
直接抛出，调用方据此阻断永久知识库写入并保留外部现场。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from app.modules.analysis.ports.audit import (
    AnalysisAuditOutcome,
    AnalysisAuditPort,
    AnalysisInteractionAuditReceipt,
    AnalysisInteractionAuditRecord,
    AnalysisRecallAuditReceipt,
    AnalysisRecallAuditRecord,
    AppendAnalysisLifecycleEvents,
    FinalizeAnalysisRecallAudit,
    LoadAnalysisInteraction,
)
from app.modules.analysis.ports.rag import (
    AnalysisRagLifecycleEvent,
    AnalysisRagLifecycleOutcome,
    AnalysisRagOperation,
    AnalysisRagSource,
)
from app.ports import RagAttempt, RagExecutionTrace, RagLifecycleEvent, RagPromptKind, RagSource
from app.services.llm_service.interaction_audit_service import AUDIT_STATUS_SUCCEEDED
from app.services.llm_service.task_service import LLMTaskService


logger = logging.getLogger(__name__)


class LegacyAnalysisAuditAdapterError(RuntimeError):
    """旧审计存储无法安全表达当前 Analysis 事实。"""


class LegacyAnalysisAuditAdapter(AnalysisAuditPort):
    """复用现有召回与交互审计事务，保持 Analysis 的 hard gate 语义。

    这里不会直接执行 SQL，也不会在审计失败时伪造 Receipt。召回审计的旧表没有独立
    自增 ID，因此使用 execution 派生的稳定内部标识；交互审计则透传已提交的自增 ID。
    两者都只在本进程内部的 Port 边界流动，绝不进入 HTTP、Progress 或 Callback。
    """

    _RECALL_REQUIRED_KEYS = frozenset(
        {
            "tree_fingerprint",
            "query_digest",
            "base_top64",
            "final_candidates",
            "channel_rankings",
            "rrf_scores",
            "protected_reasons",
            "prompt_chars",
            "recall_elapsed_ms",
        }
    )
    _RECALL_FINALIZE_REQUIRED_KEYS = frozenset(
        {
            "returned_architecture_id",
            "returned_rank",
            "total_elapsed_ms",
            "failure_stage",
            "error_message",
        }
    )

    def __init__(self, task_service: LLMTaskService) -> None:
        if not isinstance(task_service, LLMTaskService):
            raise TypeError("task_service 必须是 LLMTaskService")
        self._task_service = task_service

    def reserve_recall(
        self,
        record: AnalysisRecallAuditRecord,
    ) -> AnalysisRecallAuditReceipt:
        """在创建远端 RAG Session 前，原子保存不可变召回决策。"""

        if not isinstance(record, AnalysisRecallAuditRecord):
            raise TypeError("record 必须是 AnalysisRecallAuditRecord")
        payload = self._recall_payload(record.payload.to_dict())
        try:
            written = self._task_service.upsert_architecture_recall_decision(
                execution_id=record.execution.task_id.value,
                **payload,
            )
        except Exception as exc:
            logger.critical(
                "文件分析召回审计预留失败，禁止创建远端会话: task_id=%s error_type=%s",
                record.execution.task_id,
                type(exc).__name__,
                exc_info=True,
            )
            raise LegacyAnalysisAuditAdapterError("文件分析召回审计预留失败") from exc
        self._require_recall_write_execution(written, record)
        receipt = AnalysisRecallAuditReceipt(
            execution=record.execution,
            idempotency_key=record.idempotency_key,
            audit_id=self._recall_audit_id(record.execution.task_id.value),
            version=1 if bool(getattr(written, "finalized", False)) else 0,
            finalized=bool(getattr(written, "finalized", False)),
        )
        logger.info(
            "文件分析召回审计已预留: task_id=%s reused=%s finalized=%s",
            record.execution.task_id,
            bool(getattr(written, "reused", False)),
            receipt.finalized,
        )
        return receipt

    def finalize_recall(
        self,
        command: FinalizeAnalysisRecallAudit,
    ) -> AnalysisRecallAuditReceipt:
        """按 Receipt 版本终结召回审计，不覆盖初始候选证据。"""

        if not isinstance(command, FinalizeAnalysisRecallAudit):
            raise TypeError("command 必须是 FinalizeAnalysisRecallAudit")
        payload = self._recall_finalize_payload(command.payload.to_dict())
        if command.outcome is AnalysisAuditOutcome.SUCCEEDED:
            if payload["failure_stage"] is not None or payload["error_message"]:
                raise LegacyAnalysisAuditAdapterError("成功召回终结不得携带失败字段")
        else:
            if not payload["failure_stage"] or not payload["error_message"]:
                raise LegacyAnalysisAuditAdapterError("失败召回终结缺少失败字段")
        try:
            written = self._task_service.finalize_architecture_recall_decision(
                execution_id=command.receipt.execution.task_id.value,
                **payload,
            )
        except Exception as exc:
            logger.critical(
                "文件分析召回审计终结失败: task_id=%s outcome=%s error_type=%s",
                command.receipt.execution.task_id,
                command.outcome.value,
                type(exc).__name__,
                exc_info=True,
            )
            raise LegacyAnalysisAuditAdapterError("文件分析召回审计终结失败") from exc
        self._require_recall_write_execution(written, command.receipt)
        logger.info(
            "文件分析召回审计已终结: task_id=%s outcome=%s reused=%s",
            command.receipt.execution.task_id,
            command.outcome.value,
            bool(getattr(written, "reused", False)),
        )
        return AnalysisRecallAuditReceipt(
            execution=command.receipt.execution,
            idempotency_key=command.receipt.idempotency_key,
            audit_id=command.receipt.audit_id,
            version=command.expected_version + 1,
            finalized=True,
        )

    def persist_interaction(
        self,
        record: AnalysisInteractionAuditRecord,
    ) -> AnalysisInteractionAuditReceipt:
        """原子保存所有模型 attempt 与打开阶段生命周期事实。"""

        if not isinstance(record, AnalysisInteractionAuditRecord):
            raise TypeError("record 必须是 AnalysisInteractionAuditRecord")
        trace = self._to_shared_trace(record)
        status = record.outcome.value
        error_message = record.error_code if record.outcome is AnalysisAuditOutcome.FAILED else ""
        try:
            result = self._task_service.create_llm_interaction_with_trace(
                business_type="file",
                business_key=record.execution.file_name,
                execution_id=record.execution.task_id.value,
                prompt=record.prompt,
                trace=trace,
                status=status,
                error_message=error_message,
                audit_idempotency_key=record.idempotency_key,
                document_upload_facts=(
                    record.document_upload.to_dict()
                    if record.document_upload is not None
                    else None
                ),
            )
        except Exception as exc:
            logger.critical(
                "文件分析交互审计失败，禁止永久知识入库: task_id=%s trace_id=%s error_type=%s",
                record.execution.task_id,
                record.trace_id,
                type(exc).__name__,
                exc_info=True,
            )
            raise LegacyAnalysisAuditAdapterError("文件分析交互审计未能原子提交") from exc
        if getattr(result, "audit_status", None) != AUDIT_STATUS_SUCCEEDED:
            raise LegacyAnalysisAuditAdapterError("文件分析交互审计未返回成功门禁")
        receipt = AnalysisInteractionAuditReceipt(
            execution=record.execution,
            idempotency_key=record.idempotency_key,
            audit_id=str(getattr(result, "interaction_id", "") or ""),
        )
        logger.info(
            "文件分析交互审计已提交: task_id=%s audit_id=%s attempts=%d lifecycle=%d",
            record.execution.task_id,
            receipt.audit_id,
            len(record.attempts),
            len(record.lifecycle_events),
        )
        return receipt

    def load_interaction(
        self,
        query: LoadAnalysisInteraction,
    ) -> AnalysisInteractionAuditReceipt | None:
        """按 execution 与幂等键联合查回已提交交互，避免跨任务串读。"""

        if not isinstance(query, LoadAnalysisInteraction):
            raise TypeError("query 必须是 LoadAnalysisInteraction")
        try:
            row = self._task_service.get_llm_interaction_by_execution(
                "file",
                query.execution.file_name,
                query.execution.task_id.value,
                query.idempotency_key,
            )
        except Exception as exc:
            logger.exception(
                "文件分析交互审计查回失败: task_id=%s error_type=%s",
                query.execution.task_id,
                type(exc).__name__,
            )
            raise LegacyAnalysisAuditAdapterError("文件分析交互审计查回失败") from exc
        if row is None:
            return None
        audit_id = str(row.get("id") or "").strip()
        if not audit_id:
            raise LegacyAnalysisAuditAdapterError("文件分析交互审计查回缺少 ID")
        return AnalysisInteractionAuditReceipt(
            execution=query.execution,
            idempotency_key=query.idempotency_key,
            audit_id=audit_id,
        )

    def append_lifecycle_events(
        self,
        command: AppendAnalysisLifecycleEvents,
    ) -> None:
        """幂等追加审计提交后的 close 事件，并同步旧 cleanup 状态。"""

        if not isinstance(command, AppendAnalysisLifecycleEvents):
            raise TypeError("command 必须是 AppendAnalysisLifecycleEvents")
        try:
            interaction_id = int(command.receipt.audit_id)
        except (TypeError, ValueError) as exc:
            raise LegacyAnalysisAuditAdapterError("文件分析审计凭据 ID 无效") from exc
        if interaction_id < 1:
            raise LegacyAnalysisAuditAdapterError("文件分析审计凭据 ID 无效")
        shared_events = tuple(self._to_shared_lifecycle_event(item) for item in command.events)
        deletion_events = tuple(
            item
            for item in shared_events
            if item.operation
            in {"conversation_delete", "context_delete", "global_document_delete"}
        )
        if not deletion_events:
            raise LegacyAnalysisAuditAdapterError("文件分析清理审计缺少资源删除事件")
        failed_deletions = tuple(item for item in deletion_events if not item.success)
        cleanup_status = "failed" if failed_deletions else "deleted"
        cleanup_error = (
            "；".join(
                dict.fromkeys(
                    item.error_message or "文件分析外部资源删除失败"
                    for item in failed_deletions
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
                expected_execution_id=command.receipt.execution.task_id.value,
                expected_audit_idempotency_key=command.receipt.idempotency_key,
            )
        except Exception as exc:
            logger.critical(
                "文件分析 RAG 关闭审计追加失败，保留恢复现场: task_id=%s audit_id=%s error_type=%s",
                command.receipt.execution.task_id,
                interaction_id,
                type(exc).__name__,
                exc_info=True,
            )
            raise LegacyAnalysisAuditAdapterError("文件分析 RAG 关闭审计追加失败") from exc
        logger.info(
            "文件分析 RAG 关闭审计已追加: task_id=%s audit_id=%s event_count=%d cleanup_status=%s",
            command.receipt.execution.task_id,
            interaction_id,
            len(shared_events),
            cleanup_status,
        )

    @classmethod
    def _recall_payload(cls, raw: Mapping[str, Any]) -> dict[str, Any]:
        cls._require_exact_keys(raw, cls._RECALL_REQUIRED_KEYS, "recall payload")
        return {
            "tree_fingerprint": raw["tree_fingerprint"],
            "query_digest": raw["query_digest"],
            "base_top64": raw["base_top64"],
            "final_candidates": raw["final_candidates"],
            "channel_rankings": raw["channel_rankings"],
            "rrf_scores": raw["rrf_scores"],
            "protected_reasons": raw["protected_reasons"],
            "prompt_chars": raw["prompt_chars"],
            "recall_elapsed_ms": raw["recall_elapsed_ms"],
        }

    @classmethod
    def _recall_finalize_payload(cls, raw: Mapping[str, Any]) -> dict[str, Any]:
        cls._require_exact_keys(raw, cls._RECALL_FINALIZE_REQUIRED_KEYS, "recall finalize payload")
        return {
            "returned_architecture_id": raw["returned_architecture_id"],
            "returned_rank": raw["returned_rank"],
            "total_elapsed_ms": raw["total_elapsed_ms"],
            "failure_stage": raw["failure_stage"],
            "error_message": raw["error_message"],
        }

    @staticmethod
    def _require_exact_keys(
        raw: Mapping[str, Any],
        expected: frozenset[str],
        label: str,
    ) -> None:
        actual = frozenset(raw)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            raise LegacyAnalysisAuditAdapterError(
                f"{label} 字段不匹配: missing={missing} unknown={unknown}"
            )

    @staticmethod
    def _recall_audit_id(task_id: str) -> str:
        return f"analysis-recall:{task_id}"

    @staticmethod
    def _require_recall_write_execution(
        written: object,
        source: AnalysisRecallAuditRecord | AnalysisRecallAuditReceipt,
    ) -> None:
        if getattr(written, "execution_id", None) != source.execution.task_id.value:
            raise LegacyAnalysisAuditAdapterError("文件分析召回审计返回 execution 不一致")

    @classmethod
    def _to_shared_trace(
        cls,
        record: AnalysisInteractionAuditRecord,
    ) -> RagExecutionTrace:
        attempts = tuple(cls._to_shared_attempt(item) for item in record.attempts)
        lifecycle_events = tuple(
            cls._to_shared_lifecycle_event(item)
            for item in record.lifecycle_events
        )
        failure_stage = record.error_code if record.outcome is AnalysisAuditOutcome.FAILED else None
        # 打开 Session 的第二步可能失败，此时只有 Context 引用而没有可构造的完整
        # ``AnalysisRagSessionRef``。生命周期事件仍是可信且必须落库的资源证据，因此失败
        # 审计允许从已成功的创建事件恢复部分引用；成功审计仍由 DTO 强制要求完整 Session。
        context_ref = (
            record.session.context_ref
            if record.session is not None
            else cls._latest_successful_ref(record.lifecycle_events, "context_create")
        )
        conversation_ref = (
            record.session.conversation_ref
            if record.session is not None
            else cls._latest_successful_ref(record.lifecycle_events, "conversation_create")
        )
        return RagExecutionTrace(
            context_name=record.context_name,
            context_ref=context_ref,
            conversation_ref=conversation_ref,
            attempts=attempts,
            failure_stage=failure_stage,
            error_message=(record.error_code if failure_stage else None),
            lifecycle_events=lifecycle_events,
            trace_id=record.trace_id,
        )

    @staticmethod
    def _latest_successful_ref(
        events: tuple[AnalysisRagLifecycleEvent, ...],
        operation: str,
    ) -> str | None:
        """从有序生命周期中提取指定资源最后一次成功创建的不透明引用。"""

        for event in reversed(events):
            if (
                event.operation == operation
                and event.outcome is AnalysisRagLifecycleOutcome.SUCCEEDED
                and event.external_ref
            ):
                return event.external_ref
        return None

    @classmethod
    def _to_shared_attempt(cls, attempt: Any) -> RagAttempt:
        failure_stage = attempt.error_code or None
        return RagAttempt(
            operation=attempt.operation.value,
            attempt=attempt.attempt_number,
            prompt_kind=cls._prompt_kind(attempt.operation),
            raw_response=attempt.raw_response,
            sources=tuple(cls._to_shared_source(item) for item in attempt.sources),
            failure_stage=failure_stage,
            error_message=("文件分析模型调用失败" if failure_stage else None),
            prompt_digest=attempt.prompt_digest,
            query_mode="query",
            source_count=len(attempt.sources),
            verified_source_count=len(attempt.sources),
            missing_marker_count=0,
            mismatched_marker_count=0,
            source_marker_status=("matched" if attempt.sources else "not_returned"),
        )

    @staticmethod
    def _to_shared_source(source: AnalysisRagSource) -> RagSource:
        return RagSource(
            document_ref=source.document_ref,
            text=source.text,
            id=source.source_id or None,
            title=source.title or None,
            url=source.url or None,
            score=source.score,
        )

    @staticmethod
    def _to_shared_lifecycle_event(event: AnalysisRagLifecycleEvent) -> RagLifecycleEvent:
        success = event.outcome is AnalysisRagLifecycleOutcome.SUCCEEDED
        failure_stage = None
        error_message = None
        if not success:
            failure_stage = event.error_code
            if event.outcome is AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN and not failure_stage.endswith("outcome_unknown"):
                failure_stage = f"{failure_stage}_outcome_unknown"
            error_message = "文件分析 RAG 外部操作结果未知" if event.outcome is AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN else "文件分析 RAG 外部操作失败"
        return RagLifecycleEvent(
            sequence_no=event.sequence_no,
            operation=event.operation,
            attempt=event.attempt_number,
            success=success,
            external_ref=event.external_ref or None,
            failure_stage=failure_stage,
            error_message=error_message,
        )

    @staticmethod
    def _prompt_kind(operation: AnalysisRagOperation) -> RagPromptKind:
        return {
            AnalysisRagOperation.COMBINED: RagPromptKind.ANALYSIS,
            AnalysisRagOperation.CLASSIFICATION: RagPromptKind.ARCHITECTURE_CLASSIFICATION,
            AnalysisRagOperation.CLASSIFICATION_REPAIR: RagPromptKind.ARCHITECTURE_REPAIR,
            AnalysisRagOperation.IDENTITY_RESELECT: RagPromptKind.ARCHITECTURE_RESELECT,
            AnalysisRagOperation.EXTRACTION: RagPromptKind.ANALYSIS_EXTRACTION,
            AnalysisRagOperation.EXTRACTION_REPAIR: RagPromptKind.JSON_REPAIR,
        }[operation]


__all__ = ("LegacyAnalysisAuditAdapter", "LegacyAnalysisAuditAdapterError")
