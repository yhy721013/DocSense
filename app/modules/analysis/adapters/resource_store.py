"""新 Analysis execution 的 SQLite 资源事实适配器。

资源记录只保存任务目录、RAG 不透明引用、审计凭据和清理意图等可恢复事实。它不执行
AnythingLLM 删除、文件删除或网络调用；外部副作用必须由 Application 在先写意图、后调用
并再次落库的边界中完成，未知结果只能隔离。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import logging
from typing import Any

from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.analysis.ports import (
    AnalysisExecutionRef,
    AnalysisResourceCommand,
    AnalysisResourcePort,
    AnalysisResourceRecord,
    AnalysisResourceScanBatch,
    AnalysisResourceState,
)
from app.modules.tasks.domain import TaskId
from app.services.llm_service.task_service import LLMTaskService


logger = logging.getLogger(__name__)

_ANALYSIS_BUSINESS_TYPE = "file"


def _utc_now() -> datetime:
    """返回带时区 UTC 时钟，避免 SQLite 记录混入本地无时区时间。"""

    return datetime.now(timezone.utc)


def _clock_iso(clock: Callable[[], datetime]) -> str:
    """严格规范化依赖注入时钟，测试与生产都使用同一时间合同。"""

    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("resource store clock 必须返回 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("resource store clock 必须返回带时区 datetime")
    return value.astimezone(timezone.utc).isoformat()


class AnalysisResourceStoreConcurrencyError(RuntimeError):
    """资源 ``state + version`` CAS 未命中时的可判定并发结果。"""


class SQLiteAnalysisResourceStoreAdapter(AnalysisResourcePort):
    """把 Analysis Resource Port 映射到短 SQLite 事务。

    每次写入均由 ``LLMTaskService`` 在 ``BEGIN IMMEDIATE`` 中完成；本适配器绝不在
    SQLite 写事务内执行网络、睡眠或文件操作。恢复者收到 CAS 冲突后必须重读事实，而不是
    用旧 payload 覆盖另一执行者刚持久化的外部引用。
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

    def create(self, command: AnalysisResourceCommand) -> AnalysisResourceRecord:
        """在任何远端 RAG 资源创建前登记 ``tracking`` 事实。"""

        if not isinstance(command, AnalysisResourceCommand):
            raise TypeError("command 必须是 AnalysisResourceCommand")
        if command.expected_state is not None or command.expected_version is not None:
            raise ValueError("资源创建不得携带 expected state/version")
        raw = self._task_service.create_analysis_resource_record(
            execution_id=command.execution.task_id.value,
            business_type=_ANALYSIS_BUSINESS_TYPE,
            business_key=command.execution.file_name,
            state=command.target_state.value,
            record_payload=command.record_payload.to_dict(),
            created_at=_clock_iso(self._clock),
        )
        record = self._decode_record(raw)
        self._require_same_execution(command.execution, record, operation="create")
        if record.state is not AnalysisResourceState.TRACKING:
            raise RuntimeError("Analysis资源创建未返回tracking记录")
        return record

    def get(
        self,
        execution: AnalysisExecutionRef,
    ) -> AnalysisResourceRecord | None:
        """读取当前资源事实；读取本身不触发删除、重试或状态转换。"""

        if not isinstance(execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        raw = self._task_service.get_analysis_resource_record(execution.task_id.value)
        if raw is None:
            return None
        record = self._decode_record(raw)
        self._require_same_execution(execution, record, operation="get")
        return record

    def advance(self, command: AnalysisResourceCommand) -> AnalysisResourceRecord:
        """以 state/version CAS 推进状态或补充同状态下的新外部引用。"""

        if not isinstance(command, AnalysisResourceCommand):
            raise TypeError("command 必须是 AnalysisResourceCommand")
        if command.expected_state is None or command.expected_version is None:
            raise ValueError("资源推进必须携带 expected state/version")
        raw = self._task_service.advance_analysis_resource_record(
            execution_id=command.execution.task_id.value,
            business_type=_ANALYSIS_BUSINESS_TYPE,
            business_key=command.execution.file_name,
            expected_state=command.expected_state.value,
            expected_version=command.expected_version,
            target_state=command.target_state.value,
            record_payload=command.record_payload.to_dict(),
            updated_at=_clock_iso(self._clock),
        )
        if raw is None:
            raise AnalysisResourceStoreConcurrencyError(
                "Analysis资源记录state/version条件写未命中"
            )
        record = self._decode_record(raw)
        self._require_same_execution(command.execution, record, operation="advance")
        if record.state is not command.target_state:
            raise RuntimeError("Analysis资源推进返回状态与目标不一致")
        if record.version <= command.expected_version:
            raise RuntimeError("Analysis资源推进未递增version")
        return record

    def list_recoverable(self, *, limit: int) -> AnalysisResourceScanBatch:
        """按稳定顺序读取到期恢复候选；已隔离记录不会进入自动扫描。"""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit 必须是1~1000的整数")
        identifiers = self._task_service.list_recoverable_analysis_resource_ids(
            limit=limit,
            ready_at=_clock_iso(self._clock),
        )
        if len(identifiers) > limit or len(set(identifiers)) != len(identifiers):
            raise RuntimeError("Analysis资源恢复扫描返回了无效的执行身份")
        records: list[AnalysisResourceRecord] = []
        quarantined_count = 0
        pending_count = 0
        for execution_id in identifiers:
            control = self._task_service.get_analysis_resource_control_record(
                execution_id
            )
            if control is None:
                # 候选读和逐条读之间可能被另一个恢复者清理。该情况不构成可重放外部
                # 副作用，跳过即可，并用日志保留诊断线索。
                logger.info(
                    "Analysis资源恢复候选已被其他执行者收敛，跳过: execution_id=%s",
                    execution_id,
                )
                continue
            try:
                raw = self._task_service.get_analysis_resource_record(execution_id)
                if raw is None:
                    continue
                records.append(self._decode_record(raw))
            except (RuntimeError, TypeError, ValueError) as exc:
                # 毒化 payload 不能让一个有界扫描整体失败，更不能反复占据稳定排序的
                # 最老位置饿死后续记录。隔离入口只依赖控制面 identity/state/version，
                # 保留原始 payload 供人工取证，且绝不执行外部补偿。
                reason = f"resource_decode_{type(exc).__name__}"[:256]
                try:
                    quarantined = self._task_service.quarantine_analysis_resource_recovery_record(
                        execution_id=execution_id,
                        business_type=str(control.get("business_type") or ""),
                        business_key=str(control.get("business_key") or ""),
                        expected_state=str(control.get("state") or ""),
                        expected_version=control.get("version"),  # type: ignore[arg-type]
                        reason=reason,
                        updated_at=_clock_iso(self._clock),
                    )
                except Exception:
                    quarantined = False
                    logger.exception(
                        "Analysis毒化资源记录隔离失败，保持原始现场: execution_id=%s",
                        execution_id,
                    )
                logger.log(
                    logging.CRITICAL if quarantined else logging.WARNING,
                    "Analysis毒化资源记录已从本轮扫描隔离: "
                    "execution_id=%s quarantined=%s error_type=%s "
                    "payload_bytes=%s payload_sha256=%s",
                    execution_id,
                    quarantined,
                    type(exc).__name__,
                    control.get("payload_bytes"),
                    str(control.get("payload_sha256") or "")[:16],
                )
                if quarantined:
                    quarantined_count += 1
                else:
                    pending_count += 1
        return AnalysisResourceScanBatch(
            records=tuple(records),
            quarantined_count=quarantined_count,
            pending_count=pending_count,
        )

    def defer_recovery(
        self,
        execution: AnalysisExecutionRef,
        *,
        expected_version: int,
        retry_at: str,
        reason: str,
    ) -> AnalysisResourceRecord:
        """仅对仍可恢复的非隔离记录记录有限退避。"""

        if not isinstance(execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        raw = self._task_service.defer_analysis_resource_recovery(
            execution.task_id.value,
            expected_version=expected_version,
            retry_at=retry_at,
            reason=reason,
        )
        if raw is None:
            raise AnalysisResourceStoreConcurrencyError(
                "Analysis资源恢复延期条件写未命中"
            )
        record = self._decode_record(raw)
        self._require_same_execution(execution, record, operation="defer_recovery")
        if record.version <= expected_version:
            raise RuntimeError("Analysis资源恢复延期未递增version")
        return record

    def quarantine_recovery_record(
        self,
        execution: AnalysisExecutionRef,
        *,
        expected_state: AnalysisResourceState,
        expected_version: int,
        reason: str,
    ) -> bool:
        """不解码业务 payload，条件隔离恢复预算耗尽或结构不可用的记录。"""

        if not isinstance(execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(expected_state, AnalysisResourceState):
            raise TypeError("expected_state 必须是 AnalysisResourceState")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise ValueError("expected_version 必须是非负整数")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须是非空 str")
        return self._task_service.quarantine_analysis_resource_recovery_record(
            execution_id=execution.task_id.value,
            business_type=_ANALYSIS_BUSINESS_TYPE,
            business_key=execution.file_name,
            expected_state=expected_state.value,
            expected_version=expected_version,
            reason=reason.strip()[:256],
            updated_at=_clock_iso(self._clock),
        )

    @staticmethod
    def _decode_record(raw: object) -> AnalysisResourceRecord:
        """对 TaskService 返还的动态字典做一次严格、无副作用的领域映射。"""

        if not isinstance(raw, Mapping):
            raise TypeError("Analysis资源记录必须是Mapping")
        if raw.get("business_type") != _ANALYSIS_BUSINESS_TYPE:
            raise RuntimeError("Analysis资源记录business_type无效")
        execution_id = raw.get("execution_id")
        business_key = raw.get("business_key")
        # 资源表以 execution_id 为主键；查询时与 execution 联表读取批次身份，避免
        # Adapter 从业务键反查或猜测所属批次。
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise RuntimeError("Analysis资源记录缺少execution_id")
        if not isinstance(business_key, str) or not business_key.strip():
            raise RuntimeError("Analysis资源记录缺少business_key")
        batch_id = raw.get("batch_id")
        batch_sequence = raw.get("batch_sequence")
        try:
            state = AnalysisResourceState(raw.get("state"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Analysis资源记录state无效") from exc
        payload = raw.get("record_payload")
        if not isinstance(payload, Mapping):
            raise RuntimeError("Analysis资源记录payload无效")
        try:
            return AnalysisResourceRecord(
                execution=AnalysisExecutionRef(
                    task_id=TaskId(execution_id),
                    file_name=business_key,
                    batch_id=batch_id,  # type: ignore[arg-type]
                    batch_sequence=batch_sequence,  # type: ignore[arg-type]
                ),
                state=state,
                version=raw.get("version"),  # type: ignore[arg-type]
                record_payload=FrozenJsonObject.from_mapping(
                    dict(payload),
                    name="analysis_resource_record",
                ),
                recovery_deferral_count=raw.get("recovery_deferral_count", 0),  # type: ignore[arg-type]
                next_recovery_at=raw.get("next_recovery_at"),  # type: ignore[arg-type]
                last_recovery_reason=raw.get("last_recovery_reason", ""),  # type: ignore[arg-type]
            )
        except Exception as exc:
            raise RuntimeError("Analysis资源记录字段无效") from exc

    @staticmethod
    def _require_same_execution(
        expected: AnalysisExecutionRef,
        record: AnalysisResourceRecord,
        *,
        operation: str,
    ) -> None:
        """确认 Adapter 返回的记录属于同一个完整 execution。"""

        # 虽然资源表主键是 execution_id，但读取时 TaskService 会联表带回批次身份。
        # 因此这里必须比较完整的 AnalysisExecutionRef，避免同一 task/file 在错误批次
        # 身份下被上层误当成可继续推进的资源事实。
        if record.execution != expected:
            raise RuntimeError(f"Analysis资源{operation}返回了其他execution")


__all__ = (
    "AnalysisResourceStoreConcurrencyError",
    "SQLiteAnalysisResourceStoreAdapter",
)
