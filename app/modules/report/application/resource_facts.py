"""Report 执行事务内的纯资源事实变更。"""

from __future__ import annotations

from dataclasses import replace

from app.modules.report.domain import ReportCleanupError, ReportPortContractError
from app.modules.report.ports import (
    ReportArtifactRef,
    ReportArtifactScope,
    ReportAuditReceipt,
    ReportCleanupPartState,
    ReportRagCleanupRef,
    ReportResourceRecord,
    ReportResourceState,
    ReportResourceStorePort,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskId


class ReportResourceFactService:
    """只更新资源 Aggregate，不执行清理、网络、文件或审计操作。"""

    def __init__(self, store: ReportResourceStorePort) -> None:
        if not isinstance(store, ReportResourceStorePort):
            raise TypeError("store 必须实现 ReportResourceStorePort")
        self._store = store

    def register(
        self,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        scope: ReportArtifactScope,
    ) -> None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if not isinstance(scope, ReportArtifactScope) or scope.task_id != task_id:
            raise ValueError("scope 不属于当前 task_id")
        created = self._store.create(
            ReportResourceRecord(
                task_id=task_id,
                business_ref=business_ref,
                scope=scope,
            )
        )
        if (
            created.task_id != task_id
            or created.business_ref != business_ref
            or created.scope != scope
        ):
            raise ReportPortContractError("资源 Store 幂等登记返回了其他任务记录")

    def track_rag_cleanup(
        self,
        task_id: TaskId,
        cleanup_ref: ReportRagCleanupRef,
    ) -> None:
        if not isinstance(cleanup_ref, ReportRagCleanupRef):
            raise TypeError("cleanup_ref 必须是 ReportRagCleanupRef")
        record = self._required(task_id)
        if record.cleanup_ref is not None:
            if record.cleanup_ref != cleanup_ref:
                raise ReportCleanupError("同一任务出现不同 RAG cleanup ref")
            return
        self._save(
            replace(
                record,
                cleanup_ref=cleanup_ref,
                external_state=ReportCleanupPartState.PENDING,
            )
        )

    def track_audit(self, receipt: ReportAuditReceipt) -> None:
        if not isinstance(receipt, ReportAuditReceipt):
            raise TypeError("receipt 必须是 ReportAuditReceipt")
        record = self._required(receipt.task_id)
        if record.audit_receipt is not None:
            if record.audit_receipt != receipt:
                raise ReportCleanupError("同一任务出现不同 Audit Receipt")
            return
        self._save(replace(record, audit_receipt=receipt))

    def track_final_artifact(self, artifact: ReportArtifactRef) -> None:
        if not isinstance(artifact, ReportArtifactRef):
            raise TypeError("artifact 必须是 ReportArtifactRef")
        record = self._required(artifact.task_id)
        if record.final_artifact is not None:
            if record.final_artifact != artifact:
                raise ReportCleanupError("同一任务出现不同最终 Artifact")
            return
        self._save(replace(record, final_artifact=artifact))

    def quarantine(self, task_id: TaskId, *, stage: str, reason: str) -> None:
        """在任务事实事务内冻结不可自动清理的现场。"""

        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("stage 不能为空")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 不能为空")
        record = self._required(task_id)
        if record.state is ReportResourceState.QUARANTINED:
            return
        self._save(
            replace(
                record,
                state=ReportResourceState.QUARANTINED,
                last_error_stage=stage.strip()[:128],
                last_error_message=reason.strip()[:500],
            )
        )

    def _required(self, task_id: TaskId) -> ReportResourceRecord:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        record = self._store.get(task_id)
        if record is None:
            raise ReportCleanupError("报告资源恢复记录不存在")
        return record

    def _save(self, record: ReportResourceRecord) -> ReportResourceRecord:
        return self._store.save(record, expected_version=record.version)


__all__ = ["ReportResourceFactService"]
