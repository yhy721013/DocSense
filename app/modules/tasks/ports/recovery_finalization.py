"""Recovery 终态结果预检与 Callback eligibility 的内部契约。

本端口只服务阶段 2-7 的 ``finalize_from_checkpoint``。业务结果 Store 必须在 Recovery
Unit of Work 的同一数据库事务中完成只读核验；任何文件、网络或模型 I/O 都必须在进入
该事务前形成脱敏 Observation，不能藏在预检实现里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import (
    RecoveryAuthority,
    TaskBusinessRef,
    TaskId,
    TaskRecoveryDecision,
)

from .clock import require_persisted_utc
from .task_recovery import TaskRecoverySnapshot


@dataclass(frozen=True, slots=True)
class RecoveryCallbackEligibilityCommand:
    """Recovery Decision 提交终态后登记 Callback 可投递事实。

    ``RecoveryAuthority``、Decision 身份和来源 Checkpoint 必须全部显式携带。Callback
    Store 只能复核这些已经持久化的事实，禁止从“当前 Task 是终态”反向补造恢复权限。
    """

    authority: RecoveryAuthority = field(repr=False)
    decision_id: str
    task_id: TaskId
    business_ref: TaskBusinessRef
    source_step_key: str
    source_step_attempt_no: int
    checkpoint_code: str
    checkpoint_digest: str
    eligible_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, RecoveryAuthority):
            raise TypeError("authority 必须是 RecoveryAuthority")
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        for name in ("decision_id", "source_step_key", "checkpoint_code"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空 str")
            object.__setattr__(self, name, value.strip())
        if type(self.source_step_attempt_no) is not int or self.source_step_attempt_no <= 0:
            raise ValueError("source_step_attempt_no 必须是正整数")
        digest = self.checkpoint_digest.strip().lower() if isinstance(
            self.checkpoint_digest, str
        ) else ""
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("checkpoint_digest 必须是 SHA-256 hex")
        object.__setattr__(self, "checkpoint_digest", digest)
        object.__setattr__(
            self,
            "eligible_at",
            require_persisted_utc(self.eligible_at, name="eligible_at"),
        )


@runtime_checkable
class TaskRecoveryFinalizationPreflightPort(Protocol):
    """在 Recovery 写事务的一致性视图中核验业务结果快照。"""

    def verify(
        self,
        snapshot: TaskRecoverySnapshot,
        decision: TaskRecoveryDecision,
    ) -> bool:
        ...


__all__ = [
    "RecoveryCallbackEligibilityCommand",
    "TaskRecoveryFinalizationPreflightPort",
]
