"""基于兼容 ``LLMTaskService`` 的通用 SQLite TaskCommand Adapter。

Adapter 只认识 tasks 的通用命令与执行事实。各业务输入/结果如何编码由注入的 Codec
负责，因此 tasks 模块不会反向导入 report、analysis 或 weaponry。后续替换 MySQL
Repository 时，Application 和业务 Codec 均无需改变签名。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Generic, Protocol, TypeVar
from uuid import uuid4

from app.modules.tasks.domain import TaskBusinessRef, TaskExecutionSnapshot, TaskId
from app.modules.tasks.ports import (
    ExpectedProgressUpdate,
    ExpectedTaskCompletion,
    TaskClaimOutcome,
    TaskClaimResult,
    TaskSubmissionCommand,
    TaskSubmissionOutcome,
    TaskSubmissionResult,
    TaskQueueSnapshot,
)
from app.services.llm_service.task_service import LLMTaskService


TTaskSubmission = TypeVar("TTaskSubmission")
TTaskInput = TypeVar("TTaskInput")
TTaskResult = TypeVar("TTaskResult")


logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _aware_utc_iso(value: object, *, name: str) -> str:
    """把内部时钟值规范为带时区的 UTC ISO 文本。

    SQLite Repository 会统一以 UTC 保存 execution 时间。若 Adapter 在事务前构造的
    ``TaskExecutionSnapshot`` 仍保留调用方的 ``+08:00`` 等原始偏移，同一时刻会因为
    文本不同被误判为“提交后读回损坏”，报告用例也会拒绝这份快照。受理前先规范化，
    可以保证内存返回值、输入快照和持久化行使用同一种时间表示。
    """

    normalized = _required_text(value, name=name)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是 ISO 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} 必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat()


def _new_task_id() -> TaskId:
    return TaskId(uuid4().hex)


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


class LegacyTaskCommandAdapterError(RuntimeError):
    """兼容存储返回了损坏、未知或跨业务的 execution 数据。"""


@dataclass(frozen=True)
class EncodedTaskSubmission(Generic[TTaskInput]):
    """业务 Codec 交给通用 SQLite Adapter 的完整受理编码。"""

    input_snapshot: TTaskInput
    input_payload: Mapping[str, Any]
    projection_request_payload: Mapping[str, Any]
    initial_public_status: str
    active_public_statuses: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.input_snapshot is None:
            raise ValueError("input_snapshot 不能为空")
        if not isinstance(self.input_payload, Mapping):
            raise TypeError("input_payload 必须是 Mapping")
        if not isinstance(self.projection_request_payload, Mapping):
            raise TypeError("projection_request_payload 必须是 Mapping")
        object.__setattr__(
            self,
            "initial_public_status",
            _required_text(
                self.initial_public_status,
                name="initial_public_status",
            ),
        )
        statuses = tuple(self.active_public_statuses)
        if not statuses:
            raise ValueError("active_public_statuses 不能为空")
        normalized = tuple(
            _required_text(item, name="active_public_status")
            for item in statuses
        )
        object.__setattr__(self, "active_public_statuses", normalized)


@dataclass(frozen=True)
class EncodedTaskResult:
    """区分追加 execution 事实与旧 ``llm_tasks`` 公共回调投影。

    两类 JSON 具有不同用途，禁止再次把包含内部 Schema、Artifact 引用的执行结果直接
    暴露给 check-task 回调恢复。具体业务 Codec 可以让 execution 仅保存恢复和审计所需
    的最小事实，而 projection 必须保持既有外部 Callback 载荷。
    """

    execution_result_payload: Mapping[str, Any]
    projection_result_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.execution_result_payload, Mapping):
            raise TypeError("execution_result_payload 必须是 Mapping")
        if not isinstance(self.projection_result_payload, Mapping):
            raise TypeError("projection_result_payload 必须是 Mapping")


class TaskCommandCodec(
    Protocol,
    Generic[TTaskSubmission, TTaskInput, TTaskResult],
):
    """把业务 DTO 映射为供应商无关 JSON 的 Adapter 内部协议。"""

    task_type: str

    def encode_submission(
        self,
        command: TaskSubmissionCommand[TTaskSubmission],
        *,
        task_id: TaskId,
        accepted_at: str,
    ) -> EncodedTaskSubmission[TTaskInput]:
        ...

    def decode_input(
        self,
        *,
        schema_version: int,
        payload: Mapping[str, Any],
    ) -> TTaskInput:
        ...

    def encode_result(self, result: TTaskResult) -> EncodedTaskResult:
        ...


class LegacyTaskCommandAdapter(
    Generic[TTaskSubmission, TTaskInput, TTaskResult],
):
    """以独立 SQLite 连接实现原子受理、领取和 expected TaskId 条件写。"""

    def __init__(
        self,
        task_service: LLMTaskService,
        codec: TaskCommandCodec[TTaskSubmission, TTaskInput, TTaskResult],
        *,
        task_id_factory: Callable[[], TaskId] = _new_task_id,
        clock: Callable[[], str] = _utc_now_iso,
    ) -> None:
        if not isinstance(task_service, LLMTaskService):
            raise TypeError("task_service 必须是 LLMTaskService")
        task_type = _required_text(getattr(codec, "task_type", None), name="task_type")
        for method_name in ("encode_submission", "decode_input", "encode_result"):
            if not callable(getattr(codec, method_name, None)):
                raise TypeError(f"codec 缺少可调用方法: {method_name}")
        if not callable(task_id_factory):
            raise TypeError("task_id_factory 必须可调用")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self._task_service = task_service
        self._codec = codec
        self._task_type = task_type
        self._task_id_factory = task_id_factory
        self._clock = clock

    def create_if_allowed(
        self,
        command: TaskSubmissionCommand[TTaskSubmission],
    ) -> TaskSubmissionResult[TTaskInput]:
        if not isinstance(command, TaskSubmissionCommand):
            raise TypeError("command 必须是 TaskSubmissionCommand")
        if (
            command.task_type != self._task_type
            or command.business_ref.business_type != self._task_type
        ):
            raise ValueError("TaskCommand 与 Codec 业务类型不一致")
        task_id = self._task_id_factory()
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id_factory 必须返回 TaskId")
        accepted_at = _aware_utc_iso(self._clock(), name="accepted_at")
        encoded = self._codec.encode_submission(
            command,
            task_id=task_id,
            accepted_at=accepted_at,
        )
        if not isinstance(encoded, EncodedTaskSubmission):
            raise TypeError("Codec 必须返回 EncodedTaskSubmission")

        # 所有可能由业务 Codec 触发的解码/领域校验必须在数据库事务前完成。否则 accepted
        # 事实已经提交后才抛出 500，会让调用方重试并收到 409，同时即时 Dispatcher 唤醒
        # 也不会发生。Repository 返回值只作为提交后诊断，不再决定公开受理是否成功。
        try:
            roundtrip_input = self._codec.decode_input(
                schema_version=command.input_schema_version,
                payload=encoded.input_payload,
            )
        except Exception as exc:
            raise LegacyTaskCommandAdapterError(
                "Codec 输入快照在受理前无法往返解码"
            ) from exc
        if roundtrip_input != encoded.input_snapshot:
            raise LegacyTaskCommandAdapterError("Codec 输入快照往返解码不一致")
        expected_execution = TaskExecutionSnapshot(
            task_id=task_id,
            task_type=command.task_type,
            business_ref=command.business_ref,
            execution_state="accepted",
            public_status=encoded.initial_public_status,
            progress=0.0,
            message="",
            input_snapshot=encoded.input_snapshot,
            accepted_at=accepted_at,
            trace_id=command.trace_id,
        )

        raw_result = self._task_service.create_task_execution_if_allowed(
            execution_id=task_id.value,
            business_type=command.business_ref.business_type,
            business_key=command.business_ref.business_key,
            input_schema_version=command.input_schema_version,
            input_payload=encoded.input_payload,
            projection_request_payload=encoded.projection_request_payload,
            initial_public_status=encoded.initial_public_status,
            active_public_statuses=encoded.active_public_statuses,
            trace_id=command.trace_id,
            accepted_at=accepted_at,
        )
        if not isinstance(raw_result, Mapping):
            raise LegacyTaskCommandAdapterError("原子受理返回值必须是 Mapping")
        outcome = self._submission_outcome(raw_result.get("outcome"))
        if outcome is not TaskSubmissionOutcome.ACCEPTED:
            if raw_result.get("execution") is not None:
                raise LegacyTaskCommandAdapterError("冲突受理结果不得携带 execution")
            return TaskSubmissionResult(outcome)

        try:
            persisted_execution = self._decode_execution(raw_result.get("execution"))
            persisted_matches = persisted_execution == expected_execution
        except Exception:
            persisted_matches = False
            logger.critical(
                "任务 accepted 已提交但 Repository 读回快照无法解码；"
                "仍返回已受理并唤醒 Dispatcher: task_id=%s task_type=%s",
                task_id,
                command.task_type,
                exc_info=True,
            )
        else:
            if not persisted_matches:
                logger.critical(
                    "任务 accepted 已提交但 Repository 读回快照与受理输入不一致；"
                    "仍以事务前已验证快照返回: task_id=%s task_type=%s",
                    task_id,
                    command.task_type,
                )
        return TaskSubmissionResult(outcome, expected_execution)

    def get_execution(
        self,
        task_id: TaskId,
    ) -> TaskExecutionSnapshot[TTaskInput] | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        raw = self._task_service.get_task_execution(task_id.value)
        return self._decode_execution(raw) if raw is not None else None

    def claim(self, task_id: TaskId) -> TaskClaimResult[TTaskInput]:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        raw_result = self._task_service.claim_task_execution(task_id.value)
        if not isinstance(raw_result, Mapping):
            raise LegacyTaskCommandAdapterError("领取返回值必须是 Mapping")
        outcome = self._claim_outcome(raw_result.get("outcome"))
        if outcome is TaskClaimOutcome.MISSING:
            if raw_result.get("execution") is not None:
                raise LegacyTaskCommandAdapterError("missing 领取结果不得携带 execution")
            return TaskClaimResult(outcome)
        execution = self._decode_execution(raw_result.get("execution"))
        if execution.task_id != task_id:
            raise LegacyTaskCommandAdapterError("领取结果 task_id 不一致")
        return TaskClaimResult(outcome, execution)

    def update_progress_if_current(self, update: ExpectedProgressUpdate) -> bool:
        if not isinstance(update, ExpectedProgressUpdate):
            raise TypeError("update 必须是 ExpectedProgressUpdate")
        self._ensure_business_ref(update.business_ref)
        return self._task_service.update_task_execution_progress_if_current(
            expected_execution_id=update.expected_task_id.value,
            business_type=update.business_ref.business_type,
            business_key=update.business_ref.business_key,
            progress=update.progress,
            message=update.message,
            execution_state=update.execution_state,
            public_status=update.public_status,
        )

    def finish_if_current(
        self,
        completion: ExpectedTaskCompletion[TTaskResult],
    ) -> bool:
        if not isinstance(completion, ExpectedTaskCompletion):
            raise TypeError("completion 必须是 ExpectedTaskCompletion")
        self._ensure_business_ref(completion.business_ref)
        encoded_result = self._codec.encode_result(completion.result)
        if not isinstance(encoded_result, EncodedTaskResult):
            raise TypeError("Codec.encode_result 必须返回 EncodedTaskResult")
        return self._task_service.finish_task_execution_if_current(
            expected_execution_id=completion.expected_task_id.value,
            business_type=completion.business_ref.business_type,
            business_key=completion.business_ref.business_key,
            execution_state=completion.execution_state,
            public_status=completion.public_status,
            message=completion.message,
            execution_result_payload=encoded_result.execution_result_payload,
            projection_result_payload=encoded_result.projection_result_payload,
        )

    def is_latest(self, task_id: TaskId, business_ref: TaskBusinessRef) -> bool:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        self._ensure_business_ref(business_ref)
        return self._task_service.is_task_execution_latest(
            execution_id=task_id.value,
            business_type=business_ref.business_type,
            business_key=business_ref.business_key,
        )

    def list_accepted(self, task_type: str, *, limit: int) -> tuple[TaskId, ...]:
        normalized_task_type = _required_text(task_type, name="task_type")
        if normalized_task_type != self._task_type:
            raise ValueError("task_type 与 Codec 业务类型不一致")
        return tuple(
            TaskId(item)
            for item in self._task_service.list_accepted_task_execution_ids(
                normalized_task_type,
                limit=limit,
            )
        )

    def defer_accepted(
        self,
        task_id: TaskId,
        *,
        retry_at: str,
        reason: str,
    ) -> bool:
        """持久化领取前故障的冷却时间，避免坏任务热循环并阻塞后续 FIFO 页。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        normalized_retry_at = _required_text(retry_at, name="retry_at")
        try:
            parsed = datetime.fromisoformat(normalized_retry_at)
        except ValueError as exc:
            raise ValueError("retry_at 必须是 ISO 时间") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("retry_at 必须包含时区")
        normalized_reason = _required_text(reason, name="reason")
        if len(normalized_reason) > 256:
            raise ValueError("reason 长度不能超过 256")
        return self._task_service.defer_accepted_task_execution(
            task_id.value,
            retry_at=parsed.astimezone(timezone.utc).isoformat(),
            reason=normalized_reason,
        )

    def inspect_queue(
        self,
        task_type: str,
        *,
        running_sample_limit: int,
    ) -> TaskQueueSnapshot:
        """把兼容 SQLite 的只读汇总映射为供应商无关队列快照。"""

        normalized_task_type = _required_text(task_type, name="task_type")
        if normalized_task_type != self._task_type:
            raise ValueError("task_type 与 Codec 业务类型不一致")
        raw = self._task_service.inspect_task_execution_queue(
            normalized_task_type,
            running_sample_limit=running_sample_limit,
        )
        if not isinstance(raw, Mapping):
            raise LegacyTaskCommandAdapterError("任务队列汇总必须是 Mapping")
        raw_running_ids = raw.get("running_execution_ids")
        if isinstance(raw_running_ids, (str, bytes, bytearray)) or not isinstance(
            raw_running_ids,
            (list, tuple),
        ):
            raise LegacyTaskCommandAdapterError(
                "任务队列 running_execution_ids 必须是序列"
            )
        try:
            return TaskQueueSnapshot(
                task_type=_required_text(
                    raw.get("business_type"),
                    name="business_type",
                ),
                accepted_count=raw.get("accepted_count"),  # type: ignore[arg-type]
                running_count=raw.get("running_count"),  # type: ignore[arg-type]
                oldest_accepted_at=raw.get("oldest_accepted_at"),
                oldest_running_at=raw.get("oldest_running_at"),
                running_task_ids=tuple(
                    TaskId(_required_text(item, name="running_execution_id"))
                    for item in raw_running_ids
                ),
            )
        except (TypeError, ValueError) as exc:
            raise LegacyTaskCommandAdapterError("任务队列汇总数据无效") from exc

    def _decode_execution(self, raw: object) -> TaskExecutionSnapshot[TTaskInput]:
        if not isinstance(raw, Mapping):
            raise LegacyTaskCommandAdapterError("execution 必须是 Mapping")
        business_type = _required_text(
            raw.get("business_type"),
            name="business_type",
        )
        if business_type != self._task_type:
            raise LegacyTaskCommandAdapterError("execution 属于其他业务类型")
        schema_version = raw.get("input_schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise LegacyTaskCommandAdapterError("input_schema_version 类型错误")
        payload = raw.get("input_payload")
        if not isinstance(payload, Mapping):
            raise LegacyTaskCommandAdapterError("input_payload 必须是 Mapping")
        input_snapshot = self._codec.decode_input(
            schema_version=schema_version,
            payload=payload,
        )
        execution = TaskExecutionSnapshot(
            task_id=TaskId(_required_text(raw.get("execution_id"), name="execution_id")),
            task_type=business_type,
            business_ref=TaskBusinessRef(
                business_type,
                _required_text(raw.get("business_key"), name="business_key"),
            ),
            execution_state=_required_text(
                raw.get("execution_state"),
                name="execution_state",
            ),
            public_status=_required_text(
                raw.get("public_status"),
                name="public_status",
            ),
            progress=raw.get("progress"),
            message=raw.get("message"),
            input_snapshot=input_snapshot,
            accepted_at=_required_text(raw.get("created_at"), name="created_at"),
            trace_id=_required_text(raw.get("trace_id"), name="trace_id"),
        )
        snapshot_task_id = getattr(input_snapshot, "task_id", execution.task_id.value)
        if snapshot_task_id != execution.task_id.value:
            raise LegacyTaskCommandAdapterError("input_snapshot 与 execution task_id 不一致")
        return execution

    def _ensure_business_ref(self, business_ref: TaskBusinessRef) -> None:
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if business_ref.business_type != self._task_type:
            raise ValueError("business_ref 与 Codec 业务类型不一致")

    @staticmethod
    def _submission_outcome(value: object) -> TaskSubmissionOutcome:
        if not isinstance(value, str):
            raise LegacyTaskCommandAdapterError("受理 outcome 必须是 str")
        try:
            return TaskSubmissionOutcome(value)
        except ValueError as error:
            raise LegacyTaskCommandAdapterError("受理 outcome 未知") from error

    @staticmethod
    def _claim_outcome(value: object) -> TaskClaimOutcome:
        if not isinstance(value, str):
            raise LegacyTaskCommandAdapterError("领取 outcome 必须是 str")
        try:
            return TaskClaimOutcome(value)
        except ValueError as error:
            raise LegacyTaskCommandAdapterError("领取 outcome 未知") from error


__all__ = [
    "EncodedTaskResult",
    "EncodedTaskSubmission",
    "LegacyTaskCommandAdapter",
    "LegacyTaskCommandAdapterError",
    "TaskCommandCodec",
]
