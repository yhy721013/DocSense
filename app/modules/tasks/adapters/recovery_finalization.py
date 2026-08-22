"""按冻结 task_type 路由 Recovery 终态业务预检。"""

from __future__ import annotations

from collections.abc import Mapping

from app.modules.tasks.domain import TaskRecoveryDecision
from app.modules.tasks.ports import (
    TaskRecoveryFinalizationPreflightPort,
    TaskRecoverySnapshot,
)


class RoutedTaskRecoveryFinalizationPreflight:
    """显式覆盖三业务的严格路由；缺失、别名和默认回退均被禁止。"""

    def __init__(
        self,
        verifiers: Mapping[str, TaskRecoveryFinalizationPreflightPort],
    ) -> None:
        normalized = dict(verifiers)
        if set(normalized) != {"report", "weaponry", "file"} or any(
            not isinstance(value, TaskRecoveryFinalizationPreflightPort)
            for value in normalized.values()
        ):
            raise ValueError("Recovery 终态预检必须精确覆盖 report/weaponry/file")
        self._verifiers = normalized

    def verify(
        self,
        snapshot: TaskRecoverySnapshot,
        decision: TaskRecoveryDecision,
    ) -> bool:
        if not isinstance(snapshot, TaskRecoverySnapshot):
            raise TypeError("snapshot 必须是 TaskRecoverySnapshot")
        if not isinstance(decision, TaskRecoveryDecision):
            raise TypeError("decision 必须是 TaskRecoveryDecision")
        verifier = self._verifiers.get(snapshot.task.task_type)
        return bool(verifier is not None and verifier.verify(snapshot, decision))


__all__ = ["RoutedTaskRecoveryFinalizationPreflight"]
