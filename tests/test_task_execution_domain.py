"""阶段 2-1 统一 Task 纯领域状态机、Authority 与 Recovery 测试。"""

from __future__ import annotations

import itertools
import unittest

from app.modules.tasks.domain import (
    RecoveryCaseState,
    RecoveryDecisionKind,
    RecoveryObservationKind,
    RecoveryOperationKind,
    RecoveryOperationState,
    StepEffectKind,
    StepReplayPolicy,
    TaskAttemptState,
    TaskAttemptTransition,
    TaskBatchRef,
    TaskBusinessRef,
    TaskEvent,
    TaskExecutionAuthority,
    TaskId,
    TaskOwnerIdentity,
    TaskRecord,
    TaskRecoveryDecision,
    TaskRecoveryIsolation,
    TaskRecoveryObservation,
    TaskRecoveryOperation,
    TaskRecoveryStepResolution,
    TaskRecoveryTerminalProjection,
    TaskState,
    TaskStateTransitionError,
    TaskStep,
    TaskStepState,
    TaskStepTransition,
    TaskTransition,
    apply_recovery_decision,
    apply_recovery_step_resolution,
    claim_recovery_case,
    converge_recovery_operation,
    create_recovery_case,
    take_over_expired_recovery_case,
    transition_attempt_state,
    transition_step_state,
    transition_task_state,
)


NOW = "2026-08-12T01:02:03.123456Z"
LATER = "2026-08-12T01:02:33.123456Z"
MUCH_LATER = "2026-08-12T01:03:03.123456Z"
DIGEST = "a" * 64


def _task(*, state: TaskState = TaskState.RUNNING) -> TaskRecord:
    return TaskRecord(
        task_id=TaskId("task-1"),
        task_type="report",
        business_ref=TaskBusinessRef("report", "101"),
        state=state,
        current_attempt_no=1,
        fencing_token=1,
        row_version=7,
        recovery_generation=0,
    )


class TaskStateMachineTests(unittest.TestCase):
    """以完整笛卡尔积证明合法转换精确且非法转换全部拒绝。"""

    def test_task_transition_table_is_exact(self) -> None:
        expected = {
            (TaskState.ACCEPTED, TaskTransition.CLAIM): TaskState.RUNNING,
            (TaskState.ACCEPTED, TaskTransition.SUPERSEDE): TaskState.STALE,
            (TaskState.RUNNING, TaskTransition.BUSINESS_SUCCEEDED): TaskState.SUCCEEDED,
            (TaskState.RUNNING, TaskTransition.BUSINESS_FAILED): TaskState.FAILED,
            (TaskState.RUNNING, TaskTransition.ISOLATE_FOR_RECOVERY): TaskState.RECOVERY_REQUIRED,
            (TaskState.RUNNING, TaskTransition.SUPERSEDE): TaskState.STALE,
            (TaskState.RUNNING, TaskTransition.RETRY_SAFE): TaskState.ACCEPTED,
            (TaskState.RECOVERY_REQUIRED, TaskTransition.RETRY_AUTHORIZED): TaskState.ACCEPTED,
            (TaskState.RECOVERY_REQUIRED, TaskTransition.RECONCILED_SUCCEEDED): TaskState.SUCCEEDED,
            (TaskState.RECOVERY_REQUIRED, TaskTransition.RECONCILED_FAILED): TaskState.FAILED,
            (TaskState.RECOVERY_REQUIRED, TaskTransition.SUPERSEDE): TaskState.STALE,
        }
        for state, transition in itertools.product(TaskState, TaskTransition):
            with self.subTest(state=state, transition=transition):
                target = expected.get((state, transition))
                if target is None:
                    with self.assertRaises(TaskStateTransitionError):
                        transition_task_state(state, transition)
                else:
                    self.assertIs(target, transition_task_state(state, transition))

    def test_attempt_transition_table_is_exact(self) -> None:
        expected = {
            (TaskAttemptState.LEASED, TaskAttemptTransition.START): TaskAttemptState.RUNNING,
            (TaskAttemptState.LEASED, TaskAttemptTransition.LEASE_EXPIRED): TaskAttemptState.EXPIRED,
            (TaskAttemptState.RUNNING, TaskAttemptTransition.SUCCEED): TaskAttemptState.SUCCEEDED,
            (TaskAttemptState.RUNNING, TaskAttemptTransition.FAIL): TaskAttemptState.FAILED,
            (TaskAttemptState.RUNNING, TaskAttemptTransition.LEASE_EXPIRED): TaskAttemptState.EXPIRED,
            (
                TaskAttemptState.RUNNING,
                TaskAttemptTransition.ISOLATE_FOR_RECOVERY,
            ): TaskAttemptState.ABANDONED,
            (TaskAttemptState.EXPIRED, TaskAttemptTransition.ABANDON_AFTER_CLASSIFICATION): TaskAttemptState.ABANDONED,
        }
        for state, transition in itertools.product(TaskAttemptState, TaskAttemptTransition):
            with self.subTest(state=state, transition=transition):
                target = expected.get((state, transition))
                if target is None:
                    with self.assertRaises(TaskStateTransitionError):
                        transition_attempt_state(state, transition)
                else:
                    self.assertIs(target, transition_attempt_state(state, transition))

    def test_step_transition_table_is_exact(self) -> None:
        expected = {
            (TaskStepState.PENDING, TaskStepTransition.BEGIN): TaskStepState.RUNNING,
            (TaskStepState.PENDING, TaskStepTransition.SKIP): TaskStepState.SKIPPED,
            (TaskStepState.RUNNING, TaskStepTransition.SUCCEED): TaskStepState.SUCCEEDED,
            (TaskStepState.RUNNING, TaskStepTransition.FAIL): TaskStepState.FAILED,
            (TaskStepState.RUNNING, TaskStepTransition.MARK_OUTCOME_UNKNOWN): TaskStepState.OUTCOME_UNKNOWN,
            (TaskStepState.OUTCOME_UNKNOWN, TaskStepTransition.SUCCEED): TaskStepState.SUCCEEDED,
            (TaskStepState.OUTCOME_UNKNOWN, TaskStepTransition.FAIL): TaskStepState.FAILED,
            (TaskStepState.OUTCOME_UNKNOWN, TaskStepTransition.COMPENSATE): TaskStepState.COMPENSATED,
            (
                TaskStepState.OUTCOME_UNKNOWN,
                TaskStepTransition.RETRY_AUTHORIZED,
            ): TaskStepState.PENDING,
        }
        for state, transition in itertools.product(TaskStepState, TaskStepTransition):
            with self.subTest(state=state, transition=transition):
                target = expected.get((state, transition))
                if target is None:
                    with self.assertRaises(TaskStateTransitionError):
                        transition_step_state(state, transition)
                else:
                    self.assertIs(target, transition_step_state(state, transition))


class TaskExecutionDomainTests(unittest.TestCase):
    def test_recovery_operation_requires_intent_before_observation(self) -> None:
        operation = TaskRecoveryOperation(
            operation_id="operation-probe-1",
            case_id="case-operation-1",
            generation=1,
            recovery_fencing_token=1,
            kind=RecoveryOperationKind.PROBE,
            step_key="rag.generate",
            idempotency_key="case-operation-1:rag.generate:probe",
            intent_digest=DIGEST,
            external_ref="provider:request-1",
            state=RecoveryOperationState.INTENT_RECORDED,
            intent_at=NOW,
        )
        observation = TaskRecoveryObservation(
            observation_id="observation-probe-1",
            operation_id=operation.operation_id,
            case_id=operation.case_id,
            generation=operation.generation,
            # 接管后的新 owner 可以收敛旧 fencing 下已提交的 Intent，但 Observation 自身仍由
            # 新 fencing 保护。数据库 Store 会同时复核当前 Case Authority。
            recovery_fencing_token=2,
            kind=RecoveryObservationKind.NO_EFFECT_CONFIRMED,
            evidence_digest=DIGEST,
            observed_at=LATER,
            step_key=operation.step_key,
            external_ref=operation.external_ref,
        )
        converged = converge_recovery_operation(operation, observation)
        self.assertIs(
            RecoveryOperationState.OBSERVATION_RECORDED,
            converged.state,
        )
        self.assertEqual(LATER, converged.result_at)
        with self.assertRaisesRegex(ValueError, "已经收敛"):
            converge_recovery_operation(converged, observation)

    def test_recovery_isolation_requires_stable_case_policy_and_reason(self) -> None:
        isolation = TaskRecoveryIsolation(
            case_id="case-outcome-unknown-1",
            reason_code="provider_outcome_unknown",
            policy_version="report-recovery-v1",
        )
        self.assertEqual("case-outcome-unknown-1", isolation.case_id)
        with self.assertRaises(ValueError):
            TaskRecoveryIsolation(
                case_id="invalid case id",
                reason_code="provider_outcome_unknown",
                policy_version="report-recovery-v1",
            )

    def test_terminal_projection_contains_checkpoint_cas_without_body(self) -> None:
        projection = TaskRecoveryTerminalProjection(
            source_step_key="terminal.commit",
            source_step_attempt_no=2,
            checkpoint_code="result_persisted",
            checkpoint_digest=DIGEST,
            public_status="completed",
            message="任务已完成",
            result_ref="result:task-1",
        )
        self.assertEqual(2, projection.source_step_attempt_no)
        with self.assertRaises(ValueError):
            TaskRecoveryTerminalProjection(
                source_step_key="terminal.commit",
                source_step_attempt_no=2,
                checkpoint_code="result_persisted",
                checkpoint_digest="not-a-digest",
                public_status="completed",
                message="任务已完成",
            )

    def test_batch_and_owner_identity_are_structured_without_store_guessing(self) -> None:
        batch = TaskBatchRef(batch_id="analysis-batch-1", sequence=2)
        self.assertEqual(2, batch.sequence)
        owner = TaskOwnerIdentity(
            instance_start_id="12345678-1234-4234-8234-123456789abc",
            process_id=321,
            executor_name="analysis",
            worker_slot="worker-2",
        )
        self.assertEqual(
            "12345678-1234-4234-8234-123456789abc/321/analysis/worker-2",
            owner.owner_id,
        )
        with self.assertRaises(ValueError):
            TaskOwnerIdentity(
                instance_start_id="not-a-uuid",
                process_id=321,
                executor_name="analysis",
                worker_slot="worker-2",
            )
        with self.assertRaises(ValueError):
            TaskOwnerIdentity(
                instance_start_id="12345678-1234-4234-8234-123456789abc",
                process_id=321,
                executor_name="analysis/ambiguous",
                worker_slot="worker-2",
            )

    def test_authority_requires_strict_utc_and_complete_identity(self) -> None:
        authority = TaskExecutionAuthority(
            task_id=TaskId("task-1"),
            attempt_no=1,
            owner_id="instance/pid/report/worker-0",
            lease_token="opaque-high-entropy-token",
            fencing_token=1,
            lease_expires_at=LATER,
        )
        self.assertEqual(1, authority.attempt_no)
        with self.assertRaises(ValueError):
            TaskExecutionAuthority(
                task_id=TaskId("task-1"),
                attempt_no=1,
                owner_id="instance/pid/report/worker-0",
                lease_token="token",
                fencing_token=1,
                lease_expires_at="2026-08-12T09:02:33+08:00",
            )
        with self.assertRaises(ValueError):
            TaskExecutionAuthority(
                task_id=TaskId("task-1"),
                attempt_no=1,
                owner_id="instance/pid/report/worker-0",
                lease_token="token",
                fencing_token=1,
                lease_expires_at="2026-02-30T09:02:33.000000Z",
            )

    def test_external_write_cannot_claim_unconditional_safe_replay(self) -> None:
        with self.assertRaises(ValueError):
            TaskStep(
                task_id=TaskId("task-1"),
                step_key="rag.generate",
                definition_version=1,
                effect_kind=StepEffectKind.EXTERNAL_WRITE,
                replay_policy=StepReplayPolicy.SAFE,
                state=TaskStepState.PENDING,
                current_step_attempt_no=0,
                idempotency_key="report:task-1:generation",
                checkpoint=None,
                row_version=0,
            )

    def test_event_metadata_rejects_duplicate_keys_without_silent_collapse(self) -> None:
        with self.assertRaises(ValueError):
            TaskEvent(
                task_id=TaskId("event-task"),
                sequence_no=1,
                event_type="task.accepted",
                trace_id="trace-event",
                created_at="2026-08-12T00:00:00.000000Z",
                metadata=(("reason", "first"), ("reason", "second")),
            )

    def test_recovery_required_record_must_reference_current_case(self) -> None:
        with self.assertRaises(ValueError):
            TaskRecord(
                task_id=TaskId("task-1"),
                task_type="analysis",
                business_ref=TaskBusinessRef("file", "f-1"),
                state=TaskState.RECOVERY_REQUIRED,
                current_attempt_no=1,
                fencing_token=1,
                row_version=1,
                recovery_generation=1,
            )


class TaskRecoveryGenerationTests(unittest.TestCase):
    """证明 generation 只在创建新独立 Case 时递增。"""

    def _decision(
        self,
        *,
        task: TaskRecord,
        case_id: str,
        generation: int,
        recovery_fencing: int,
        kind: RecoveryDecisionKind,
        retry_from: str = "",
        terminal: TaskState | None = None,
        next_observation_at: str = "",
        terminal_projection: TaskRecoveryTerminalProjection | None = None,
        step_resolution: TaskRecoveryStepResolution | None = None,
    ) -> TaskRecoveryDecision:
        if kind is RecoveryDecisionKind.RETRY_AUTHORIZED and step_resolution is None:
            step_resolution = TaskRecoveryStepResolution(
                source_step_key=retry_from,
                source_step_attempt_no=1,
                expected_step_row_version=2,
                operation_id="operation-retry-1",
                observation_id="observation-retry-1",
                evidence_digest=DIGEST,
                target_transition=TaskStepTransition.RETRY_AUTHORIZED,
            )
        return TaskRecoveryDecision(
            decision_id=f"decision-{kind.value}",
            task_id=task.task_id,
            case_id=case_id,
            generation=generation,
            recovery_fencing_token=recovery_fencing,
            expected_task_row_version=task.row_version,
            source_attempt_no=1,
            source_fencing_token=1,
            kind=kind,
            evidence_digest=DIGEST,
            reason_code="verified",
            policy_version="report-recovery-v1",
            actor_marker="automatic/recovery-0",
            decided_at=NOW,
            retry_from_step_key=retry_from,
            terminal_state=terminal,
            next_observation_at=next_observation_at,
            terminal_projection=terminal_projection,
            step_resolution=step_resolution,
        )

    def test_create_claim_keep_and_retry_use_separate_sequences(self) -> None:
        original = _task()
        isolated, case = create_recovery_case(
            original,
            case_id="case-1",
            source_attempt_no=1,
            source_fencing_token=1,
            reason_code="lease_expired",
            policy_version="report-recovery-v1",
            created_at=NOW,
        )
        self.assertEqual(1, isolated.recovery_generation)
        self.assertEqual(1, case.generation)

        claimed, authority = claim_recovery_case(
            case,
            owner_id="instance/pid/recovery/worker-0",
            lease_token="recovery-token-1",
            lease_expires_at=LATER,
        )
        self.assertEqual(1, claimed.generation)
        self.assertEqual(1, authority.generation)
        self.assertEqual(1, claimed.recovery_fencing_token)

        keep = self._decision(
            task=isolated,
            case_id=claimed.case_id,
            generation=claimed.generation,
            recovery_fencing=claimed.recovery_fencing_token,
            kind=RecoveryDecisionKind.KEEP_QUARANTINED,
            next_observation_at=LATER,
        )
        still_isolated, awaiting = apply_recovery_decision(isolated, claimed, keep)
        self.assertEqual(1, still_isolated.recovery_generation)
        self.assertIs(RecoveryCaseState.AWAITING_EVIDENCE, awaiting.state)
        self.assertEqual(LATER, awaiting.next_observation_at)

        reclaimed, _ = claim_recovery_case(
            awaiting,
            owner_id="instance/pid/recovery/worker-1",
            lease_token="recovery-token-2",
            lease_expires_at=LATER,
        )
        self.assertEqual(1, reclaimed.generation)
        self.assertEqual(2, reclaimed.recovery_fencing_token)

        retry = self._decision(
            task=still_isolated,
            case_id=reclaimed.case_id,
            generation=reclaimed.generation,
            recovery_fencing=reclaimed.recovery_fencing_token,
            kind=RecoveryDecisionKind.RETRY_AUTHORIZED,
            retry_from="rag.generate",
        )
        unknown_step = TaskStep(
            task_id=still_isolated.task_id,
            step_key="rag.generate",
            definition_version=1,
            effect_kind=StepEffectKind.EXTERNAL_WRITE,
            replay_policy=StepReplayPolicy.RECONCILE_ONLY,
            state=TaskStepState.OUTCOME_UNKNOWN,
            current_step_attempt_no=1,
            idempotency_key="task-1:rag.generate",
            checkpoint=None,
            row_version=2,
        )
        assert retry.step_resolution is not None
        reset_step = apply_recovery_step_resolution(
            unknown_step,
            retry.step_resolution,
        )
        accepted, resolved = apply_recovery_decision(still_isolated, reclaimed, retry)
        self.assertIs(TaskStepState.PENDING, reset_step.state)
        self.assertEqual(1, reset_step.current_step_attempt_no)
        self.assertEqual(3, reset_step.row_version)
        self.assertIs(TaskState.ACCEPTED, accepted.state)
        self.assertEqual("rag.generate", accepted.retry_from_step_key)
        self.assertEqual(1, accepted.recovery_generation)
        self.assertIs(RecoveryCaseState.RESOLVED, resolved.state)

    def test_observing_case_takeover_requires_database_lease_expiry(self) -> None:
        _isolated, case = create_recovery_case(
            _task(),
            case_id="case-takeover",
            source_attempt_no=1,
            source_fencing_token=1,
            reason_code="lease_expired",
            policy_version="report-recovery-v1",
            created_at=NOW,
        )
        observing, authority = claim_recovery_case(
            case,
            owner_id="instance/pid/recovery/worker-0",
            lease_token="recovery-token-1",
            lease_expires_at=LATER,
        )
        with self.assertRaisesRegex(ValueError, "不得被抢占"):
            take_over_expired_recovery_case(
                observing,
                authority,
                claimed_at=NOW,
                owner_id="instance/pid/recovery/worker-1",
                lease_token="recovery-token-2",
                lease_expires_at=MUCH_LATER,
            )

        taken_over, replacement = take_over_expired_recovery_case(
            observing,
            authority,
            claimed_at=LATER,
            owner_id="instance/pid/recovery/worker-1",
            lease_token="recovery-token-2",
            lease_expires_at=MUCH_LATER,
        )
        self.assertIs(RecoveryCaseState.OBSERVING, taken_over.state)
        self.assertEqual(2, taken_over.recovery_fencing_token)
        self.assertEqual(2, replacement.fencing_token)

    def test_finalize_requires_complete_terminal_projection(self) -> None:
        isolated, case = create_recovery_case(
            _task(),
            case_id="case-finalize",
            source_attempt_no=1,
            source_fencing_token=1,
            reason_code="terminal_checkpoint_found",
            policy_version="report-recovery-v1",
            created_at=NOW,
        )
        claimed, _ = claim_recovery_case(
            case,
            owner_id="instance/pid/recovery/worker-0",
            lease_token="recovery-token",
            lease_expires_at=LATER,
        )
        with self.assertRaisesRegex(ValueError, "完整终态投影"):
            self._decision(
                task=isolated,
                case_id=claimed.case_id,
                generation=claimed.generation,
                recovery_fencing=claimed.recovery_fencing_token,
                kind=RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT,
                terminal=TaskState.SUCCEEDED,
            )

        projection = TaskRecoveryTerminalProjection(
            source_step_key="terminal.commit",
            source_step_attempt_no=1,
            checkpoint_code="result_persisted",
            checkpoint_digest=DIGEST,
            public_status="completed",
            message="任务已完成",
            result_ref="result:task-1",
        )
        decision = self._decision(
            task=isolated,
            case_id=claimed.case_id,
            generation=claimed.generation,
            recovery_fencing=claimed.recovery_fencing_token,
            kind=RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT,
            terminal=TaskState.SUCCEEDED,
            terminal_projection=projection,
        )
        completed, resolved = apply_recovery_decision(isolated, claimed, decision)
        self.assertIs(TaskState.SUCCEEDED, completed.state)
        self.assertIs(RecoveryCaseState.RESOLVED, resolved.state)

    def test_stale_recovery_fencing_is_rejected(self) -> None:
        isolated, case = create_recovery_case(
            _task(),
            case_id="case-1",
            source_attempt_no=1,
            source_fencing_token=1,
            reason_code="lease_expired",
            policy_version="report-recovery-v1",
            created_at=NOW,
        )
        claimed, _ = claim_recovery_case(
            case,
            owner_id="instance/pid/recovery/worker-0",
            lease_token="recovery-token",
            lease_expires_at=LATER,
        )
        stale = self._decision(
            task=isolated,
            case_id=claimed.case_id,
            generation=claimed.generation,
            recovery_fencing=claimed.recovery_fencing_token + 1,
            kind=RecoveryDecisionKind.RETRY_AUTHORIZED,
            retry_from="rag.generate",
        )
        with self.assertRaises(ValueError):
            apply_recovery_decision(isolated, claimed, stale)


if __name__ == "__main__":
    unittest.main()
