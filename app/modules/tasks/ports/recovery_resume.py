"""Recovery retry_authorized 的业务续跑能力预检契约。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskRecoveryDecision
from .task_recovery import TaskRecoverySnapshot


@runtime_checkable
class TaskRecoveryResumePreflightPort(Protocol):
    """在 Decision 同一事务中证明目标 Step 存在真实解析器和原始快照。"""

    def verify(
        self,
        snapshot: TaskRecoverySnapshot,
        decision: TaskRecoveryDecision,
    ) -> bool: ...


__all__ = ["TaskRecoveryResumePreflightPort"]
