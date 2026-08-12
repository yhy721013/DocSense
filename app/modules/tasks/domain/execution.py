"""统一 Task 执行权、步骤、检查点和内部事件的不可变领域对象。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .models import TaskBusinessRef, TaskId
from .states import (
    StepEffectKind,
    StepReplayPolicy,
    TaskAttemptState,
    TaskState,
    TaskStepState,
)


_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
MAX_STABLE_TEXT = 512
MAX_REASON_CODE = 128
MAX_EVENT_METADATA_ITEMS = 32


def _required_text(value: object, *, name: str, maximum: int = MAX_STABLE_TEXT) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{name} 长度不能超过 {maximum}")
    return normalized


def _optional_text(value: object, *, name: str, maximum: int = MAX_STABLE_TEXT) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    if len(value) > maximum:
        raise ValueError(f"{name} 长度不能超过 {maximum}")
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return value


def _non_negative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return value


def _utc_timestamp(value: object, *, name: str) -> str:
    normalized = _required_text(value, name=name, maximum=32)
    if _UTC_TIMESTAMP.fullmatch(normalized) is None:
        raise ValueError(f"{name} 必须是 UTC RFC3339 微秒格式并以 Z 结尾")
    return normalized


def _optional_utc_timestamp(value: object, *, name: str) -> str:
    if value == "":
        return ""
    return _utc_timestamp(value, name=name)


def _sha256(value: object, *, name: str, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    normalized = _required_text(value, name=name, maximum=64).casefold()
    if _SHA256.fullmatch(normalized) is None:
        raise ValueError(f"{name} 必须是 SHA-256 十六进制摘要")
    return normalized


def _frozen_metadata_items(
    value: tuple[tuple[str, str | int | bool], ...],
) -> tuple[tuple[str, str | int | bool], ...]:
    """严格校验已冻结条目，不能先转 dict 后悄悄吞掉重复键。"""

    if not isinstance(value, tuple):
        raise TypeError("metadata 条目必须是 tuple")
    if len(value) > MAX_EVENT_METADATA_ITEMS:
        raise ValueError("metadata 项数超过上限")
    result: list[tuple[str, str | int | bool]] = []
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("metadata 每项必须是二元 tuple")
        raw_key, raw_value = item
        key = _required_text(raw_key, name="metadata.key", maximum=64)
        if isinstance(raw_value, bool):
            normalized: str | int | bool = raw_value
        elif type(raw_value) is int:
            normalized = raw_value
        elif isinstance(raw_value, str):
            normalized = _optional_text(raw_value, name=f"metadata.{key}", maximum=256)
        else:
            raise TypeError("metadata 值只能是 str/int/bool")
        result.append((key, normalized))
    result.sort(key=lambda item: item[0])
    if len({item[0] for item in result}) != len(result):
        raise ValueError("metadata 键规范化后不得重复")
    return tuple(result)


def _frozen_metadata(value: Mapping[str, object]) -> tuple[tuple[str, str | int | bool], ...]:
    """冻结小型脱敏元数据；禁止嵌套对象和任意字符串化。"""

    if not isinstance(value, Mapping):
        raise TypeError("metadata 必须是 Mapping")
    return _frozen_metadata_items(tuple(value.items()))


@dataclass(frozen=True, slots=True)
class TaskBatchRef:
    """可选批次中的稳定位置；当前仅 Analysis/file 任务使用。"""

    batch_id: str
    sequence: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "batch_id",
            _required_text(self.batch_id, name="batch_id", maximum=256),
        )
        object.__setattr__(
            self,
            "sequence",
            _positive_int(self.sequence, name="batch_sequence"),
        )


@dataclass(frozen=True, slots=True)
class TaskOwnerIdentity:
    """一次进程启动下的 Worker 诊断身份，不构成写权限。

    持久层同时保存组合 ``owner_id`` 与四个拆分字段，禁止 Store 临时解析任意字符串。
    真正写权限仍由 attempt、lease token、fencing token 和租约共同组成。
    """

    instance_start_id: str
    process_id: int
    executor_name: str
    worker_slot: str

    def __post_init__(self) -> None:
        instance_start_id = _required_text(
            self.instance_start_id,
            name="instance_start_id",
            maximum=36,
        )
        if _CANONICAL_UUID.fullmatch(instance_start_id) is None:
            raise ValueError("instance_start_id 必须是规范小写 UUID")
        object.__setattr__(self, "instance_start_id", instance_start_id)
        object.__setattr__(
            self,
            "process_id",
            _non_negative_int(self.process_id, name="process_id"),
        )
        for name in ("executor_name", "worker_slot"):
            normalized = _required_text(getattr(self, name), name=name, maximum=128)
            if "/" in normalized:
                raise ValueError(f"{name} 不得包含 /，避免 owner_id 产生歧义")
            object.__setattr__(self, name, normalized)

    @property
    def owner_id(self) -> str:
        return (
            f"{self.instance_start_id}/{self.process_id}/"
            f"{self.executor_name}/{self.worker_slot}"
        )


@dataclass(frozen=True, slots=True)
class TaskExecutionAuthority:
    """一次 Worker 写入 Task 的完整能力；owner_id 仅供诊断。"""

    task_id: TaskId
    attempt_no: int
    owner_id: str
    lease_token: str
    fencing_token: int
    lease_expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(self, "attempt_no", _positive_int(self.attempt_no, name="attempt_no"))
        object.__setattr__(self, "owner_id", _required_text(self.owner_id, name="owner_id"))
        object.__setattr__(
            self,
            "lease_token",
            _required_text(self.lease_token, name="lease_token", maximum=256),
        )
        object.__setattr__(
            self,
            "fencing_token",
            _positive_int(self.fencing_token, name="fencing_token"),
        )
        object.__setattr__(
            self,
            "lease_expires_at",
            _utc_timestamp(self.lease_expires_at, name="lease_expires_at"),
        )


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """Task 当前权威投影；公开状态由业务 Presenter 另行维护。"""

    task_id: TaskId
    task_type: str
    business_ref: TaskBusinessRef
    state: TaskState
    current_attempt_no: int
    fencing_token: int
    row_version: int
    recovery_generation: int
    current_recovery_case_id: str = ""
    recovery_reason_code: str = ""
    retry_from_step_key: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if not isinstance(self.state, TaskState):
            raise TypeError("state 必须是 TaskState")
        object.__setattr__(self, "task_type", _required_text(self.task_type, name="task_type", maximum=64))
        for name in ("current_attempt_no", "fencing_token", "row_version", "recovery_generation"):
            object.__setattr__(self, name, _non_negative_int(getattr(self, name), name=name))
        for name in ("current_recovery_case_id", "retry_from_step_key"):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name=name))
        object.__setattr__(
            self,
            "recovery_reason_code",
            _optional_text(self.recovery_reason_code, name="recovery_reason_code", maximum=MAX_REASON_CODE),
        )
        has_case = bool(self.current_recovery_case_id)
        if (self.state is TaskState.RECOVERY_REQUIRED) != has_case:
            raise ValueError("recovery_required 与 current_recovery_case_id 必须同时存在")
        if self.recovery_generation == 0 and has_case:
            raise ValueError("Recovery Case 存在时 generation 必须为正整数")


@dataclass(frozen=True, slots=True)
class TaskAttempt:
    """一次整任务执行权的追加事实。"""

    authority: TaskExecutionAuthority
    state: TaskAttemptState
    claimed_at: str
    heartbeat_at: str
    started_at: str = ""
    completed_at: str = ""
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.authority, TaskExecutionAuthority):
            raise TypeError("authority 必须是 TaskExecutionAuthority")
        if not isinstance(self.state, TaskAttemptState):
            raise TypeError("state 必须是 TaskAttemptState")
        object.__setattr__(self, "claimed_at", _utc_timestamp(self.claimed_at, name="claimed_at"))
        object.__setattr__(self, "heartbeat_at", _utc_timestamp(self.heartbeat_at, name="heartbeat_at"))
        object.__setattr__(self, "started_at", _optional_utc_timestamp(self.started_at, name="started_at"))
        object.__setattr__(self, "completed_at", _optional_utc_timestamp(self.completed_at, name="completed_at"))
        object.__setattr__(self, "error_code", _optional_text(self.error_code, name="error_code", maximum=MAX_REASON_CODE))


@dataclass(frozen=True, slots=True)
class TaskExecutionContext:
    """业务 Workflow 可见的执行上下文；不暴露数据库或线程实现。"""

    authority: TaskExecutionAuthority
    task_type: str
    trace_id: str
    input_profile_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, TaskExecutionAuthority):
            raise TypeError("authority 必须是 TaskExecutionAuthority")
        object.__setattr__(self, "task_type", _required_text(self.task_type, name="task_type", maximum=64))
        object.__setattr__(self, "trace_id", _required_text(self.trace_id, name="trace_id"))
        object.__setattr__(
            self,
            "input_profile_fingerprint",
            _sha256(self.input_profile_fingerprint, name="input_profile_fingerprint"),
        )


@dataclass(frozen=True, slots=True)
class TaskStepCheckpoint:
    """不含正文的 Step 结果检查点。"""

    code: str
    result_ref: str = ""
    result_digest: str = ""
    external_ref: str = ""
    observation_ref: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _required_text(self.code, name="checkpoint.code", maximum=128))
        for name in ("result_ref", "external_ref", "observation_ref"):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name=name))
        object.__setattr__(self, "result_digest", _sha256(self.result_digest, name="result_digest", optional=True))


@dataclass(frozen=True, slots=True)
class TaskStep:
    """稳定 step_key 的当前投影。"""

    task_id: TaskId
    step_key: str
    definition_version: int
    effect_kind: StepEffectKind
    replay_policy: StepReplayPolicy
    state: TaskStepState
    current_step_attempt_no: int
    idempotency_key: str
    checkpoint: TaskStepCheckpoint | None
    row_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(self, "step_key", _required_text(self.step_key, name="step_key"))
        object.__setattr__(self, "definition_version", _positive_int(self.definition_version, name="definition_version"))
        if not isinstance(self.effect_kind, StepEffectKind):
            raise TypeError("effect_kind 必须是 StepEffectKind")
        if not isinstance(self.replay_policy, StepReplayPolicy):
            raise TypeError("replay_policy 必须是 StepReplayPolicy")
        if not isinstance(self.state, TaskStepState):
            raise TypeError("state 必须是 TaskStepState")
        object.__setattr__(self, "current_step_attempt_no", _non_negative_int(self.current_step_attempt_no, name="current_step_attempt_no"))
        object.__setattr__(self, "idempotency_key", _required_text(self.idempotency_key, name="idempotency_key"))
        if self.checkpoint is not None and not isinstance(self.checkpoint, TaskStepCheckpoint):
            raise TypeError("checkpoint 必须是 TaskStepCheckpoint 或 None")
        object.__setattr__(self, "row_version", _non_negative_int(self.row_version, name="row_version"))
        if self.effect_kind is StepEffectKind.EXTERNAL_WRITE and self.replay_policy is StepReplayPolicy.SAFE:
            raise ValueError("external_write 不能声明为无条件 safe 重放")


@dataclass(frozen=True, slots=True)
class TaskStepAttempt:
    """某个 Step 的一次追加执行历史；结果一经写入不可覆盖。"""

    task_id: TaskId
    step_key: str
    step_attempt_no: int
    task_attempt_no: int
    fencing_token: int
    state: TaskStepState
    idempotency_key: str
    intent_at: str
    result_at: str = ""
    checkpoint: TaskStepCheckpoint | None = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(self, "step_key", _required_text(self.step_key, name="step_key"))
        for name in ("step_attempt_no", "task_attempt_no", "fencing_token"):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name=name))
        if not isinstance(self.state, TaskStepState):
            raise TypeError("state 必须是 TaskStepState")
        if self.state is TaskStepState.PENDING:
            raise ValueError("Step Attempt 保存 intent 后至少必须处于 running")
        object.__setattr__(self, "idempotency_key", _required_text(self.idempotency_key, name="idempotency_key"))
        object.__setattr__(self, "intent_at", _utc_timestamp(self.intent_at, name="intent_at"))
        object.__setattr__(self, "result_at", _optional_utc_timestamp(self.result_at, name="result_at"))
        if self.checkpoint is not None and not isinstance(self.checkpoint, TaskStepCheckpoint):
            raise TypeError("checkpoint 必须是 TaskStepCheckpoint 或 None")
        object.__setattr__(self, "error_code", _optional_text(self.error_code, name="error_code", maximum=MAX_REASON_CODE))
        is_running = self.state is TaskStepState.RUNNING
        if is_running == bool(self.result_at):
            raise ValueError("running 不得有 result_at，结果状态必须有 result_at")


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """同 Task 状态变更一起追加的有界内部诊断事件。"""

    task_id: TaskId
    sequence_no: int
    event_type: str
    trace_id: str
    created_at: str
    attempt_no: int = 0
    step_key: str = ""
    reason_code: str = ""
    metadata: tuple[tuple[str, str | int | bool], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(self, "sequence_no", _positive_int(self.sequence_no, name="sequence_no"))
        object.__setattr__(self, "event_type", _required_text(self.event_type, name="event_type", maximum=128))
        object.__setattr__(self, "trace_id", _required_text(self.trace_id, name="trace_id"))
        object.__setattr__(self, "created_at", _utc_timestamp(self.created_at, name="created_at"))
        object.__setattr__(self, "attempt_no", _non_negative_int(self.attempt_no, name="attempt_no"))
        object.__setattr__(self, "step_key", _optional_text(self.step_key, name="step_key"))
        object.__setattr__(self, "reason_code", _optional_text(self.reason_code, name="reason_code", maximum=MAX_REASON_CODE))
        object.__setattr__(self, "metadata", _frozen_metadata_items(self.metadata))

    @classmethod
    def create(
        cls,
        *,
        task_id: TaskId,
        sequence_no: int,
        event_type: str,
        trace_id: str,
        created_at: str,
        attempt_no: int = 0,
        step_key: str = "",
        reason_code: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "TaskEvent":
        return cls(
            task_id=task_id,
            sequence_no=sequence_no,
            event_type=event_type,
            trace_id=trace_id,
            created_at=created_at,
            attempt_no=attempt_no,
            step_key=step_key,
            reason_code=reason_code,
            metadata=_frozen_metadata(metadata or {}),
        )


__all__ = [
    "TaskBatchRef",
    "TaskAttempt",
    "TaskEvent",
    "TaskExecutionAuthority",
    "TaskExecutionContext",
    "TaskOwnerIdentity",
    "TaskRecord",
    "TaskStep",
    "TaskStepAttempt",
    "TaskStepCheckpoint",
]
