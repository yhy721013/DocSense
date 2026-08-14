"""统一 Task 单项/批量原子受理 Port。

本 Port 只表达控制面事实，不映射 HTTP 状态码，也不发送唤醒、Callback 或消息。
批量方法用于 Analysis：实现必须让整批全部受理或全部不落盘，禁止部分成功。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, Mapping, Protocol, TypeVar, runtime_checkable

from app.modules.tasks.domain import TaskBatchRef, TaskBusinessRef, TaskId, TaskRecord

from .clock import require_persisted_utc


TTaskInput = TypeVar("TTaskInput")


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


class TaskAdmissionOutcome(str, Enum):
    """内部受理分类；Presenter 决定其既有公开状态映射。"""

    ACCEPTED = "accepted"
    ACTIVE_TASK_CONFLICT = "active_task_conflict"
    CALLBACK_SENDING = "callback_sending"
    CALLBACK_OUTCOME_UNKNOWN = "callback_outcome_unknown"
    BATCH_REJECTED = "batch_rejected"


@dataclass(frozen=True, slots=True)
class TaskAdmissionRequest(Generic[TTaskInput]):
    """已经完成业务校验、可直接进入同一 Admission UoW 的冻结输入。"""

    task_id: TaskId
    task_type: str
    business_ref: TaskBusinessRef
    input_schema_version: int
    input_snapshot: TTaskInput
    input_payload: Mapping[str, Any]
    public_request_payload: Mapping[str, Any]
    initial_public_status: str
    trace_id: str
    accepted_at: str
    batch: TaskBatchRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if type(self.input_schema_version) is not int or self.input_schema_version <= 0:
            raise ValueError("input_schema_version 必须是正整数")
        if self.input_snapshot is None:
            raise ValueError("input_snapshot 不能为空")
        if self.batch is not None and not isinstance(self.batch, TaskBatchRef):
            raise TypeError("batch 必须是 TaskBatchRef 或 None")
        if not isinstance(self.input_payload, Mapping):
            raise TypeError("input_payload 必须是 Mapping")
        if not isinstance(self.public_request_payload, Mapping):
            raise TypeError("public_request_payload 必须是 Mapping")
        for name in ("task_type", "initial_public_status", "trace_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name=name))
        if self.task_type != self.business_ref.business_type:
            raise ValueError("task_type 必须与 business_ref.business_type 完全一致")
        has_batch = self.batch is not None
        if (self.task_type == "file") != has_batch:
            raise ValueError("阶段 2 的 file/Analysis Task 必须且只能携带批次身份")
        object.__setattr__(
            self,
            "accepted_at",
            require_persisted_utc(self.accepted_at, name="accepted_at"),
        )


@dataclass(frozen=True, slots=True)
class TaskAdmissionResult:
    """受理结果；只有 accepted 可以携带新建 TaskRecord。"""

    task_id: TaskId
    business_ref: TaskBusinessRef
    outcome: TaskAdmissionOutcome
    task: TaskRecord | None = None
    reason_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if not isinstance(self.outcome, TaskAdmissionOutcome):
            raise TypeError("outcome 必须是 TaskAdmissionOutcome")
        if self.outcome is TaskAdmissionOutcome.ACCEPTED:
            if not isinstance(self.task, TaskRecord):
                raise TypeError("accepted 结果必须包含 TaskRecord")
            if self.task.task_id != self.task_id or self.task.business_ref != self.business_ref:
                raise ValueError("受理结果携带的 TaskRecord 身份不一致")
        elif self.task is not None:
            raise ValueError("未受理结果不得携带 TaskRecord")
        if not isinstance(self.reason_code, str):
            raise TypeError("reason_code 必须是 str")


def validate_task_admission_batch(
    requests: tuple[TaskAdmissionRequest[Any], ...],
) -> None:
    """校验批量受理的结构不变量，不读取数据库也不修改输入。

    Analysis 的每个文件仍是独立 Task，因此批次身份必须在进入 Store 前已经完整、同批且与
    请求顺序一致。把规则放在 Port 层的纯函数中，可以让 SQLite Store、严格 Fake 和未来队列
    Adapter 复用同一契约，避免各实现临场猜测 ``batch_id`` 或偷偷重排请求。

    Report/Weaponry 当前没有请求内顺序语义，允许调用批量原语做原子性测试，但不得与 file
    Task 混入同一批次。
    """

    if not isinstance(requests, tuple):
        raise TypeError("requests 必须是 tuple")
    if not requests:
        raise ValueError("批量受理不能为空")
    if any(not isinstance(item, TaskAdmissionRequest) for item in requests):
        raise TypeError("requests 只能包含 TaskAdmissionRequest")

    file_requests = tuple(item for item in requests if item.task_type == "file")
    if not file_requests:
        return
    if len(file_requests) != len(requests):
        raise ValueError("file/Analysis Task 不得与其他业务类型混入同一批量受理")

    batch_ids = {item.batch.batch_id for item in file_requests if item.batch is not None}
    if len(batch_ids) != 1:
        raise ValueError("同一 Analysis 批量受理必须使用唯一 batch_id")
    actual_sequences = tuple(
        item.batch.sequence for item in file_requests if item.batch is not None
    )
    expected_sequences = tuple(range(1, len(file_requests) + 1))
    if actual_sequences != expected_sequences:
        raise ValueError("Analysis batch_sequence 必须从 1 开始、连续且与请求顺序一致")


@runtime_checkable
class TaskAdmissionPort(Protocol):
    """在调用方提供的窄 UoW 内写入受理事实。"""

    def admit_one(self, request: TaskAdmissionRequest[Any]) -> TaskAdmissionResult:
        ...

    def admit_many(
        self,
        requests: tuple[TaskAdmissionRequest[Any], ...],
    ) -> tuple[TaskAdmissionResult, ...]:
        """整批原子受理；返回值必须与输入等长、同序。"""
        ...


__all__ = [
    "TaskAdmissionOutcome",
    "TaskAdmissionPort",
    "TaskAdmissionRequest",
    "TaskAdmissionResult",
    "validate_task_admission_batch",
]
