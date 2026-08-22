"""业务策略驱动的有限 Task Reaper；不执行网络探测或直接调用 Runner。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
from uuid import uuid4

from app.modules.tasks.domain import (
    RecoveryClassification,
    TaskRecoveryCandidate,
    add_persisted_utc_seconds,
)
from app.modules.tasks.ports import (
    ClockPort,
    TaskControlQueryUnitOfWorkFactory,
    TaskRecoveryClassificationCommand,
    TaskRecoveryMutationOutcome,
    TaskRecoveryPolicyPort,
    TaskRecoveryUnitOfWorkFactory,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecoverExpiredAttemptsResult:
    scanned: int
    classified: int
    source_changed: int
    by_classification: tuple[tuple[str, int], ...]


class RecoverExpiredTaskAttempts:
    """按 task_type 路由纯 Policy，并用 source Attempt/fencing CAS 收敛候选。"""

    def __init__(
        self,
        *,
        clock: ClockPort,
        query_uow_factory: TaskControlQueryUnitOfWorkFactory,
        recovery_uow_factory: TaskRecoveryUnitOfWorkFactory,
        policies: Mapping[str, TaskRecoveryPolicyPort],
        scan_limit: int = 50,
        retry_backoff_seconds: float = 5.0,
        defer_seconds: float = 30.0,
        case_id_factory: Callable[[str], str] | None = None,
    ) -> None:
        if not isinstance(clock, ClockPort):
            raise TypeError("clock 必须实现 ClockPort")
        if not callable(query_uow_factory) or not callable(recovery_uow_factory):
            raise TypeError("Query/Recovery UoW Factory 必须可调用")
        normalized = dict(policies)
        if set(normalized) != {"report", "weaponry", "file"}:
            raise ValueError("policies 必须精确覆盖 report/weaponry/file")
        if any(
            not isinstance(item, TaskRecoveryPolicyPort)
            for item in normalized.values()
        ):
            raise TypeError("policies 值必须实现 TaskRecoveryPolicyPort")
        if type(scan_limit) is not int or scan_limit <= 0:
            raise ValueError("scan_limit 必须是正整数")
        if retry_backoff_seconds <= 0 or defer_seconds <= 0:
            raise ValueError("Recovery 退避时间必须大于 0")
        if case_id_factory is not None and not callable(case_id_factory):
            raise TypeError("case_id_factory 必须可调用或为 None")
        self._clock = clock
        self._query_uow_factory = query_uow_factory
        self._recovery_uow_factory = recovery_uow_factory
        self._policies = normalized
        self._scan_limit = scan_limit
        self._retry_backoff_seconds = float(retry_backoff_seconds)
        self._defer_seconds = float(defer_seconds)
        self._case_id_factory = case_id_factory or (
            lambda task_type: f"{task_type}-reaper-{uuid4().hex}"
        )

    def run_once(self) -> RecoverExpiredAttemptsResult:
        classified_at = self._clock.now_utc()
        with self._query_uow_factory() as unit_of_work:
            task_ids = unit_of_work.queries.scan_expired_attempts(
                expired_before=classified_at,
                limit=self._scan_limit,
            )

        counts = {item.value: 0 for item in RecoveryClassification}
        applied = 0
        source_changed = 0
        for task_id in task_ids:
            with self._query_uow_factory() as unit_of_work:
                candidate = unit_of_work.queries.load_candidate(task_id)
                steps = unit_of_work.queries.list_steps(task_id)
            if candidate is None:
                source_changed += 1
                continue
            policy = self._policies[candidate.task.task_type]
            classification = policy.classify(
                candidate,
                steps=steps,
                observations=(),
            )
            command = self._command(
                candidate=candidate,
                classification=classification,
                policy_version=policy.policy_version,
                classified_at=classified_at,
            )
            with self._recovery_uow_factory() as unit_of_work:
                result = unit_of_work.recovery.classify_candidate_if_current(command)
                if result.outcome is TaskRecoveryMutationOutcome.APPLIED:
                    unit_of_work.commit()
                    applied += 1
                    counts[classification.value] += 1
                else:
                    source_changed += 1

        if task_ids:
            logger.info(
                "业务 Task Reaper 扫描完成: candidates=%d classified=%d "
                "source_changed=%d classifications=%s",
                len(task_ids),
                applied,
                source_changed,
                ",".join(
                    f"{key}:{value}" for key, value in sorted(counts.items())
                ),
            )
        return RecoverExpiredAttemptsResult(
            scanned=len(task_ids),
            classified=applied,
            source_changed=source_changed,
            by_classification=tuple(sorted(counts.items())),
        )

    def _command(
        self,
        *,
        candidate: TaskRecoveryCandidate,
        classification: RecoveryClassification,
        policy_version: str,
        classified_at: str,
    ) -> TaskRecoveryClassificationCommand:
        case_id = ""
        next_action_at = ""
        if classification in {
            RecoveryClassification.RECONCILE_REQUIRED,
            RecoveryClassification.FINALIZE_FROM_CHECKPOINT,
        }:
            case_id = self._case_id_factory(candidate.task.task_type)
        elif classification in {
            RecoveryClassification.RETRY_SAFE,
            RecoveryClassification.DEFER,
        }:
            delay = (
                self._retry_backoff_seconds
                if classification is RecoveryClassification.RETRY_SAFE
                else self._defer_seconds
            )
            next_action_at = add_persisted_utc_seconds(
                classified_at,
                seconds=delay,
            )
        return TaskRecoveryClassificationCommand(
            candidate=candidate,
            classification=classification,
            policy_version=policy_version,
            classified_at=classified_at,
            case_id=case_id,
            next_action_at=next_action_at,
        )


__all__ = ["RecoverExpiredAttemptsResult", "RecoverExpiredTaskAttempts"]
