"""任务写入、领取和 expected-task-id 条件更新端口。

本文件只定义通用任务控制面语义，不知道报告、武器谱或文件分析的字段。业务命令和
输入快照通过泛型参数传递；具体 Adapter 负责序列化，但不能在 Repository 内执行下载、
RAG、回调、Progress 通知或线程创建。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, Protocol, TypeVar, runtime_checkable

from app.modules.tasks.domain.models import (
    TaskBusinessRef,
    TaskExecutionSnapshot,
    TaskId,
)


TTaskSubmission = TypeVar("TTaskSubmission")
TTaskInput = TypeVar("TTaskInput")
TTaskResult = TypeVar("TTaskResult")


def _required_text(value: object, *, name: str) -> str:
    """严格校验内部文本，不把数字或对象静默字符串化。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _progress(value: object) -> float:
    """校验任务写入使用的 0～1 进度比例。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("progress 必须是数字")
    normalized = float(value)
    if (
        normalized != normalized
        or normalized in (float("inf"), float("-inf"))
        or normalized < 0.0
        or normalized > 1.0
    ):
        raise ValueError("progress 必须是 0 到 1 之间的有限数字")
    return normalized


class TaskSubmissionOutcome(str, Enum):
    """原子受理的内部分类，不直接等同于某个 HTTP 状态码。"""

    ACCEPTED = "accepted"
    ACTIVE_CONFLICT = "active_conflict"
    CALLBACK_SENDING = "callback_sending"
    CALLBACK_OUTCOME_UNKNOWN = "callback_outcome_unknown"


class TaskClaimOutcome(str, Enum):
    """Worker 条件领取一次执行的结果。"""

    CLAIMED = "claimed"
    MISSING = "missing"
    ALREADY_RUNNING = "already_running"
    TERMINAL = "terminal"
    STALE = "stale"


@dataclass(frozen=True)
class TaskSubmissionCommand(Generic[TTaskSubmission]):
    """业务 Application 交给原子受理端口的不可变命令。"""

    task_type: str
    business_ref: TaskBusinessRef
    input_schema_version: int
    submission: TTaskSubmission
    trace_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_type",
            _required_text(self.task_type, name="task_type"),
        )
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if (
            isinstance(self.input_schema_version, bool)
            or not isinstance(self.input_schema_version, int)
            or self.input_schema_version <= 0
        ):
            raise ValueError("input_schema_version 必须是正整数")
        if self.submission is None:
            raise ValueError("submission 不能为空")
        object.__setattr__(
            self,
            "trace_id",
            _required_text(self.trace_id, name="trace_id"),
        )


@dataclass(frozen=True)
class TaskSubmissionResult(Generic[TTaskInput]):
    """原子受理结果；只有 ``accepted`` 可以携带新执行快照。"""

    outcome: TaskSubmissionOutcome
    execution: TaskExecutionSnapshot[TTaskInput] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, TaskSubmissionOutcome):
            raise TypeError("outcome 必须是 TaskSubmissionOutcome")
        if self.outcome is TaskSubmissionOutcome.ACCEPTED:
            if not isinstance(self.execution, TaskExecutionSnapshot):
                raise TypeError("accepted 结果必须包含 TaskExecutionSnapshot")
        elif self.execution is not None:
            raise ValueError("冲突结果不得携带新执行快照")


@dataclass(frozen=True)
class TaskClaimResult(Generic[TTaskInput]):
    """条件领取结果；除 ``missing`` 外均返回被判断的执行快照。"""

    outcome: TaskClaimOutcome
    execution: TaskExecutionSnapshot[TTaskInput] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, TaskClaimOutcome):
            raise TypeError("outcome 必须是 TaskClaimOutcome")
        if self.outcome is TaskClaimOutcome.MISSING:
            if self.execution is not None:
                raise ValueError("missing 结果不得携带执行快照")
        elif not isinstance(self.execution, TaskExecutionSnapshot):
            raise TypeError("非 missing 领取结果必须包含执行快照")


@dataclass(frozen=True)
class ExpectedProgressUpdate:
    """只允许当前 latest execution 写入的进度命令。"""

    expected_task_id: TaskId
    business_ref: TaskBusinessRef
    progress: float
    message: str
    execution_state: str
    public_status: str

    def __post_init__(self) -> None:
        if not isinstance(self.expected_task_id, TaskId):
            raise TypeError("expected_task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        object.__setattr__(self, "progress", _progress(self.progress))
        if not isinstance(self.message, str):
            raise TypeError("message 必须是 str")
        object.__setattr__(
            self,
            "execution_state",
            _required_text(self.execution_state, name="execution_state"),
        )
        object.__setattr__(
            self,
            "public_status",
            _required_text(self.public_status, name="public_status"),
        )


@dataclass(frozen=True)
class ExpectedTaskCompletion(Generic[TTaskResult]):
    """按 expected TaskId 提交业务终态和公开投影的命令。"""

    expected_task_id: TaskId
    business_ref: TaskBusinessRef
    execution_state: str
    public_status: str
    message: str
    result: TTaskResult

    def __post_init__(self) -> None:
        if not isinstance(self.expected_task_id, TaskId):
            raise TypeError("expected_task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        object.__setattr__(
            self,
            "execution_state",
            _required_text(self.execution_state, name="execution_state"),
        )
        object.__setattr__(
            self,
            "public_status",
            _required_text(self.public_status, name="public_status"),
        )
        if not isinstance(self.message, str):
            raise TypeError("message 必须是 str")
        if self.result is None:
            raise ValueError("result 不能为空")


@runtime_checkable
class TaskCommandPort(
    Protocol,
    Generic[TTaskSubmission, TTaskInput, TTaskResult],
):
    """任务事实、输入快照和 latest 投影的统一写端口。

    ``False`` 条件写是预期并发结果，不是基础设施异常。实现必须在同一事务中完成
    create-if-allowed，且 Repository 方法不得私自触发任何外部副作用。
    """

    def create_if_allowed(
        self,
        command: TaskSubmissionCommand[TTaskSubmission],
    ) -> TaskSubmissionResult[TTaskInput]:
        ...

    def get_execution(
        self,
        task_id: TaskId,
    ) -> TaskExecutionSnapshot[TTaskInput] | None:
        ...

    def claim(self, task_id: TaskId) -> TaskClaimResult[TTaskInput]:
        ...

    def update_progress_if_current(self, update: ExpectedProgressUpdate) -> bool:
        ...

    def finish_if_current(
        self,
        completion: ExpectedTaskCompletion[TTaskResult],
    ) -> bool:
        ...

    def is_latest(self, task_id: TaskId, business_ref: TaskBusinessRef) -> bool:
        ...

    def list_accepted(self, task_type: str, *, limit: int) -> tuple[TaskId, ...]:
        ...

    def defer_accepted(
        self,
        task_id: TaskId,
        *,
        retry_at: str,
        reason: str,
    ) -> bool:
        """仅当任务仍为 accepted 时持久化下次可领取时间。

        返回 ``False`` 表示任务已被其他执行者领取或已进入终态。实现不得把该结果解释
        为错误，也不得借此把 ``running`` 回退为 ``accepted``。
        """
        ...


__all__ = [
    "ExpectedProgressUpdate",
    "ExpectedTaskCompletion",
    "TaskClaimOutcome",
    "TaskClaimResult",
    "TaskCommandPort",
    "TaskSubmissionCommand",
    "TaskSubmissionOutcome",
    "TaskSubmissionResult",
]
