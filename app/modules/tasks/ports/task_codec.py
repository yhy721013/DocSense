"""业务 Task 输入与结果的稳定 Codec Port。

Codec 的具体实现仍归各业务 Adapter；通用 Task Adapter 只依赖本 Port，不能要求业务模块
反向导入基础设施层。DTO 保持供应商无关，并明确区分内部 execution 结果和公开 Callback 投影。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Mapping, Protocol, TypeVar

from app.modules.tasks.domain import TaskId

from .task_commands import TaskSubmissionCommand


TTaskSubmission = TypeVar("TTaskSubmission")
TTaskInput = TypeVar("TTaskInput")
TTaskResult = TypeVar("TTaskResult")


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


@dataclass(frozen=True, slots=True)
class EncodedTaskSubmission(Generic[TTaskInput]):
    """业务 Codec 交给通用 Task Adapter 的完整受理编码。"""

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
            _required_text(self.initial_public_status, name="initial_public_status"),
        )
        statuses = tuple(self.active_public_statuses)
        if not statuses:
            raise ValueError("active_public_statuses 不能为空")
        object.__setattr__(
            self,
            "active_public_statuses",
            tuple(
                _required_text(item, name="active_public_status")
                for item in statuses
            ),
        )


@dataclass(frozen=True, slots=True)
class EncodedTaskResult:
    """分别保存内部 execution 结果与既有公开 Callback 投影。"""

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
    """业务 DTO 与通用、供应商无关持久载荷之间的稳定边界。"""

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


__all__ = ["EncodedTaskResult", "EncodedTaskSubmission", "TaskCommandCodec"]
