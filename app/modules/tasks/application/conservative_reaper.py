"""阶段 2-3 初始保守 Reaper：只观察并延后，不自动重试业务副作用。"""

from __future__ import annotations

import logging

from app.modules.tasks.domain import RecoveryClassification, add_persisted_utc_seconds
from app.modules.tasks.ports import (
    ClockPort,
    TaskControlQueryUnitOfWorkFactory,
    TaskRecoveryClassificationCommand,
    TaskRecoveryMutationOutcome,
    TaskRecoveryUnitOfWorkFactory,
)


logger = logging.getLogger(__name__)


class ConservativeTaskReaper:
    """持久化 DEFER 事实，明确禁止仅凭 lease 过期把 Task 重置为 accepted。"""

    policy_version = "stage2-3-conservative-defer-v1"

    def __init__(
        self,
        *,
        clock: ClockPort,
        query_uow_factory: TaskControlQueryUnitOfWorkFactory,
        recovery_uow_factory: TaskRecoveryUnitOfWorkFactory,
        scan_limit: int = 50,
        defer_seconds: float = 5,
    ) -> None:
        if not isinstance(clock, ClockPort):
            raise TypeError("clock 必须实现 ClockPort")
        if not callable(query_uow_factory) or not callable(recovery_uow_factory):
            raise TypeError("Query/Recovery UoW Factory 必须可调用")
        if type(scan_limit) is not int or scan_limit <= 0:
            raise ValueError("scan_limit 必须是正整数")
        if defer_seconds <= 0:
            raise ValueError("defer_seconds 必须大于 0")
        self._clock = clock
        self._query_uow_factory = query_uow_factory
        self._recovery_uow_factory = recovery_uow_factory
        self._scan_limit = scan_limit
        self._defer_seconds = float(defer_seconds)

    def run_once(self) -> int:
        observed_at = self._clock.now_utc()
        with self._query_uow_factory() as unit_of_work:
            task_ids = unit_of_work.queries.scan_expired_attempts(
                expired_before=observed_at,
                limit=self._scan_limit,
            )
        applied = 0
        for task_id in task_ids:
            # 候选读取与条件写分开；写 UoW 会重新加载并以完整 source Attempt/fencing CAS。
            with self._query_uow_factory() as query_uow:
                candidate = query_uow.queries.load_candidate(task_id)
            if candidate is None:
                continue
            command = TaskRecoveryClassificationCommand(
                candidate=candidate,
                classification=RecoveryClassification.DEFER,
                policy_version=self.policy_version,
                classified_at=observed_at,
                next_action_at=add_persisted_utc_seconds(
                    observed_at,
                    seconds=self._defer_seconds,
                ),
            )
            with self._recovery_uow_factory() as recovery_uow:
                result = recovery_uow.recovery.classify_candidate_if_current(command)
                if result.outcome is TaskRecoveryMutationOutcome.APPLIED:
                    recovery_uow.commit()
                    applied += 1
        if task_ids:
            logger.info(
                "Task Reaper 完成保守观察: candidates=%d deferred=%d policy_version=%s",
                len(task_ids),
                applied,
                self.policy_version,
            )
        return applied


__all__ = ["ConservativeTaskReaper"]
