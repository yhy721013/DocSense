"""按冻结 task_type 路由业务续跑预检。"""

from __future__ import annotations

from collections.abc import Mapping

from app.modules.tasks.domain import TaskRecoveryDecision
from app.modules.tasks.ports import TaskRecoverySnapshot
from app.modules.tasks.ports.recovery_resume import TaskRecoveryResumePreflightPort


class RoutedTaskRecoveryResumePreflight:
    def __init__(self, verifiers: Mapping[str, TaskRecoveryResumePreflightPort]) -> None:
        normalized = dict(verifiers)
        if set(normalized) != {"report", "weaponry", "file"} or any(
            not isinstance(value, TaskRecoveryResumePreflightPort)
            for value in normalized.values()
        ):
            raise ValueError("Recovery 续跑预检必须精确覆盖 report/weaponry/file")
        self._verifiers = normalized

    def verify(
        self,
        snapshot: TaskRecoverySnapshot,
        decision: TaskRecoveryDecision,
    ) -> bool:
        verifier = self._verifiers.get(snapshot.task.task_type)
        return bool(verifier is not None and verifier.verify(snapshot, decision))


__all__ = ["RoutedTaskRecoveryResumePreflight"]
