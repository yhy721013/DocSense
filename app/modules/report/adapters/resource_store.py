"""基于兼容 SQLite 任务库的报告资源恢复事实适配器。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import logging
from typing import Any

from app.modules.report.domain import (
    ReportCleanupError,
    ReportResourceConcurrencyError,
    ReportResourceNotReadyError,
)
from app.modules.report.ports import (
    ReportArtifactCategory,
    ReportArtifactRef,
    ReportArtifactScope,
    ReportAuditReceipt,
    ReportCleanupPartState,
    ReportRagCleanupRef,
    ReportRagLifecycleEvent,
    ReportResourceRecord,
    ReportResourceState,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.services.llm_service.task_service import LLMTaskService


logger = logging.getLogger(__name__)

_PAYLOAD_SCHEMA_VERSION = 4
_PAYLOAD_FIELDS_V2 = frozenset(
    {
        "schema_version",
        "external_state",
        "artifact_state",
        "cleanup_ref",
        "audit_receipt",
        "final_artifact",
        "retained",
        "pending_events",
        "pending_events_succeeded",
        "pending_artifacts",
        "next_sequence_no",
        "external_attempt_open",
        "external_attempt_started_at",
        "attempt_count",
        "last_error_stage",
        "last_error_message",
    }
)
_PAYLOAD_FIELDS_V3 = _PAYLOAD_FIELDS_V2.union(
    {"external_attempt_token", "external_attempt_heartbeat_at"}
)
_PAYLOAD_FIELDS = _PAYLOAD_FIELDS_V3.union({"operation_attempts"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clock_iso(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("resource store clock 必须返回 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("resource store clock 必须返回带时区 datetime")
    return value.astimezone(timezone.utc).isoformat()


class SQLiteReportResourceStoreAdapter:
    """把资源恢复 DTO 映射到 ``report_resource_records``。

    本适配器只执行短数据库事务，不进行文件删除、AnythingLLM 调用或审计追加。最终
    Artifact 所有权由 TaskService 在 ``prepare_cleanup`` 时读取不可变 execution 终态后
    权威决定，避免 Application 或旧 Worker 自行声明 retain。
    """

    def __init__(
        self,
        task_service: LLMTaskService,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(task_service, LLMTaskService):
            raise TypeError("task_service 必须是 LLMTaskService")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self._task_service = task_service
        self._clock = clock

    def create(self, record: ReportResourceRecord) -> ReportResourceRecord:
        if not isinstance(record, ReportResourceRecord):
            raise TypeError("record 必须是 ReportResourceRecord")
        if record.version != 0:
            raise ValueError("新资源记录 version 必须为 0")
        raw = self._task_service.create_report_resource_record(
            execution_id=record.task_id.value,
            business_type=record.business_ref.business_type,
            business_key=record.business_ref.business_key,
            artifact_namespace=record.scope.namespace,
            state=record.state.value,
            record_payload=self._encode_payload(record),
            created_at=_clock_iso(self._clock),
        )
        return self._decode_record(raw)

    def get(self, task_id: TaskId) -> ReportResourceRecord | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        raw = self._task_service.get_report_resource_record(task_id.value)
        return self._decode_record(raw) if raw is not None else None

    def save(
        self,
        record: ReportResourceRecord,
        *,
        expected_version: int,
    ) -> ReportResourceRecord:
        if not isinstance(record, ReportResourceRecord):
            raise TypeError("record 必须是 ReportResourceRecord")
        if record.version != expected_version:
            raise ValueError("record.version 与 expected_version 不一致")
        raw = self._task_service.save_report_resource_record(
            execution_id=record.task_id.value,
            business_type=record.business_ref.business_type,
            business_key=record.business_ref.business_key,
            artifact_namespace=record.scope.namespace,
            state=record.state.value,
            record_payload=self._encode_payload(record),
            expected_version=expected_version,
            updated_at=_clock_iso(self._clock),
        )
        if raw is None:
            # CAS 未命中是可预期的并发结果，必须与数据库损坏、解码失败等真正故障
            # 区分。Application 只会对该精确异常重读最新事实，绝不会吞掉其他错误。
            raise ReportResourceConcurrencyError("报告资源恢复记录并发版本冲突")
        return self._decode_record(raw)

    def prepare_cleanup(self, task_id: TaskId) -> ReportResourceRecord:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        execution = self._task_service.get_task_execution(task_id.value)
        if execution is None:
            raise ReportCleanupError("报告资源对应的 execution 不存在")
        if execution.get("execution_state") not in {"succeeded", "failed", "stale"}:
            raise ReportResourceNotReadyError("报告 execution 尚未形成可清理终态")
        raw = self._task_service.prepare_report_resource_cleanup(
            task_id.value,
            updated_at=_clock_iso(self._clock),
        )
        return self._decode_record(raw)

    def list_recoverable(self, *, limit: int) -> tuple[TaskId, ...]:
        return tuple(
            TaskId(value)
            for value in self._task_service.list_recoverable_report_resource_ids(
                limit=limit,
                ready_at=_clock_iso(self._clock),
            )
        )

    def defer_recovery(
        self,
        task_id: TaskId,
        *,
        retry_at: str,
        reason: str,
    ) -> bool:
        """即使 payload 已损坏，也通过独立调度列让该记录暂时让出扫描首页。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(retry_at, str) or not retry_at.strip():
            raise ValueError("retry_at 不能为空")
        try:
            parsed = datetime.fromisoformat(retry_at.strip())
        except ValueError as exc:
            raise ValueError("retry_at 必须是 ISO 时间") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("retry_at 必须包含时区")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 不能为空")
        normalized_reason = reason.strip()
        if len(normalized_reason) > 256:
            raise ValueError("reason 长度不能超过 256")
        return self._task_service.defer_report_resource_recovery(
            task_id.value,
            retry_at=parsed.astimezone(timezone.utc).isoformat(),
            reason=normalized_reason,
        )

    @classmethod
    def _encode_payload(cls, record: ReportResourceRecord) -> dict[str, Any]:
        return {
            "schema_version": _PAYLOAD_SCHEMA_VERSION,
            "external_state": record.external_state.value,
            "artifact_state": record.artifact_state.value,
            "cleanup_ref": record.cleanup_ref.value if record.cleanup_ref else None,
            "audit_receipt": (
                {
                    "task_id": record.audit_receipt.task_id.value,
                    "idempotency_key": record.audit_receipt.idempotency_key,
                    "audit_id": record.audit_receipt.audit_id,
                }
                if record.audit_receipt
                else None
            ),
            "final_artifact": cls._encode_artifact(record.final_artifact),
            "retained": [cls._encode_artifact(item) for item in record.retained],
            "pending_events": [cls._encode_event(item) for item in record.pending_events],
            "pending_events_succeeded": record.pending_events_succeeded,
            "pending_artifacts": [
                cls._encode_artifact(item) for item in record.pending_artifacts
            ],
            "next_sequence_no": record.next_sequence_no,
            "external_attempt_open": record.external_attempt_open,
            "external_attempt_token": record.external_attempt_token,
            "external_attempt_started_at": record.external_attempt_started_at,
            "external_attempt_heartbeat_at": record.external_attempt_heartbeat_at,
            "attempt_count": record.attempt_count,
            "operation_attempts": dict(record.operation_attempts),
            "last_error_stage": record.last_error_stage,
            "last_error_message": record.last_error_message,
        }

    @classmethod
    def _decode_record(cls, raw: Mapping[str, Any]) -> ReportResourceRecord:
        if not isinstance(raw, Mapping):
            raise TypeError("resource store 返回值必须是 Mapping")
        payload = raw.get("record_payload")
        if not isinstance(payload, Mapping):
            raise ReportCleanupError("报告资源恢复 payload 不是对象")
        schema_version = payload.get("schema_version")
        expected_fields = (
            _PAYLOAD_FIELDS
            if schema_version == _PAYLOAD_SCHEMA_VERSION
            else _PAYLOAD_FIELDS_V3
            if schema_version == 3
            else _PAYLOAD_FIELDS_V2
            if schema_version == 2
            else frozenset()
        )
        unknown = set(payload).difference(expected_fields)
        missing = expected_fields.difference(payload)
        if not expected_fields or unknown or missing:
            raise ReportCleanupError("报告资源恢复 payload Schema 不兼容")
        task_id = TaskId(raw.get("execution_id"))  # type: ignore[arg-type]
        business_ref = TaskBusinessRef(
            raw.get("business_type"),  # type: ignore[arg-type]
            raw.get("business_key"),  # type: ignore[arg-type]
        )
        scope = ReportArtifactScope(
            task_id,
            raw.get("artifact_namespace"),  # type: ignore[arg-type]
        )
        audit_payload = payload.get("audit_receipt")
        audit_receipt = None
        if audit_payload is not None:
            if not isinstance(audit_payload, Mapping):
                raise ReportCleanupError("报告资源审计凭据格式错误")
            audit_receipt = ReportAuditReceipt(
                TaskId(audit_payload.get("task_id")),  # type: ignore[arg-type]
                audit_payload.get("idempotency_key"),  # type: ignore[arg-type]
                audit_payload.get("audit_id"),  # type: ignore[arg-type]
            )
        cleanup_value = payload.get("cleanup_ref")
        cleanup_ref = (
            ReportRagCleanupRef(cleanup_value)  # type: ignore[arg-type]
            if cleanup_value is not None
            else None
        )
        external_attempt_open = payload.get("external_attempt_open")
        started_at = payload.get("external_attempt_started_at")
        # v2 记录没有 fencing token/心跳；兼容读取时以原开始时间作为初始心跳，并使用
        # 仅用于识别旧租约的确定性 token。下一次保存会自动升级为当前 v4。
        legacy_open = schema_version == 2 and external_attempt_open is True
        return ReportResourceRecord(
            task_id=task_id,
            business_ref=business_ref,
            scope=scope,
            state=ReportResourceState(raw.get("state")),  # type: ignore[arg-type]
            external_state=ReportCleanupPartState(
                payload.get("external_state")  # type: ignore[arg-type]
            ),
            artifact_state=ReportCleanupPartState(
                payload.get("artifact_state")  # type: ignore[arg-type]
            ),
            cleanup_ref=cleanup_ref,
            audit_receipt=audit_receipt,
            final_artifact=cls._decode_optional_artifact(
                task_id,
                payload.get("final_artifact"),
            ),
            retained=cls._decode_artifact_list(task_id, payload.get("retained")),
            pending_events=cls._decode_events(payload.get("pending_events")),
            pending_events_succeeded=payload.get(
                "pending_events_succeeded"
            ),  # type: ignore[arg-type]
            pending_artifacts=cls._decode_artifact_list(
                task_id,
                payload.get("pending_artifacts"),
            ),
            next_sequence_no=payload.get("next_sequence_no"),  # type: ignore[arg-type]
            external_attempt_open=external_attempt_open,  # type: ignore[arg-type]
            external_attempt_token=(
                f"legacy-v2:{raw.get('version')}"
                if legacy_open
                else payload.get("external_attempt_token", "")
            ),  # type: ignore[arg-type]
            external_attempt_started_at=started_at,  # type: ignore[arg-type]
            external_attempt_heartbeat_at=(
                started_at
                if legacy_open
                else payload.get("external_attempt_heartbeat_at")
            ),  # type: ignore[arg-type]
            attempt_count=payload.get("attempt_count"),  # type: ignore[arg-type]
            operation_attempts=cls._decode_operation_attempts(
                payload.get("operation_attempts", {})
            ),
            last_error_stage=payload.get("last_error_stage"),  # type: ignore[arg-type]
            last_error_message=payload.get("last_error_message"),  # type: ignore[arg-type]
            version=raw.get("version"),  # type: ignore[arg-type]
        )

    @staticmethod
    def _decode_operation_attempts(raw: object) -> tuple[tuple[str, int], ...]:
        """兼容 v2/v3 空基线，并严格拒绝损坏的 v4 重试审计数据。"""

        if not isinstance(raw, Mapping):
            raise ReportCleanupError("operation_attempts 必须是对象")
        decoded: list[tuple[str, int]] = []
        for operation, attempt_no in raw.items():
            if not isinstance(operation, str) or not operation.strip():
                raise ReportCleanupError("operation_attempts operation 无效")
            if (
                isinstance(attempt_no, bool)
                or not isinstance(attempt_no, int)
                or attempt_no < 1
            ):
                raise ReportCleanupError("operation_attempts attempt_no 无效")
            decoded.append((operation.strip(), attempt_no))
        return tuple(sorted(decoded))

    @staticmethod
    def _encode_artifact(artifact: ReportArtifactRef | None) -> dict[str, Any] | None:
        if artifact is None:
            return None
        return {
            "task_id": artifact.task_id.value,
            "artifact_id": artifact.artifact_id,
            "category": artifact.category.value,
            "sequence_no": artifact.sequence_no,
            "size_bytes": artifact.size_bytes,
            "checksum": artifact.checksum,
        }

    @classmethod
    def _decode_optional_artifact(
        cls,
        task_id: TaskId,
        raw: object,
    ) -> ReportArtifactRef | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ReportCleanupError("Artifact 恢复引用格式错误")
        artifact = ReportArtifactRef(
            TaskId(raw.get("task_id")),  # type: ignore[arg-type]
            raw.get("artifact_id"),  # type: ignore[arg-type]
            ReportArtifactCategory(raw.get("category")),  # type: ignore[arg-type]
            sequence_no=raw.get("sequence_no"),  # type: ignore[arg-type]
            size_bytes=raw.get("size_bytes"),  # type: ignore[arg-type]
            checksum=raw.get("checksum"),  # type: ignore[arg-type]
        )
        if artifact.task_id != task_id:
            raise ReportCleanupError("Artifact 恢复引用跨越 task_id")
        return artifact

    @classmethod
    def _decode_artifact_list(
        cls,
        task_id: TaskId,
        raw: object,
    ) -> tuple[ReportArtifactRef, ...]:
        if not isinstance(raw, list):
            raise ReportCleanupError("Artifact 恢复引用列表格式错误")
        return tuple(cls._decode_required_artifact(task_id, item) for item in raw)

    @classmethod
    def _decode_required_artifact(
        cls,
        task_id: TaskId,
        raw: object,
    ) -> ReportArtifactRef:
        artifact = cls._decode_optional_artifact(task_id, raw)
        if artifact is None:
            raise ReportCleanupError("Artifact 恢复引用不能为空")
        return artifact

    @staticmethod
    def _encode_event(event: ReportRagLifecycleEvent) -> dict[str, Any]:
        return {
            "sequence_no": event.sequence_no,
            "operation": event.operation,
            "attempt_no": event.attempt_no,
            "success": event.success,
            "external_ref": event.external_ref,
            "failure_stage": event.failure_stage,
            "error_message": event.error_message,
        }

    @staticmethod
    def _decode_events(raw: object) -> tuple[ReportRagLifecycleEvent, ...]:
        if not isinstance(raw, list):
            raise ReportCleanupError("生命周期恢复事件列表格式错误")
        events: list[ReportRagLifecycleEvent] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ReportCleanupError("生命周期恢复事件格式错误")
            events.append(
                ReportRagLifecycleEvent(
                    sequence_no=item.get("sequence_no"),  # type: ignore[arg-type]
                    operation=item.get("operation"),  # type: ignore[arg-type]
                    attempt_no=item.get("attempt_no"),  # type: ignore[arg-type]
                    success=item.get("success"),  # type: ignore[arg-type]
                    external_ref=item.get("external_ref"),  # type: ignore[arg-type]
                    failure_stage=item.get("failure_stage"),  # type: ignore[arg-type]
                    error_message=item.get("error_message"),  # type: ignore[arg-type]
                )
            )
        return tuple(events)


__all__ = ["SQLiteReportResourceStoreAdapter"]
