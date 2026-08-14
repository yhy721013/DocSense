"""阶段 2-5 第 6 步：Weaponry 断联任务事实分类验收。"""

from __future__ import annotations

from dataclasses import replace
import unittest

from app.modules.tasks.domain import (
    RecoveryClassification,
    TaskId,
    TaskStepCheckpoint,
    TaskStepState,
)
from app.modules.weaponry.application import (
    WeaponryDisconnectedTaskClassifier,
    WeaponryDisconnectedTaskFacts,
    resolve_weaponry_step,
)


_TASK_ID = TaskId("weaponry-disconnected-task-1")
_DIGEST = "a" * 64


def _facts(**changes) -> WeaponryDisconnectedTaskFacts:
    values = {
        "task_id": _TASK_ID,
        "resource_record_present": False,
        "resource_quarantined": False,
        "resource_cleanup_lease_present": False,
        "tracked_external_resource_count": 0,
        "unknown_resource_count": 0,
        "pending_intent_count": 0,
        "recovering_intent_count": 0,
        "resolved_intent_count": 0,
        "quarantined_intent_count": 0,
        "pending_audit_count": 0,
        "facts_truncated": False,
        "evidence_digest": _DIGEST,
    }
    values.update(changes)
    return WeaponryDisconnectedTaskFacts(**values)


def _step(step_key: str, *, state: TaskStepState, checkpoint=None):
    definition = resolve_weaponry_step(step_key)
    step = definition.new_step(
        task_id=_TASK_ID,
        step_key=step_key,
        idempotency_key=f"idempotency:{step_key}",
    )
    return replace(
        step,
        state=state,
        current_step_attempt_no=(0 if state is TaskStepState.PENDING else 1),
        checkpoint=checkpoint,
        row_version=(0 if state is TaskStepState.PENDING else 1),
    )


class WeaponryDisconnectedTaskClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = WeaponryDisconnectedTaskClassifier()

    def assert_classification(self, expected, facts, *, steps=(), latest=True):
        result = self.classifier.classify(
            facts,
            steps=steps,
            latest_is_current=latest,
        )
        self.assertIs(expected, result.classification)
        self.assertEqual(facts.evidence_digest, result.evidence_digest)
        self.assertTrue(result.reason_code)

    def test_unknown_component_facts_always_require_reconciliation(self) -> None:
        cases = (
            _facts(resource_quarantined=True),
            _facts(unknown_resource_count=1),
            _facts(pending_intent_count=1),
            _facts(recovering_intent_count=1),
            _facts(quarantined_intent_count=1),
            _facts(pending_audit_count=1),
            _facts(facts_truncated=True),
        )
        for facts in cases:
            with self.subTest(facts=facts):
                self.assert_classification(
                    RecoveryClassification.RECONCILE_REQUIRED,
                    facts,
                )

    def test_recorded_effect_or_started_external_step_is_never_retry_safe(self) -> None:
        self.assert_classification(
            RecoveryClassification.RECONCILE_REQUIRED,
            _facts(tracked_external_resource_count=1),
        )
        self.assert_classification(
            RecoveryClassification.RECONCILE_REQUIRED,
            _facts(resolved_intent_count=1),
        )
        self.assert_classification(
            RecoveryClassification.RECONCILE_REQUIRED,
            _facts(),
            steps=(
                _step("retrieval.execute:1", state=TaskStepState.RUNNING),
            ),
        )

    def test_only_no_external_effect_path_is_retry_safe(self) -> None:
        self.assert_classification(
            RecoveryClassification.RETRY_SAFE,
            _facts(resource_record_present=True),
            steps=(
                _step("document_scope.load", state=TaskStepState.SUCCEEDED),
                _step("result.map", state=TaskStepState.PENDING),
            ),
        )

    def test_cleanup_lease_defers_and_superseded_latest_marks_stale(self) -> None:
        self.assert_classification(
            RecoveryClassification.DEFER,
            _facts(resource_cleanup_lease_present=True),
        )
        self.assert_classification(
            RecoveryClassification.MARK_STALE,
            _facts(resource_cleanup_lease_present=True),
            latest=False,
        )

    def test_terminal_checkpoint_is_finalize_candidate_not_direct_terminal_write(self) -> None:
        terminal = _step(
            "terminal.commit",
            state=TaskStepState.SUCCEEDED,
            checkpoint=TaskStepCheckpoint(
                code="weaponry_terminal",
                result_ref="weaponry-result:132",
                result_digest="b" * 64,
            ),
        )
        self.assert_classification(
            RecoveryClassification.FINALIZE_FROM_CHECKPOINT,
            _facts(),
            steps=(terminal,),
        )


if __name__ == "__main__":
    unittest.main()
