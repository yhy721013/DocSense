"""文件分析资源事实、关闭收口与有界恢复。

本模块只处理已经持久化的 Analysis 资源事实。它从不重新执行模型、RAG 查询、永久知识
写入或文件下载；恢复阶段也不会猜测性重放远端删除。对外部结果未知、审计身份不完整或
所有权不明确的记录，一律进入 ``quarantined`` 并等待人工查回。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging
from typing import Any, Callable, Mapping

from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.analysis.ports import (
    AnalysisAuditPort,
    AnalysisExecutionRef,
    AnalysisInteractionAuditReceipt,
    AnalysisKnowledgeWriteOutcome,
    AnalysisKnowledgeWriteRequest,
    AnalysisKnowledgeWriteResult,
    AnalysisRagCloseOutcome,
    AnalysisRagCloseResult,
    AnalysisRagLifecycleEvent,
    AnalysisRagLifecycleOutcome,
    AnalysisRagUploadDescriptor,
    AnalysisResourceCommand,
    AnalysisResourcePort,
    AnalysisResourceRecord,
    AnalysisResourceScanBatch,
    AnalysisResourceState,
    AppendAnalysisLifecycleEvents,
    LoadAnalysisInteraction,
)

from .workflow_models import _RagWorkflowState


logger = logging.getLogger(__name__)

_RESOURCE_SCHEMA_VERSION_V1 = 1
_RESOURCE_SCHEMA_VERSION_V2 = 2
_RESOURCE_SCHEMA_VERSION = 3
_FINAL_CLEANUP_STATES = frozenset({"confirmed", "known_not_applied", "not_required"})
_RECOVERABLE_CLOSE_STATES = frozenset({"confirmed", "known_not_applied"})


def _utc_now() -> datetime:
    """返回带时区的 UTC 时钟，避免恢复退避混入本地无时区时间。"""

    return datetime.now(timezone.utc)


def _utc_iso(clock: Callable[[], datetime]) -> str:
    """把注入时钟规范化为 SQLite 可比较的 UTC ISO 时间。"""

    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("resource recovery clock 必须返回 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("resource recovery clock 必须返回带时区 datetime")
    return value.astimezone(timezone.utc).isoformat()


def _safe_reason(value: object, *, maximum: int = 160) -> str:
    """只保留稳定的错误类别，禁止把路径、Prompt 或供应商响应写入恢复日志。"""

    if isinstance(value, BaseException):
        text = type(value).__name__
    else:
        text = str(value or "").strip()
    normalized = "_".join(text.split())
    return normalized[:maximum] or "analysis_resource_unknown"


def _event_payload(event: AnalysisRagLifecycleEvent) -> dict[str, object]:
    """把强类型生命周期事件投影为不含模型正文的可恢复 JSON。"""

    return {
        "sequence_no": event.sequence_no,
        "operation": event.operation,
        "attempt_number": event.attempt_number,
        "outcome": event.outcome.value,
        "external_ref": event.external_ref,
        "error_code": event.error_code,
    }


def _event_from_payload(value: object) -> AnalysisRagLifecycleEvent:
    """严格恢复已保存的 close 生命周期事件，坏记录不能驱动自动处理。"""

    if not isinstance(value, Mapping):
        raise ValueError("close lifecycle event 必须是对象")
    try:
        return AnalysisRagLifecycleEvent(
            sequence_no=value.get("sequence_no"),  # type: ignore[arg-type]
            operation=value.get("operation"),  # type: ignore[arg-type]
            attempt_number=value.get("attempt_number"),  # type: ignore[arg-type]
            outcome=AnalysisRagLifecycleOutcome(value.get("outcome")),
            external_ref=value.get("external_ref", ""),  # type: ignore[arg-type]
            error_code=value.get("error_code", ""),  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("close lifecycle event 字段无效") from exc


class AnalysisResourceLifecycleError(RuntimeError):
    """资源事实未能可靠持久化时中止当前外部链路，避免产生无现场资源。"""


class AnalysisResourceRecoveryOutcome(str, Enum):
    """一份资源记录在本次有界恢复中的内部结论。"""

    CLEANED = "cleaned"
    DEFERRED = "deferred"
    NOT_FOUND = "not_found"
    PENDING = "pending"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class AnalysisResourceRecoveryResult:
    """单条恢复结果，仅用于内部维护日志和未来 Dispatcher 指标。"""

    execution: AnalysisExecutionRef
    outcome: AnalysisResourceRecoveryOutcome
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.outcome, AnalysisResourceRecoveryOutcome):
            raise TypeError("outcome 必须是 AnalysisResourceRecoveryOutcome")
        if not isinstance(self.reason, str):
            raise TypeError("reason 必须是 str")
        object.__setattr__(self, "reason", self.reason.strip()[:160])


@dataclass(frozen=True)
class AnalysisResourceSweepResult:
    """一次限制数量的资源扫描汇总，不把失败记录伪装成已清理。"""

    requested_limit: int
    scanned_count: int
    cleaned_count: int
    deferred_count: int
    quarantined_count: int
    pending_count: int

    def __post_init__(self) -> None:
        for name in (
            "requested_limit",
            "scanned_count",
            "cleaned_count",
            "deferred_count",
            "quarantined_count",
            "pending_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if self.requested_limit < 1:
            raise ValueError("requested_limit 必须是正整数")
        if self.scanned_count > self.requested_limit:
            raise ValueError("scanned_count 不能超过 requested_limit")
        if (
            self.cleaned_count
            + self.deferred_count
            + self.quarantined_count
            + self.pending_count
            != self.scanned_count
        ):
            raise ValueError("资源扫描分类计数必须与 scanned_count 一致")


class AnalysisResourceLifecycle:
    """单个 execution 的资源事实协作器。

    一个实例只能属于一个 ``AnalysisExecutionRef``，因此不会把另一个任务的 Context、
    Document 或审计凭据写进当前记录。所有状态和载荷推进均通过 Resource Port 的
    ``state + version`` CAS；CAS 冲突不重读覆盖，调用方应停止当前副作用并让恢复流程
    处理最新事实。
    """

    def __init__(
        self,
        *,
        store: AnalysisResourcePort,
        execution: AnalysisExecutionRef,
    ) -> None:
        if not isinstance(store, AnalysisResourcePort):
            raise TypeError("store 必须实现 AnalysisResourcePort")
        if not isinstance(execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        self._store = store
        self._execution = execution
        self._record: AnalysisResourceRecord | None = None

    @property
    def execution(self) -> AnalysisExecutionRef:
        return self._execution

    @property
    def record(self) -> AnalysisResourceRecord | None:
        """返回当前已知不可变记录，不暴露可变内部 payload。"""

        return self._record

    def register(
        self,
        *,
        task_root: str,
        source_path: str,
        upload_path: str,
        state: _RagWorkflowState,
        upload_descriptor: AnalysisRagUploadDescriptor | None = None,
        processing_path: str | None = None,
    ) -> None:
        """在创建任何远端 RAG 资源前登记 ``tracking`` 记录。"""

        if self._record is not None:
            raise AnalysisResourceLifecycleError("同一 execution 不得重复登记资源记录")
        resolved_processing_path = processing_path or upload_path
        for name, value in (
            ("task_root", task_root),
            ("source_path", source_path),
            ("processing_path", resolved_processing_path),
            ("upload_path", upload_path),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空 str")
        if not isinstance(state, _RagWorkflowState):
            raise TypeError("state 必须是 _RagWorkflowState")
        if upload_descriptor is not None:
            if not isinstance(upload_descriptor, AnalysisRagUploadDescriptor):
                raise TypeError(
                    "upload_descriptor 必须是 AnalysisRagUploadDescriptor 或 None"
                )
            if upload_descriptor.artifact.task_id != self._execution.task_id:
                raise AnalysisResourceLifecycleError(
                    "RAG 上传描述符不属于当前 execution"
                )

        record = self._store.create(
            AnalysisResourceCommand(
                execution=self._execution,
                expected_state=None,
                expected_version=None,
                target_state=AnalysisResourceState.TRACKING,
                record_payload=FrozenJsonObject.from_mapping(
                    self._initial_payload(
                        task_root=task_root,
                        source_path=source_path,
                        processing_path=resolved_processing_path,
                        upload_path=upload_path,
                        upload_descriptor=upload_descriptor,
                        state=state,
                    ),
                    name="analysis_resource_initial",
                ),
            )
        )
        self._require_record(record, operation="register")
        if record.state is not AnalysisResourceState.TRACKING:
            raise AnalysisResourceLifecycleError(
                "已有资源记录不是 tracking，禁止当前 Worker 继续创建外部资源"
            )
        self._record = record

    def prepare_document_upload(self) -> None:
        """在首次文档上传请求前以 CAS 持久化副作用意图。

        CAS 失败时调用方必须停止，绝不能继续发起 HTTP。CAS 成功后若进程崩溃，恢复端
        只能看到 ``started_unknown``，因此会保留现场而不会猜测文档未上传或自动重放。
        """

        record = self._require_tracking_record("prepare_document_upload")
        payload = self._payload(record)
        upload = self._mapping(payload.get("upload"), name="upload")
        state = upload.get("delivery_state")
        if state == "confirmed":
            return
        if state != "not_started":
            raise AnalysisResourceLifecycleError(
                "RAG 文档上传状态不是 not_started，禁止重复发起外部上传"
            )
        upload["delivery_state"] = "started_unknown"
        self._advance(payload, AnalysisResourceState.TRACKING)

    def checkpoint_rag_state(self, state: _RagWorkflowState) -> None:
        """在每次取得 RAG Session 或生命周期事件后立即保存引用。"""

        if not isinstance(state, _RagWorkflowState):
            raise TypeError("state 必须是 _RagWorkflowState")
        record = self._require_tracking_record("checkpoint_rag_state")
        payload = self._payload(record)
        payload["rag"] = self._rag_payload(state)
        payload["audit"] = self._audit_payload(state, previous=payload.get("audit"))
        upload = payload.get("upload")
        if isinstance(upload, Mapping) and state.session is not None:
            upload = dict(upload)
            payload["upload"] = upload
            if state.session.document_bound:
                artifact = self._mapping(
                    upload.get("artifact"),
                    name="upload.artifact",
                )
                expected_sha256 = str(artifact.get("sha256") or "").strip().lower()
                expected_file_name = str(
                    upload.get("transport_file_name") or ""
                )
                if (
                    state.session.content_sha256 != expected_sha256
                    or state.session.ingested_file_name != expected_file_name
                ):
                    state.preserve_scene = True
                    self._set_diagnosis(
                        payload,
                        stage="document_upload",
                        reason="uploaded_document_identity_mismatch",
                    )
                    self._advance(payload, AnalysisResourceState.QUARANTINED)
                    raise AnalysisResourceLifecycleError(
                        "RAG 已绑定文档身份与登记上传描述符不一致"
                    )
                upload["delivery_state"] = "confirmed"

        has_unknown = any(
            event.outcome is AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN
            for event in state.lifecycle_events
        )
        if has_unknown:
            state.preserve_scene = True
            self._set_diagnosis(
                payload,
                stage="rag_lifecycle",
                reason="rag_lifecycle_outcome_unknown",
            )
            self._advance(payload, AnalysisResourceState.QUARANTINED)
            raise AnalysisResourceLifecycleError(
                "RAG 生命周期结果未知，已隔离资源并禁止继续外部调用"
            )
        self._advance(payload, AnalysisResourceState.TRACKING)

    def record_recall_state(
        self,
        state: _RagWorkflowState,
        *,
        failed: bool = False,
    ) -> None:
        """记录召回审计的预留/终结状态，不保存 Prompt 或模型输出。"""

        if not isinstance(state, _RagWorkflowState):
            raise TypeError("state 必须是 _RagWorkflowState")
        record = self._require_tracking_record("record_recall_state")
        payload = self._payload(record)
        payload["audit"] = self._audit_payload(state, previous=payload.get("audit"))
        audit = self._mapping(payload["audit"], name="audit")
        recall = self._mapping(audit.get("recall"), name="audit.recall")
        recall["status"] = "failed" if failed else (
            "finalized" if state.recall_finalized else "reserved"
        )
        self._advance(payload, AnalysisResourceState.TRACKING)

    def record_interaction_receipt(
        self,
        receipt: AnalysisInteractionAuditReceipt,
    ) -> None:
        """交互审计提交成功后立即保存其幂等身份，作为 close 事件追加的前置。"""

        if not isinstance(receipt, AnalysisInteractionAuditReceipt):
            raise TypeError("receipt 必须是 AnalysisInteractionAuditReceipt")
        if receipt.execution != self._execution:
            raise AnalysisResourceLifecycleError("交互审计凭据不属于当前 execution")
        record = self._require_tracking_record("record_interaction_receipt")
        payload = self._payload(record)
        audit = self._mapping(payload["audit"], name="audit")
        audit["interaction"] = {
            "idempotency_key": receipt.idempotency_key,
            "audit_id": receipt.audit_id,
            "status": "persisted",
        }
        self._advance(payload, AnalysisResourceState.TRACKING)

    def mark_audit_pending(self, error: BaseException) -> None:
        """交互审计调用结果不确定时保留现场，禁止关闭或删除 RAG 资源。"""

        record = self._require_record()
        if record.state is AnalysisResourceState.QUARANTINED:
            return
        if record.state is AnalysisResourceState.CLEANED:
            raise AnalysisResourceLifecycleError("已清理资源不得回退为审计待定")
        payload = self._payload(record)
        audit = self._mapping(payload["audit"], name="audit")
        interaction = self._mapping(audit.get("interaction"), name="audit.interaction")
        interaction["status"] = "outcome_unknown"
        self._set_diagnosis(payload, stage="audit", reason=_safe_reason(error))
        self._advance(payload, AnalysisResourceState.AUDIT_PENDING)

    def record_knowledge_result(
        self,
        request: AnalysisKnowledgeWriteRequest,
        result: AnalysisKnowledgeWriteResult,
    ) -> None:
        """保存永久知识库三态结果；未知结果立即冻结文档所有权。"""

        if not isinstance(request, AnalysisKnowledgeWriteRequest):
            raise TypeError("request 必须是 AnalysisKnowledgeWriteRequest")
        if not isinstance(result, AnalysisKnowledgeWriteResult):
            raise TypeError("result 必须是 AnalysisKnowledgeWriteResult")
        if (
            request.execution != self._execution
            or result.execution != self._execution
            or result.idempotency_key != request.idempotency_key
        ):
            raise AnalysisResourceLifecycleError("永久知识库结果与当前 execution 不一致")
        record = self._require_tracking_record("record_knowledge_result")
        payload = self._payload(record)
        payload["knowledge"] = {
            "idempotency_key": request.idempotency_key,
            "architecture_id": request.architecture_id,
            "external_ref": result.external_ref,
            "outcome": result.outcome.value,
            "detail_code": result.detail_code,
        }
        ownership = self._mapping(payload["ownership"], name="ownership")
        if result.outcome is AnalysisKnowledgeWriteOutcome.COMMITTED:
            ownership["document"] = "permanent"
            self._advance(payload, AnalysisResourceState.TRACKING)
            return
        if result.outcome is AnalysisKnowledgeWriteOutcome.NOT_APPLIED:
            ownership["document"] = "temporary"
            self._advance(payload, AnalysisResourceState.TRACKING)
            return

        ownership["document"] = "unknown"
        self._set_diagnosis(
            payload,
            stage="knowledge_index",
            reason=result.detail_code or "knowledge_outcome_unknown",
        )
        self._advance(payload, AnalysisResourceState.QUARANTINED)

    def prepare_close(self, *, retain_document: bool) -> None:
        """在任何远端 close/delete 前持久化清理意图。"""

        if not isinstance(retain_document, bool):
            raise TypeError("retain_document 必须是 bool")
        record = self._require_tracking_record("prepare_close")
        payload = self._payload(record)
        ownership = self._mapping(payload["ownership"], name="ownership")
        document_ownership = ownership.get("document")
        if retain_document and document_ownership != "permanent":
            # retain=true 却没有永久接管事实时，不能臆测 RAG close 是否会保留文档。
            ownership["document"] = "unknown"
            self._set_diagnosis(
                payload,
                stage="resource_ownership",
                reason="retain_document_without_permanent_ownership",
            )
            self._advance(payload, AnalysisResourceState.QUARANTINED)
            raise AnalysisResourceLifecycleError("文档所有权未知，禁止执行 RAG close")
        if not retain_document and document_ownership not in {"temporary", "released"}:
            ownership["document"] = "unknown"
            self._set_diagnosis(
                payload,
                stage="resource_ownership",
                reason="cleanup_without_temporary_ownership",
            )
            self._advance(payload, AnalysisResourceState.QUARANTINED)
            raise AnalysisResourceLifecycleError("临时文档所有权不明确，禁止自动清理")

        payload["cleanup"] = {
            "session_close": {"state": "planned", "retain_document": retain_document},
            "document": {
                "state": "not_required" if retain_document else "planned",
                "retain_document": retain_document,
            },
            "audit_append": {"state": "planned"},
            "close_events": [],
        }
        self._advance(payload, AnalysisResourceState.CLEANUP_PENDING)

    def mark_close_running(self) -> None:
        """在实际调用 RAG close 前记录已开始的外部清理动作。"""

        record = self._require_record()
        if record.state is not AnalysisResourceState.CLEANUP_PENDING:
            raise AnalysisResourceLifecycleError("只有 cleanup_pending 记录可以开始 RAG close")
        payload = self._payload(record)
        cleanup = self._mapping(payload["cleanup"], name="cleanup")
        session_close = self._mapping(cleanup.get("session_close"), name="cleanup.session_close")
        if session_close.get("state") != "planned":
            raise AnalysisResourceLifecycleError("RAG close 未处于 planned，禁止重复外部调用")
        session_close["state"] = "running"
        document = self._mapping(cleanup.get("document"), name="cleanup.document")
        if document.get("state") == "planned":
            document["state"] = "running"
        self._advance(payload, AnalysisResourceState.CLEANUP_PENDING)

    def record_close_result(self, result: AnalysisRagCloseResult) -> None:
        """RAG close 返回后先记录可判定结果，再允许追加审计事件。"""

        if not isinstance(result, AnalysisRagCloseResult):
            raise TypeError("result 必须是 AnalysisRagCloseResult")
        if result.execution != self._execution:
            raise AnalysisResourceLifecycleError("RAG close 结果不属于当前 execution")
        record = self._require_record()
        if record.state is AnalysisResourceState.QUARANTINED:
            return
        if record.state is not AnalysisResourceState.CLEANUP_PENDING:
            raise AnalysisResourceLifecycleError("RAG close 结果缺少 cleanup_pending 意图")
        payload = self._payload(record)
        if not self._apply_close_result(payload, result):
            self._set_diagnosis(
                payload,
                stage="rag_close",
                reason="rag_close_outcome_unknown",
            )
            self._advance(payload, AnalysisResourceState.QUARANTINED)
            return
        self._advance(payload, AnalysisResourceState.CLEANUP_PENDING)

    def _apply_close_result(
        self,
        payload: dict[str, Any],
        result: AnalysisRagCloseResult,
    ) -> bool:
        """把已返回的远端 close 结果完整写入待持久化 payload。

        返回 ``False`` 表示外部结果未知，调用方必须隔离；返回 ``True`` 表示 close
        outcome 与生命周期事件均已确定，后续只允许补幂等审计。
        """

        cleanup = self._mapping(payload["cleanup"], name="cleanup")
        session_close = self._mapping(
            cleanup.get("session_close"),
            name="cleanup.session_close",
        )
        document = self._mapping(cleanup.get("document"), name="cleanup.document")
        cleanup["close_events"] = [
            _event_payload(event) for event in result.lifecycle_events
        ]
        if result.outcome is AnalysisRagCloseOutcome.OUTCOME_UNKNOWN:
            session_close["state"] = "outcome_unknown"
            document["state"] = "outcome_unknown"
            self._mapping(payload["ownership"], name="ownership")["document"] = "unknown"
            return False

        outcome = result.outcome.value
        session_close["state"] = outcome
        if document.get("state") != "not_required":
            document["state"] = outcome
            if outcome == "confirmed":
                self._mapping(payload["ownership"], name="ownership")["document"] = (
                    "released"
                )
        return True

    def mark_close_audited(self) -> None:
        """close 生命周期审计追加成功后，仅在全部清理事实确定时进入 ``cleaned``。"""

        record = self._require_record()
        if record.state is AnalysisResourceState.QUARANTINED:
            return
        if record.state not in {
            AnalysisResourceState.CLEANUP_PENDING,
            AnalysisResourceState.AUDIT_PENDING,
        }:
            raise AnalysisResourceLifecycleError("close 审计追加缺少待清理资源记录")
        payload = self._payload(record)
        cleanup = self._mapping(payload["cleanup"], name="cleanup")
        audit_append = self._mapping(cleanup.get("audit_append"), name="cleanup.audit_append")
        audit_append["state"] = "confirmed"
        states = (
            self._mapping(cleanup.get("session_close"), name="cleanup.session_close").get("state"),
            self._mapping(cleanup.get("document"), name="cleanup.document").get("state"),
            audit_append.get("state"),
        )
        if not all(state in _FINAL_CLEANUP_STATES for state in states):
            raise AnalysisResourceLifecycleError("清理事实尚未全部确认，禁止标记 cleaned")
        self._clear_diagnosis(payload)
        self._advance(payload, AnalysisResourceState.CLEANED)

    def record_close_failure(
        self,
        error: BaseException,
        result: AnalysisRagCloseResult | None,
    ) -> None:
        """保存 close 或 close 审计失败；未知外部结果不可自动重放。"""

        record = self._require_record()
        if record.state is AnalysisResourceState.QUARANTINED:
            return
        payload = self._payload(record)
        cleanup = self._mapping(payload.get("cleanup"), name="cleanup")
        if result is None:
            # RAG close 抛错可能发生在请求已抵达远端之后，不能把它降级为普通可重试失败。
            session_close = self._mapping(cleanup.get("session_close"), name="cleanup.session_close")
            document = self._mapping(cleanup.get("document"), name="cleanup.document")
            session_close["state"] = "outcome_unknown"
            document["state"] = "outcome_unknown"
            self._mapping(payload["ownership"], name="ownership")["document"] = "unknown"
            self._set_diagnosis(payload, stage="rag_close", reason=_safe_reason(error))
            self._advance(payload, AnalysisResourceState.QUARANTINED)
            return

        # 已拿到 close 结果而结果落库或审计追加失败时，先把 outcome 与 close events
        # 完整重建到本次 CAS payload，再转入后续幂等审计。这样即使首次
        # ``on_close_result`` 写入失败，也不会只留下模糊诊断而丢失已知外部事实。
        if not self._apply_close_result(payload, result):
            self._set_diagnosis(
                payload,
                stage="rag_close",
                reason="rag_close_outcome_unknown",
            )
            self._advance(payload, AnalysisResourceState.QUARANTINED)
            return
        audit_append = self._mapping(cleanup.get("audit_append"), name="cleanup.audit_append")
        audit_append["state"] = "outcome_unknown"
        self._set_diagnosis(payload, stage="audit_append", reason=_safe_reason(error))
        self._advance(payload, AnalysisResourceState.AUDIT_PENDING)

    def quarantine(self, *, stage: str, reason: str) -> None:
        """显式隔离当前资源，避免调用方继续使用不可靠的外部事实。"""

        record = self._require_record()
        if record.state is AnalysisResourceState.QUARANTINED:
            return
        if record.state is AnalysisResourceState.CLEANED:
            raise AnalysisResourceLifecycleError("已清理资源不能再隔离")
        payload = self._payload(record)
        self._set_diagnosis(payload, stage=stage, reason=reason)
        self._advance(payload, AnalysisResourceState.QUARANTINED)

    def _initial_payload(
        self,
        *,
        task_root: str,
        source_path: str,
        processing_path: str,
        upload_path: str,
        upload_descriptor: AnalysisRagUploadDescriptor | None,
        state: _RagWorkflowState,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": (
                _RESOURCE_SCHEMA_VERSION
                if upload_descriptor is not None
                else _RESOURCE_SCHEMA_VERSION_V2
            ),
            "local": {
                "task_root": task_root,
                "source_path": source_path,
                "processing_path": processing_path,
                "upload_path": upload_path,
            },
            "rag": self._rag_payload(state),
            "audit": self._audit_payload(state, previous=None),
            "knowledge": {
                "idempotency_key": "",
                "architecture_id": None,
                "external_ref": "",
                "outcome": "not_started",
                "detail_code": "",
            },
            "ownership": {"document": "temporary"},
            "cleanup": {
                "session_close": {"state": "not_planned", "retain_document": False},
                "document": {"state": "not_planned", "retain_document": False},
                "audit_append": {"state": "not_planned"},
                "close_events": [],
            },
            "diagnosis": {"stage": "", "reason": ""},
        }
        if upload_descriptor is not None:
            payload["upload"] = self._upload_payload(upload_descriptor)
        return payload

    @staticmethod
    def _upload_payload(
        descriptor: AnalysisRagUploadDescriptor,
    ) -> dict[str, object]:
        """保存可恢复的不可变上传身份，不保存宿主路径或 Provider 字段。"""

        artifact = descriptor.artifact
        return {
            "delivery_state": "not_started",
            "representation": descriptor.representation.value,
            "media_type": descriptor.media_type,
            "transport_file_name": descriptor.transport_file_name,
            "display_title": descriptor.display_title,
            "projection_profile_id": descriptor.projection_profile_id,
            "naming_policy": descriptor.naming_policy,
            "artifact": {
                "artifact_id": artifact.artifact_id,
                "step_key": artifact.step_key,
                "kind": artifact.kind.value,
                "representation": artifact.representation.value,
                "media_type": artifact.metadata.media_type,
                "size_bytes": artifact.metadata.size_bytes,
                "sha256": artifact.metadata.sha256,
                "ordinal": artifact.ordinal,
            },
        }

    @staticmethod
    def _rag_payload(state: _RagWorkflowState) -> dict[str, object]:
        session = state.session
        return {
            "session_ref": session.session_ref if session is not None else "",
            "context_ref": session.context_ref if session is not None else "",
            "conversation_ref": session.conversation_ref if session is not None else "",
            "document_ref": session.document_ref if session is not None else "",
            "document_location": session.document_location if session is not None else "",
            "content_sha256": session.content_sha256 if session is not None else "",
            "ingested_file_name": session.ingested_file_name if session is not None else "",
            "lifecycle_events": [_event_payload(event) for event in state.lifecycle_events],
        }

    @staticmethod
    def _audit_payload(
        state: _RagWorkflowState,
        *,
        previous: object,
    ) -> dict[str, object]:
        previous_mapping = previous if isinstance(previous, Mapping) else {}
        previous_recall = previous_mapping.get("recall")
        previous_interaction = previous_mapping.get("interaction")
        recall = state.recall_receipt
        interaction = state.interaction_receipt
        return {
            "recall": {
                "idempotency_key": (
                    recall.idempotency_key
                    if recall is not None
                    else AnalysisResourceLifecycle._text_from_mapping(
                        previous_recall,
                        "idempotency_key",
                    )
                ),
                "audit_id": (
                    recall.audit_id
                    if recall is not None
                    else AnalysisResourceLifecycle._text_from_mapping(previous_recall, "audit_id")
                ),
                "status": "finalized" if state.recall_finalized else "reserved",
            },
            "interaction": {
                "idempotency_key": (
                    interaction.idempotency_key
                    if interaction is not None
                    else AnalysisResourceLifecycle._text_from_mapping(
                        previous_interaction,
                        "idempotency_key",
                    )
                ),
                "audit_id": (
                    interaction.audit_id
                    if interaction is not None
                    else AnalysisResourceLifecycle._text_from_mapping(
                        previous_interaction,
                        "audit_id",
                    )
                ),
                "status": "persisted" if interaction is not None else "not_started",
            },
        }

    @staticmethod
    def _text_from_mapping(value: object, key: str) -> str:
        if isinstance(value, Mapping) and isinstance(value.get(key), str):
            return value[key].strip()
        return ""

    def _require_tracking_record(self, operation: str) -> AnalysisResourceRecord:
        record = self._require_record()
        if record.state is not AnalysisResourceState.TRACKING:
            raise AnalysisResourceLifecycleError(
                f"资源记录当前为 {record.state.value}，不能执行 {operation}"
            )
        return record

    def _payload(self, record: AnalysisResourceRecord) -> dict[str, Any]:
        payload = record.record_payload.to_dict()
        if payload.get("schema_version") not in {
            _RESOURCE_SCHEMA_VERSION_V1,
            _RESOURCE_SCHEMA_VERSION_V2,
            _RESOURCE_SCHEMA_VERSION,
        }:
            raise AnalysisResourceLifecycleError("资源记录 schema_version 不受当前用例支持")
        for key in ("local", "rag", "audit", "knowledge", "ownership", "cleanup", "diagnosis"):
            if not isinstance(payload.get(key), Mapping):
                raise AnalysisResourceLifecycleError(f"资源记录缺少有效 {key} 对象")
            payload[key] = dict(payload[key])
        if payload.get("schema_version") == _RESOURCE_SCHEMA_VERSION:
            if not isinstance(payload.get("upload"), Mapping):
                raise AnalysisResourceLifecycleError("资源记录缺少有效 upload 对象")
            payload["upload"] = dict(payload["upload"])
        return payload

    @staticmethod
    def _mapping(value: object, *, name: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise AnalysisResourceLifecycleError(f"资源记录 {name} 必须是对象")
        # ``record_payload.to_dict`` 已完成深复制；对其中原生 dict 直接原地更新，才能让
        # 下一次 CAS 带上嵌套的审计/清理状态。仅当调用方传入其他 Mapping 时再复制。
        if isinstance(value, dict):
            return value
        return dict(value)

    @staticmethod
    def _set_diagnosis(payload: dict[str, Any], *, stage: str, reason: str) -> None:
        payload["diagnosis"] = {
            "stage": _safe_reason(stage, maximum=80),
            "reason": _safe_reason(reason),
        }

    @staticmethod
    def _clear_diagnosis(payload: dict[str, Any]) -> None:
        payload["diagnosis"] = {"stage": "", "reason": ""}

    def _advance(
        self,
        payload: Mapping[str, object],
        target_state: AnalysisResourceState,
    ) -> AnalysisResourceRecord:
        record = self._require_record()
        try:
            advanced = self._store.advance(
                AnalysisResourceCommand(
                    execution=self._execution,
                    expected_state=record.state,
                    expected_version=record.version,
                    target_state=target_state,
                    record_payload=FrozenJsonObject.from_mapping(
                        dict(payload),
                        name="analysis_resource_payload",
                    ),
                )
            )
        except Exception as exc:
            raise AnalysisResourceLifecycleError(
                "资源记录 CAS 推进失败，禁止继续外部副作用"
            ) from exc
        self._require_record(advanced, operation="advance")
        self._record = advanced
        return advanced

    def _require_record(
        self,
        record: AnalysisResourceRecord | None = None,
        *,
        operation: str = "read",
    ) -> AnalysisResourceRecord:
        # 同名私有方法同时服务“当前记录存在性”和 Adapter 返回身份校验，保持调用点简洁。
        if record is None:
            if self._record is None:
                raise AnalysisResourceLifecycleError("资源记录尚未登记")
            return self._record
        if not isinstance(record, AnalysisResourceRecord) or record.execution != self._execution:
            raise AnalysisResourceLifecycleError(f"资源 {operation} 返回了其他 execution")
        return record


class RecoverAnalysisResources:
    """只执行可证明幂等的审计补齐，绝不自动重放远端 RAG 清理。"""

    def __init__(
        self,
        *,
        store: AnalysisResourcePort,
        audit: AnalysisAuditPort,
        clock: Callable[[], datetime] = _utc_now,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 300.0,
        max_deferrals: int = 3,
    ) -> None:
        if not isinstance(store, AnalysisResourcePort):
            raise TypeError("store 必须实现 AnalysisResourcePort")
        if not isinstance(audit, AnalysisAuditPort):
            raise TypeError("audit 必须实现 AnalysisAuditPort")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        for name, value in (
            ("retry_base_seconds", retry_base_seconds),
            ("retry_max_seconds", retry_max_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} 必须是正数")
        if float(retry_max_seconds) < float(retry_base_seconds):
            raise ValueError("retry_max_seconds 不能小于 retry_base_seconds")
        if isinstance(max_deferrals, bool) or not isinstance(max_deferrals, int) or max_deferrals < 1:
            raise ValueError("max_deferrals 必须是正整数")
        self._store = store
        self._audit = audit
        self._clock = clock
        self._retry_base_seconds = float(retry_base_seconds)
        self._retry_max_seconds = float(retry_max_seconds)
        self._max_deferrals = max_deferrals

    def run_once(self, *, limit: int) -> AnalysisResourceSweepResult:
        """扫描不超过 ``limit`` 条到期记录；单轮不执行任何外部删除。"""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit 必须是1~1000的整数")
        batch = self._store.list_recoverable(limit=limit)
        if not isinstance(batch, AnalysisResourceScanBatch):
            raise RuntimeError(
                "Analysis资源扫描必须返回AnalysisResourceScanBatch"
            )
        records = batch.records
        counts = {
            AnalysisResourceRecoveryOutcome.CLEANED: 0,
            AnalysisResourceRecoveryOutcome.DEFERRED: 0,
            AnalysisResourceRecoveryOutcome.QUARANTINED: batch.quarantined_count,
            AnalysisResourceRecoveryOutcome.PENDING: batch.pending_count,
        }
        for record in records:
            try:
                result = self.recover(record)
            except Exception:
                # ``recover`` 已经对预期故障做了局部收敛；这里再保留最后一道逐记录
                # 防线，确保未来 Dispatcher 维护线程不会因单条异常终止整轮扫描。
                logger.exception(
                    "文件分析资源单条恢复出现未收敛异常，跳过并继续扫描: task_id=%s",
                    record.execution.task_id,
                )
                result = AnalysisResourceRecoveryResult(
                    record.execution,
                    AnalysisResourceRecoveryOutcome.PENDING,
                    "unexpected_recovery_error",
                )
            if result.outcome in counts:
                counts[result.outcome] += 1
        return AnalysisResourceSweepResult(
            requested_limit=limit,
            scanned_count=(
                len(records)
                + batch.quarantined_count
                + batch.pending_count
            ),
            cleaned_count=counts[AnalysisResourceRecoveryOutcome.CLEANED],
            deferred_count=counts[AnalysisResourceRecoveryOutcome.DEFERRED],
            quarantined_count=counts[AnalysisResourceRecoveryOutcome.QUARANTINED],
            pending_count=counts[AnalysisResourceRecoveryOutcome.PENDING],
        )

    def recover(self, record: AnalysisResourceRecord) -> AnalysisResourceRecoveryResult:
        """收敛一条已读记录；CAS 冲突视为其他执行者接管，不发送任何请求。"""

        if not isinstance(record, AnalysisResourceRecord):
            raise TypeError("record 必须是 AnalysisResourceRecord")
        if record.state is AnalysisResourceState.CLEANED:
            return AnalysisResourceRecoveryResult(
                record.execution,
                AnalysisResourceRecoveryOutcome.CLEANED,
            )
        if record.state is AnalysisResourceState.QUARANTINED:
            return AnalysisResourceRecoveryResult(
                record.execution,
                AnalysisResourceRecoveryOutcome.QUARANTINED,
                "already_quarantined",
            )

        try:
            payload = self._payload(record)
            if record.state is AnalysisResourceState.TRACKING:
                return self._quarantine(
                    record,
                    payload,
                    stage="recovery",
                    reason="terminal_tracking_without_close_fact",
                )
            if record.state is AnalysisResourceState.AUDIT_PENDING:
                return self._recover_audit_pending(record, payload)
            return self._recover_cleanup_pending(record, payload)
        except Exception as exc:
            logger.exception(
                "文件分析资源恢复出现局部错误，保留现场并使用有界退避: task_id=%s state=%s",
                record.execution.task_id,
                record.state.value,
            )
            return self._defer_or_quarantine(record, _safe_reason(exc))

    def _recover_cleanup_pending(
        self,
        record: AnalysisResourceRecord,
        payload: dict[str, Any],
    ) -> AnalysisResourceRecoveryResult:
        cleanup = self._mapping(payload.get("cleanup"), "cleanup")
        close_state = self._mapping(cleanup.get("session_close"), "cleanup.session_close").get("state")
        if close_state not in _RECOVERABLE_CLOSE_STATES:
            return self._quarantine(
                record,
                payload,
                stage="recovery",
                reason=f"close_state_{_safe_reason(close_state)}",
            )
        return self._append_close_events(record, payload)

    def _recover_audit_pending(
        self,
        record: AnalysisResourceRecord,
        payload: dict[str, Any],
    ) -> AnalysisResourceRecoveryResult:
        cleanup = self._mapping(payload.get("cleanup"), "cleanup")
        close_state = self._mapping(cleanup.get("session_close"), "cleanup.session_close").get("state")
        if close_state in _RECOVERABLE_CLOSE_STATES:
            # 远端 close 已是确定事实，只补同一幂等审计写入，不重放 close。
            return self._append_close_events(record, payload)

        # 交互审计调用可能在本地抛错前已经提交；只做幂等查回来保存诊断事实，绝不因
        # 查到 Receipt 就自动开始此前未计划的 RAG close。
        self._lookup_interaction_receipt(record, payload)
        return self._quarantine(
            record,
            payload,
            stage="audit",
            reason="interaction_audit_not_reliably_closed",
        )

    def _lookup_interaction_receipt(
        self,
        record: AnalysisResourceRecord,
        payload: dict[str, Any],
    ) -> None:
        audit = self._mapping(payload.get("audit"), "audit")
        interaction = self._mapping(audit.get("interaction"), "audit.interaction")
        key = interaction.get("idempotency_key")
        if not isinstance(key, str) or not key.strip():
            return
        receipt = self._audit.load_interaction(
            LoadAnalysisInteraction(record.execution, key.strip())
        )
        if receipt is None:
            return
        if receipt.execution != record.execution or receipt.idempotency_key != key.strip():
            raise RuntimeError("交互审计查回了其他 execution")
        interaction.update(
            {
                "idempotency_key": receipt.idempotency_key,
                "audit_id": receipt.audit_id,
                "status": "persisted_after_lookup",
            }
        )

    def _append_close_events(
        self,
        record: AnalysisResourceRecord,
        payload: dict[str, Any],
    ) -> AnalysisResourceRecoveryResult:
        receipt, events = self._receipt_and_close_events(record, payload)
        try:
            result = self._audit.append_lifecycle_events(
                AppendAnalysisLifecycleEvents(receipt=receipt, events=events)
            )
            if result is not None:
                raise RuntimeError("append_lifecycle_events 必须返回 None")
        except Exception as exc:
            return self._defer_or_quarantine(record, _safe_reason(exc))

        cleanup = self._mapping(payload["cleanup"], "cleanup")
        self._mapping(cleanup.get("audit_append"), "cleanup.audit_append")["state"] = "confirmed"
        states = (
            self._mapping(cleanup.get("session_close"), "cleanup.session_close").get("state"),
            self._mapping(cleanup.get("document"), "cleanup.document").get("state"),
            self._mapping(cleanup.get("audit_append"), "cleanup.audit_append").get("state"),
        )
        if not all(item in _FINAL_CLEANUP_STATES for item in states):
            return self._quarantine(
                record,
                payload,
                stage="recovery",
                reason="cleanup_facts_not_final_after_audit_append",
            )
        payload["diagnosis"] = {"stage": "", "reason": ""}
        advanced = self._advance(record, payload, AnalysisResourceState.CLEANED)
        logger.info(
            "文件分析资源已通过幂等审计补齐收口: task_id=%s version=%s",
            advanced.execution.task_id,
            advanced.version,
        )
        return AnalysisResourceRecoveryResult(
            advanced.execution,
            AnalysisResourceRecoveryOutcome.CLEANED,
        )

    def _receipt_and_close_events(
        self,
        record: AnalysisResourceRecord,
        payload: Mapping[str, Any],
    ) -> tuple[AnalysisInteractionAuditReceipt, tuple[AnalysisRagLifecycleEvent, ...]]:
        audit = self._mapping(payload.get("audit"), "audit")
        interaction = self._mapping(audit.get("interaction"), "audit.interaction")
        key = interaction.get("idempotency_key")
        audit_id = interaction.get("audit_id")
        if not isinstance(key, str) or not key.strip() or not isinstance(audit_id, str) or not audit_id.strip():
            raise ValueError("资源记录缺少已提交交互审计凭据")
        cleanup = self._mapping(payload.get("cleanup"), "cleanup")
        raw_events = cleanup.get("close_events")
        if not isinstance(raw_events, list) or not raw_events:
            raise ValueError("资源记录缺少可追加 close 生命周期事件")
        events = tuple(_event_from_payload(item) for item in raw_events)
        return (
            AnalysisInteractionAuditReceipt(
                execution=record.execution,
                idempotency_key=key.strip(),
                audit_id=audit_id.strip(),
            ),
            events,
        )

    def _defer_or_quarantine(
        self,
        record: AnalysisResourceRecord,
        reason: str,
    ) -> AnalysisResourceRecoveryResult:
        if record.recovery_deferral_count >= self._max_deferrals:
            quarantine_reason = f"deferral_exhausted_{reason}"[:256]
            try:
                quarantined = self._store.quarantine_recovery_record(
                    record.execution,
                    expected_state=record.state,
                    expected_version=record.version,
                    reason=quarantine_reason,
                )
            except Exception:
                logger.exception(
                    "文件分析资源恢复预算耗尽后隔离失败，保持原始现场: task_id=%s",
                    record.execution.task_id,
                )
                quarantined = False
            if not quarantined:
                return AnalysisResourceRecoveryResult(
                    record.execution,
                    AnalysisResourceRecoveryOutcome.PENDING,
                    "quarantine_cas_unconfirmed",
                )
            logger.critical(
                "文件分析资源恢复预算耗尽，已在不解析payload的前提下隔离: "
                "task_id=%s state=%s reason=%s",
                record.execution.task_id,
                record.state.value,
                quarantine_reason,
            )
            return AnalysisResourceRecoveryResult(
                record.execution,
                AnalysisResourceRecoveryOutcome.QUARANTINED,
                quarantine_reason,
            )
        try:
            multiplier = 2 ** record.recovery_deferral_count
            seconds = min(self._retry_max_seconds, self._retry_base_seconds * multiplier)
            now = self._clock()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("resource recovery clock 必须返回带时区 datetime")
            retry_at = (now.astimezone(timezone.utc) + timedelta(seconds=seconds)).isoformat()
            deferred = self._store.defer_recovery(
                record.execution,
                expected_version=record.version,
                retry_at=retry_at,
                reason=reason[:256],
            )
        except Exception:
            logger.exception(
                "文件分析资源恢复延期 CAS 失败，保持原记录不执行外部补偿: task_id=%s",
                record.execution.task_id,
            )
            return AnalysisResourceRecoveryResult(
                record.execution,
                AnalysisResourceRecoveryOutcome.PENDING,
                "deferral_cas_unconfirmed",
            )
        logger.warning(
            "文件分析资源恢复已延期: task_id=%s retry_at=%s reason=%s",
            deferred.execution.task_id,
            retry_at,
            reason,
        )
        return AnalysisResourceRecoveryResult(
            deferred.execution,
            AnalysisResourceRecoveryOutcome.DEFERRED,
            reason,
        )

    def _quarantine(
        self,
        record: AnalysisResourceRecord,
        payload: dict[str, Any],
        *,
        stage: str,
        reason: str,
    ) -> AnalysisResourceRecoveryResult:
        payload["diagnosis"] = {
            "stage": _safe_reason(stage, maximum=80),
            "reason": _safe_reason(reason),
        }
        try:
            advanced = self._advance(record, payload, AnalysisResourceState.QUARANTINED)
        except Exception:
            logger.exception(
                "文件分析资源隔离 CAS 未确认，保持原事实且禁止自动补偿: task_id=%s",
                record.execution.task_id,
            )
            return AnalysisResourceRecoveryResult(
                record.execution,
                AnalysisResourceRecoveryOutcome.PENDING,
                "quarantine_cas_unconfirmed",
            )
        logger.critical(
            "文件分析资源已隔离，禁止自动重放或删除: task_id=%s stage=%s reason=%s",
            advanced.execution.task_id,
            stage,
            _safe_reason(reason),
        )
        return AnalysisResourceRecoveryResult(
            advanced.execution,
            AnalysisResourceRecoveryOutcome.QUARANTINED,
            _safe_reason(reason),
        )

    @staticmethod
    def _payload(record: AnalysisResourceRecord) -> dict[str, Any]:
        payload = record.record_payload.to_dict()
        if payload.get("schema_version") not in {
            _RESOURCE_SCHEMA_VERSION_V1,
            _RESOURCE_SCHEMA_VERSION_V2,
            _RESOURCE_SCHEMA_VERSION,
        }:
            raise ValueError("资源记录 schema_version 不受支持")
        for key in ("audit", "cleanup", "diagnosis"):
            if not isinstance(payload.get(key), Mapping):
                raise ValueError(f"资源记录缺少 {key} 对象")
            payload[key] = dict(payload[key])
        return payload

    @staticmethod
    def _mapping(value: object, name: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"资源记录 {name} 必须是对象")
        if isinstance(value, dict):
            return value
        return dict(value)

    def _advance(
        self,
        record: AnalysisResourceRecord,
        payload: Mapping[str, object],
        target: AnalysisResourceState,
    ) -> AnalysisResourceRecord:
        advanced = self._store.advance(
            AnalysisResourceCommand(
                execution=record.execution,
                expected_state=record.state,
                expected_version=record.version,
                target_state=target,
                record_payload=FrozenJsonObject.from_mapping(
                    dict(payload),
                    name="analysis_resource_recovery_payload",
                ),
            )
        )
        if advanced.execution != record.execution or advanced.state is not target:
            raise RuntimeError("资源恢复 CAS 返回记录不一致")
        return advanced


__all__ = (
    "AnalysisResourceLifecycle",
    "AnalysisResourceLifecycleError",
    "AnalysisResourceRecoveryOutcome",
    "AnalysisResourceRecoveryResult",
    "AnalysisResourceSweepResult",
    "RecoverAnalysisResources",
)
