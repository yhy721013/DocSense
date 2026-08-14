"""Weaponry 失联 execution 的资源、Creation Intent 与审计事实分类。

本模块只读取已持久化事实并形成脱敏摘要，不执行供应商探测、清理或业务重放。真正的
Recovery Case、Observation、Decision 与恢复租约由阶段 2-7 通用协调器持久化。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from app.modules.tasks.domain import (
    RecoveryClassification,
    StepEffectKind,
    TaskId,
    TaskStep,
    TaskStepState,
)
from app.modules.weaponry.ports import (
    WeaponryCreationIntentState,
    WeaponryCreationIntentStorePort,
    WeaponryInteractionAuditPort,
    WeaponryResourceRecordState,
    WeaponryResourceStorePort,
    WeaponryTrackedResourceState,
)


@dataclass(frozen=True, slots=True)
class WeaponryDisconnectedTaskFacts:
    """不含外部名称、URL、正文或 Token 的有界恢复事实摘要。"""

    task_id: TaskId
    resource_record_present: bool
    resource_quarantined: bool
    resource_cleanup_lease_present: bool
    tracked_external_resource_count: int
    unknown_resource_count: int
    pending_intent_count: int
    recovering_intent_count: int
    resolved_intent_count: int
    quarantined_intent_count: int
    pending_audit_count: int
    facts_truncated: bool
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class WeaponryDisconnectedTaskClassification:
    classification: RecoveryClassification
    reason_code: str
    evidence_digest: str


class WeaponryRecoveryFactCollector:
    """从三个 Weaponry Store 读取单任务事实；读取过程不持有跨 Store 写事务。"""

    def __init__(
        self,
        *,
        resources: WeaponryResourceStorePort,
        creation_intents: WeaponryCreationIntentStorePort,
        interaction_audits: WeaponryInteractionAuditPort,
        max_fact_count: int = 1_000,
    ) -> None:
        if not isinstance(resources, WeaponryResourceStorePort):
            raise TypeError("resources 必须实现 WeaponryResourceStorePort")
        if not isinstance(creation_intents, WeaponryCreationIntentStorePort):
            raise TypeError("creation_intents 必须实现 WeaponryCreationIntentStorePort")
        if not isinstance(interaction_audits, WeaponryInteractionAuditPort):
            raise TypeError("interaction_audits 必须实现 WeaponryInteractionAuditPort")
        if isinstance(max_fact_count, bool) or not isinstance(max_fact_count, int) or max_fact_count < 1:
            raise ValueError("max_fact_count 必须是正整数")
        self._resources = resources
        self._creation_intents = creation_intents
        self._interaction_audits = interaction_audits
        self._max_fact_count = max_fact_count

    def collect(self, task_id: TaskId) -> WeaponryDisconnectedTaskFacts:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        page_size = self._max_fact_count + 1
        resource = self._resources.get(task_id)
        intents = self._creation_intents.list_for_task(task_id, limit=page_size)
        audits = self._interaction_audits.list_pending(task_id, limit=page_size)
        facts_truncated = len(intents) > self._max_fact_count or len(audits) > self._max_fact_count
        intents = intents[: self._max_fact_count]
        audits = audits[: self._max_fact_count]
        tracked_resources = () if resource is None else resource.resources

        counts = {
            state: sum(item.state is state for item in intents)
            for state in WeaponryCreationIntentState
        }
        projection = {
            "task_id": task_id.value,
            "resource_record_present": resource is not None,
            "resource_state": "" if resource is None else resource.state.value,
            "resource_cleanup_lease_present": bool(resource and resource.cleanup_lease),
            "tracked_external_resource_count": len(tracked_resources),
            "unknown_resource_count": sum(
                item.state is WeaponryTrackedResourceState.CLEANUP_UNKNOWN
                for item in tracked_resources
            ),
            "intent_state_counts": {
                state.value: counts[state] for state in WeaponryCreationIntentState
            },
            "pending_audit_count": len(audits),
            "facts_truncated": facts_truncated,
        }
        digest = hashlib.sha256(
            json.dumps(
                projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return WeaponryDisconnectedTaskFacts(
            task_id=task_id,
            resource_record_present=resource is not None,
            resource_quarantined=(
                resource is not None
                and resource.state is WeaponryResourceRecordState.QUARANTINED
            ),
            resource_cleanup_lease_present=bool(resource and resource.cleanup_lease),
            tracked_external_resource_count=len(tracked_resources),
            unknown_resource_count=projection["unknown_resource_count"],
            pending_intent_count=counts[WeaponryCreationIntentState.PENDING],
            recovering_intent_count=counts[WeaponryCreationIntentState.RECOVERING],
            resolved_intent_count=counts[WeaponryCreationIntentState.RESOLVED],
            quarantined_intent_count=counts[WeaponryCreationIntentState.QUARANTINED],
            pending_audit_count=len(audits),
            facts_truncated=facts_truncated,
            evidence_digest=digest,
        )


class WeaponryDisconnectedTaskClassifier:
    """按冻结事实保守分类；任何不完整或外部效果事实都进入隔离。"""

    policy_version = "weaponry-disconnected-facts.v1"

    def classify(
        self,
        facts: WeaponryDisconnectedTaskFacts,
        *,
        steps: tuple[TaskStep, ...],
        latest_is_current: bool,
    ) -> WeaponryDisconnectedTaskClassification:
        if not isinstance(facts, WeaponryDisconnectedTaskFacts):
            raise TypeError("facts 必须是 WeaponryDisconnectedTaskFacts")
        if not isinstance(steps, tuple) or any(not isinstance(step, TaskStep) for step in steps):
            raise TypeError("steps 必须是 TaskStep tuple")
        if any(step.task_id != facts.task_id for step in steps):
            raise ValueError("steps 包含其他 task_id")
        if not isinstance(latest_is_current, bool):
            raise TypeError("latest_is_current 必须是 bool")

        if not latest_is_current:
            return self._result(facts, RecoveryClassification.MARK_STALE, "weaponry_latest_superseded")
        if facts.resource_cleanup_lease_present:
            return self._result(facts, RecoveryClassification.DEFER, "weaponry_cleanup_lease_observation_pending")
        if facts.facts_truncated:
            return self._result(facts, RecoveryClassification.RECONCILE_REQUIRED, "weaponry_recovery_facts_truncated")
        if (
            facts.resource_quarantined
            or facts.unknown_resource_count
            or facts.pending_intent_count
            or facts.recovering_intent_count
            or facts.quarantined_intent_count
            or facts.pending_audit_count
        ):
            return self._result(facts, RecoveryClassification.RECONCILE_REQUIRED, "weaponry_external_outcome_unknown")
        terminal = next((step for step in steps if step.step_key == "terminal.commit"), None)
        if terminal is not None and terminal.state is TaskStepState.SUCCEEDED and terminal.checkpoint is not None:
            return self._result(facts, RecoveryClassification.FINALIZE_FROM_CHECKPOINT, "weaponry_terminal_checkpoint_confirmed")
        if facts.tracked_external_resource_count or facts.resolved_intent_count:
            return self._result(facts, RecoveryClassification.RECONCILE_REQUIRED, "weaponry_external_effect_requires_probe")
        if any(
            step.state is TaskStepState.OUTCOME_UNKNOWN
            or (
                step.effect_kind is StepEffectKind.EXTERNAL_WRITE
                and step.state is not TaskStepState.PENDING
            )
            for step in steps
        ):
            return self._result(facts, RecoveryClassification.RECONCILE_REQUIRED, "weaponry_external_step_requires_probe")
        return self._result(facts, RecoveryClassification.RETRY_SAFE, "weaponry_no_external_effect_recorded")

    @staticmethod
    def _result(
        facts: WeaponryDisconnectedTaskFacts,
        classification: RecoveryClassification,
        reason_code: str,
    ) -> WeaponryDisconnectedTaskClassification:
        return WeaponryDisconnectedTaskClassification(
            classification=classification,
            reason_code=reason_code,
            evidence_digest=facts.evidence_digest,
        )


__all__ = [
    "WeaponryDisconnectedTaskClassification",
    "WeaponryDisconnectedTaskClassifier",
    "WeaponryDisconnectedTaskFacts",
    "WeaponryRecoveryFactCollector",
]
