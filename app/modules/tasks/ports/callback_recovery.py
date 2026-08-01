"""check-task 显式回调恢复端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain.models import (
    CALLBACK_STATUSES,
    CALLBACK_SUCCESS,
    TaskId,
    TaskSnapshot,
)


DELIVERY_OUTCOME_UNKNOWN = "delivery_outcome_unknown"


def _required_status(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("final_status 必须是 str")
    normalized = value.strip()
    if normalized not in CALLBACK_STATUSES:
        raise ValueError("final_status 不是受支持的 callback 状态")
    return normalized


@dataclass(frozen=True)
class CallbackRecoveryResult:
    """一次由 check-task 明确触发的恢复结果。

    ``attempted`` 表示发生了外部投递尝试，``replayed`` 只表示本次补发已确认成功。
    ``delivery_outcome`` 是内部审计信息，可记录 ``delivery_outcome_unknown``，绝不
    进入当前 check-task 成功响应。
    """

    attempted: bool
    replayed: bool
    final_status: str
    delivery_outcome: str = ""
    current_snapshot: TaskSnapshot | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.attempted, bool):
            raise TypeError("attempted 必须是 bool")
        if not isinstance(self.replayed, bool):
            raise TypeError("replayed 必须是 bool")
        final_status = _required_status(self.final_status)
        if self.replayed and not self.attempted:
            raise ValueError("replayed=True 时 attempted 必须为 True")
        if self.replayed and final_status != CALLBACK_SUCCESS:
            raise ValueError("replayed=True 时 final_status 必须为 success")
        if not isinstance(self.delivery_outcome, str):
            raise TypeError("delivery_outcome 必须是 str")
        if self.current_snapshot is not None:
            if not isinstance(self.current_snapshot, TaskSnapshot):
                raise TypeError("current_snapshot 必须是 TaskSnapshot 或 None")
            if self.current_snapshot.callback_status != final_status:
                raise ValueError("current_snapshot 与 final_status 不一致")
        object.__setattr__(self, "final_status", final_status)
        object.__setattr__(self, "delivery_outcome", self.delivery_outcome.strip())


@runtime_checkable
class CallbackRecoveryPort(Protocol):
    """按 TaskId 检查并执行一次显式回调恢复的能力边界。"""

    def recover_if_needed(self, task_id: TaskId) -> CallbackRecoveryResult:
        """恢复指定执行的回调；异常必须由应用层感知，不能伪装成功。"""
        ...


__all__ = [
    "CallbackRecoveryPort",
    "CallbackRecoveryResult",
    "DELIVERY_OUTCOME_UNKNOWN",
]
