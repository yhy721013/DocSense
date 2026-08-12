"""阶段 2-1 严格 Task Control Fake 的失权、时钟和受理矩阵测试。"""

from __future__ import annotations

from dataclasses import replace
import unittest

from app.modules.tasks.domain import (
    RecoveryDecisionKind,
    RecoveryObservationKind,
    RecoveryOperationKind,
    RecoveryOperationState,
    StepEffectKind,
    StepReplayPolicy,
    TaskBatchRef,
    TaskBusinessRef,
    TaskId,
    TaskOwnerIdentity,
    TaskRecord,
    TaskRecoveryIsolation,
    TaskRecoveryDecision,
    TaskRecoveryObservation,
    TaskRecoveryOperation,
    TaskRecoveryStepResolution,
    TaskState,
    TaskTransition,
    TaskStep,
    TaskStepState,
    TaskStepTransition,
)
from app.modules.tasks.ports import (
    CallbackAdmissionConflict,
    ClockAnomalyError,
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
    TaskClaimRequest,
    TaskExecutionMutationOutcome,
    TaskHeartbeatCommand,
    TaskProgressCommand,
    TaskRecoveryClaimRequest,
    TaskRecoveryMutationOutcome,
    TaskRecoveryOperationIntentCommand,
    TaskStepCompletionCommand,
    TaskStepIntentCommand,
    TaskStepSkipCommand,
    TaskTerminalCommand,
)
from tests.fakes import FakeClock, StrictTaskControlFake


_NOW = "2026-08-12T00:00:00.000000Z"
_LEASE_END = "2026-08-12T00:00:30.000000Z"


def _request(task_id: str, business_key: str) -> TaskAdmissionRequest[tuple[str, ...]]:
    return TaskAdmissionRequest(
        task_id=TaskId(task_id),
        task_type="report",
        business_ref=TaskBusinessRef("report", business_key),
        input_schema_version=1,
        input_snapshot=(business_key,),
        input_payload={"business_key": business_key},
        public_request_payload={"reportId": business_key},
        initial_public_status="waiting",
        trace_id=f"trace-{task_id}",
        accepted_at=_NOW,
    )


def _file_request(
    task_id: str,
    business_key: str,
    *,
    batch_id: str,
    sequence: int,
) -> TaskAdmissionRequest[tuple[str, ...]]:
    """构造携带显式批次身份的 Analysis 文件 Task。"""

    return TaskAdmissionRequest(
        task_id=TaskId(task_id),
        task_type="file",
        business_ref=TaskBusinessRef("file", business_key),
        input_schema_version=1,
        input_snapshot=(business_key,),
        input_payload={"business_key": business_key},
        public_request_payload={"fileId": business_key},
        initial_public_status="waiting",
        trace_id=f"trace-{task_id}",
        accepted_at=_NOW,
        batch=TaskBatchRef(batch_id=batch_id, sequence=sequence),
    )


def _claim(control: StrictTaskControlFake, request: TaskAdmissionRequest[object]):
    admission = control.admit_one(request)
    if admission.outcome is not TaskAdmissionOutcome.ACCEPTED:
        raise AssertionError("测试前置受理失败")
    result = control.claim(
        TaskClaimRequest(
            task_id=request.task_id,
            task_type=request.task_type,
            owner=TaskOwnerIdentity(
                instance_start_id="12345678-1234-4234-8234-123456789abc",
                process_id=100,
                executor_name="report",
                worker_slot="worker-0",
            ),
            lease_token=f"lease-{request.task_id}",
            claimed_at=_NOW,
            lease_expires_at=_LEASE_END,
        )
    )
    if result.attempt is None:
        raise AssertionError("测试前置 claim 失败")
    return result.attempt.authority


class FakeClockTests(unittest.TestCase):
    def test_time_expiry_requires_no_real_sleep(self) -> None:
        clock = FakeClock(_NOW)
        self.assertEqual(clock.advance(seconds=30), _LEASE_END)

    def test_clock_rollback_fails_closed(self) -> None:
        clock = FakeClock(_NOW)
        clock.rollback(seconds=1)
        with self.assertRaises(ClockAnomalyError):
            clock.now_utc()


class StrictTaskControlFakeTests(unittest.TestCase):
    @staticmethod
    def _pending_step(task_id: TaskId, step_key: str) -> TaskStep:
        return TaskStep(
            task_id=task_id,
            step_key=step_key,
            definition_version=1,
            effect_kind=StepEffectKind.EXTERNAL_WRITE,
            replay_policy=StepReplayPolicy.RECONCILE_ONLY,
            state=TaskStepState.PENDING,
            current_step_attempt_no=0,
            idempotency_key=f"{task_id}:{step_key}",
            checkpoint=None,
            row_version=0,
        )

    def test_step_outcome_unknown_atomically_revokes_attempt_and_creates_case(self) -> None:
        control = StrictTaskControlFake(FakeClock(_NOW))
        authority = _claim(control, _request("task-step-unknown", "step-unknown"))
        self.assertIs(
            TaskExecutionMutationOutcome.APPLIED,
            control.start(authority, started_at=_NOW),
        )
        step = self._pending_step(authority.task_id, "rag.generate")
        self.assertIs(
            TaskExecutionMutationOutcome.APPLIED,
            control.begin_step(
                TaskStepIntentCommand(
                    authority=authority,
                    step=step,
                    intent_at=_NOW,
                )
            ),
        )
        result = control.complete_step(
            TaskStepCompletionCommand(
                authority=authority,
                step_key=step.step_key,
                step_attempt_no=1,
                transition=TaskStepTransition.MARK_OUTCOME_UNKNOWN,
                checkpoint=None,
                error_code="provider_outcome_unknown",
                completed_at=_NOW,
                recovery_isolation=TaskRecoveryIsolation(
                    case_id="case-step-unknown",
                    reason_code="provider_outcome_unknown",
                    policy_version="report-recovery-v1",
                ),
            )
        )
        self.assertIs(TaskExecutionMutationOutcome.APPLIED, result)
        task = control.get_task(authority.task_id)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertIs(TaskState.RECOVERY_REQUIRED, task.state)
        self.assertEqual("case-step-unknown", task.current_recovery_case_id)
        self.assertIsNotNone(control.get_recovery_case("case-step-unknown"))

        # unknown 事务已撤销旧 Attempt；即使 lease 时间尚未到期，旧 Authority 也不能再写进度。
        self.assertIs(
            TaskExecutionMutationOutcome.INVALID_STATE,
            control.update_progress(
                TaskProgressCommand(
                    authority=authority,
                    progress=0.5,
                    message="stale worker",
                    public_status="processing",
                    updated_at=_NOW,
                )
            ),
        )

    def test_pending_step_skip_is_not_misreported_as_success(self) -> None:
        control = StrictTaskControlFake(FakeClock(_NOW))
        authority = _claim(control, _request("task-step-skip", "step-skip"))
        control.start(authority, started_at=_NOW)
        outcome = control.skip_step(
            TaskStepSkipCommand(
                authority=authority,
                step=self._pending_step(authority.task_id, "optional.audit"),
                reason_code="not_applicable",
                skipped_at=_NOW,
            )
        )
        self.assertIs(TaskExecutionMutationOutcome.APPLIED, outcome)

    def test_duplicate_step_intent_requires_exact_same_command(self) -> None:
        """只有相同 Authority、定义、版本、幂等键和时间才是可识别的 Intent 重放。"""

        control = StrictTaskControlFake(FakeClock(_NOW))
        authority = _claim(control, _request("task-step-intent", "step-intent"))
        control.start(authority, started_at=_NOW)
        step = self._pending_step(authority.task_id, "rag.generate")
        command = TaskStepIntentCommand(
            authority=authority,
            step=step,
            intent_at=_NOW,
        )
        self.assertIs(
            TaskExecutionMutationOutcome.APPLIED,
            control.begin_step(command),
        )
        self.assertIs(
            TaskExecutionMutationOutcome.DUPLICATE_STEP_INTENT,
            control.begin_step(command),
        )
        self.assertIs(
            TaskExecutionMutationOutcome.INVALID_STATE,
            control.begin_step(
                replace(
                    command,
                    intent_at="2026-08-12T00:00:01.000000Z",
                )
            ),
        )
        self.assertIsNotNone(control.get_step(authority.task_id, step.step_key))
        self.assertIsNotNone(
            control.get_step_attempt(authority.task_id, step.step_key, 1)
        )

    def test_recovery_intent_observation_and_retry_preserve_unknown_attempt(self) -> None:
        """三段式恢复只重置 Step 投影，并由普通执行权创建新 Step Attempt。"""

        control = StrictTaskControlFake(FakeClock(_NOW))
        request = _request("task-recovery-v4", "recovery-v4")
        authority = _claim(control, request)
        self.assertIs(
            TaskExecutionMutationOutcome.APPLIED,
            control.start(authority, started_at=_NOW),
        )
        step = self._pending_step(authority.task_id, "rag.generate")
        self.assertIs(
            TaskExecutionMutationOutcome.APPLIED,
            control.begin_step(
                TaskStepIntentCommand(
                    authority=authority,
                    step=step,
                    intent_at=_NOW,
                )
            ),
        )
        self.assertIs(
            TaskExecutionMutationOutcome.APPLIED,
            control.complete_step(
                TaskStepCompletionCommand(
                    authority=authority,
                    step_key=step.step_key,
                    step_attempt_no=1,
                    transition=TaskStepTransition.MARK_OUTCOME_UNKNOWN,
                    checkpoint=None,
                    error_code="provider_outcome_unknown",
                    completed_at=_NOW,
                    recovery_isolation=TaskRecoveryIsolation(
                        case_id="case-recovery-v4",
                        reason_code="provider_outcome_unknown",
                        policy_version="report-recovery-v1",
                    ),
                )
            ),
        )
        recovery_claim = control.claim_case(
            TaskRecoveryClaimRequest(
                case_id="case-recovery-v4",
                generation=1,
                owner_id="start-id/100/recovery/worker-0",
                lease_token="recovery-token-1",
                claimed_at=_NOW,
                lease_expires_at=_LEASE_END,
            )
        )
        self.assertIs(TaskRecoveryMutationOutcome.APPLIED, recovery_claim.outcome)
        assert recovery_claim.authority is not None

        operation = TaskRecoveryOperation(
            operation_id="operation-recovery-v4",
            case_id="case-recovery-v4",
            generation=1,
            recovery_fencing_token=recovery_claim.authority.fencing_token,
            kind=RecoveryOperationKind.PROBE,
            step_key=step.step_key,
            idempotency_key="case-recovery-v4:rag.generate:probe",
            intent_digest="a" * 64,
            external_ref="provider:request-v4",
            state=RecoveryOperationState.INTENT_RECORDED,
            intent_at=_NOW,
        )
        operation_command = TaskRecoveryOperationIntentCommand(
            authority=recovery_claim.authority,
            operation=operation,
        )
        self.assertIs(
            TaskRecoveryMutationOutcome.APPLIED,
            control.begin_operation(operation_command),
        )
        self.assertIs(
            TaskRecoveryMutationOutcome.DUPLICATE_OPERATION,
            control.begin_operation(operation_command),
        )
        observation = TaskRecoveryObservation(
            observation_id="observation-recovery-v4",
            operation_id=operation.operation_id,
            case_id=operation.case_id,
            generation=operation.generation,
            recovery_fencing_token=recovery_claim.authority.fencing_token,
            kind=RecoveryObservationKind.NO_EFFECT_CONFIRMED,
            evidence_digest="b" * 64,
            observed_at=_NOW,
            step_key=step.step_key,
            external_ref=operation.external_ref,
        )
        self.assertIs(
            TaskRecoveryMutationOutcome.APPLIED,
            control.append_observation(recovery_claim.authority, observation),
        )

        task = control.get_task(authority.task_id)
        current_step = control.get_step(authority.task_id, step.step_key)
        self.assertIsNotNone(task)
        self.assertIsNotNone(current_step)
        assert task is not None and current_step is not None
        resolution = TaskRecoveryStepResolution(
            source_step_key=step.step_key,
            source_step_attempt_no=1,
            expected_step_row_version=current_step.row_version,
            operation_id=operation.operation_id,
            observation_id=observation.observation_id,
            evidence_digest=observation.evidence_digest,
            target_transition=TaskStepTransition.RETRY_AUTHORIZED,
        )
        decision = TaskRecoveryDecision(
            decision_id="decision-recovery-v4",
            task_id=task.task_id,
            case_id=operation.case_id,
            generation=1,
            recovery_fencing_token=recovery_claim.authority.fencing_token,
            expected_task_row_version=task.row_version,
            source_attempt_no=authority.attempt_no,
            source_fencing_token=authority.fencing_token,
            kind=RecoveryDecisionKind.RETRY_AUTHORIZED,
            evidence_digest=observation.evidence_digest,
            reason_code="no_effect_confirmed",
            policy_version="report-recovery-v1",
            actor_marker="automatic/recovery-0",
            decided_at=_NOW,
            retry_from_step_key=step.step_key,
            step_resolution=resolution,
        )
        self.assertIs(
            TaskRecoveryMutationOutcome.APPLIED,
            control.decide_if_current(recovery_claim.authority, decision),
        )
        reset_step = control.get_step(authority.task_id, step.step_key)
        old_attempt = control.get_step_attempt(authority.task_id, step.step_key, 1)
        self.assertIsNotNone(reset_step)
        self.assertIsNotNone(old_attempt)
        assert reset_step is not None and old_attempt is not None
        self.assertIs(TaskStepState.PENDING, reset_step.state)
        self.assertIs(TaskStepState.OUTCOME_UNKNOWN, old_attempt.state)

        # Recovery Decision 不创建业务 Attempt；只有新 claim/start 后的标准 BEGIN 才产生第 2 次。
        second_claim = control.claim(
            TaskClaimRequest(
                task_id=authority.task_id,
                task_type=request.task_type,
                owner=TaskOwnerIdentity(
                    instance_start_id="12345678-1234-4234-8234-123456789abc",
                    process_id=100,
                    executor_name="report",
                    worker_slot="worker-1",
                ),
                lease_token="lease-task-recovery-v4-second",
                claimed_at=_NOW,
                lease_expires_at=_LEASE_END,
            )
        )
        self.assertIs(TaskExecutionMutationOutcome.APPLIED, second_claim.outcome)
        assert second_claim.attempt is not None
        second_authority = second_claim.attempt.authority
        control.start(second_authority, started_at=_NOW)
        self.assertIs(
            TaskExecutionMutationOutcome.APPLIED,
            control.begin_step(
                TaskStepIntentCommand(
                    authority=second_authority,
                    step=reset_step,
                    intent_at=_NOW,
                )
            ),
        )
        self.assertIsNotNone(
            control.get_step_attempt(authority.task_id, step.step_key, 2)
        )
        self.assertIs(TaskStepState.OUTCOME_UNKNOWN, old_attempt.state)

    def test_analysis_batch_identity_is_complete_continuous_and_not_reordered(self) -> None:
        control = StrictTaskControlFake(FakeClock(_NOW))
        requests = tuple(
            _file_request(
                f"task-file-{sequence}",
                f"file-{sequence}",
                batch_id="analysis-batch-1",
                sequence=sequence,
            )
            for sequence in range(1, 4)
        )

        results = control.admit_many(requests)
        self.assertTrue(
            all(item.outcome is TaskAdmissionOutcome.ACCEPTED for item in results)
        )

        out_of_order = (requests[1], requests[0], requests[2])
        with self.assertRaisesRegex(ValueError, "请求顺序一致"):
            control.admit_many(out_of_order)

        with self.assertRaisesRegex(ValueError, "唯一 batch_id"):
            control.admit_many(
                (
                    _file_request(
                        "task-other-1",
                        "other-1",
                        batch_id="batch-a",
                        sequence=1,
                    ),
                    _file_request(
                        "task-other-2",
                        "other-2",
                        batch_id="batch-b",
                        sequence=2,
                    ),
                )
            )

    def test_lost_authority_and_expired_lease_are_distinct(self) -> None:
        clock = FakeClock(_NOW)
        control = StrictTaskControlFake(clock)
        authority = _claim(control, _request("task-auth", "auth"))
        self.assertEqual(
            control.start(authority, started_at=_NOW),
            TaskExecutionMutationOutcome.APPLIED,
        )

        stale_authority = replace(authority, lease_token="different-owner-token")
        stale_progress = TaskProgressCommand(
            authority=stale_authority,
            progress=0.5,
            message="stale",
            public_status="processing",
            updated_at=_NOW,
        )
        self.assertEqual(
            control.update_progress(stale_progress),
            TaskExecutionMutationOutcome.AUTHORITY_LOST,
        )

        clock.advance(seconds=30)
        expired_progress = replace(stale_progress, authority=authority, message="expired")
        self.assertEqual(
            control.update_progress(expired_progress),
            TaskExecutionMutationOutcome.LEASE_EXPIRED,
        )

    def test_duplicate_terminal_is_not_reported_as_success(self) -> None:
        control = StrictTaskControlFake(FakeClock(_NOW))
        authority = _claim(control, _request("task-terminal", "terminal"))
        self.assertEqual(
            control.start(authority, started_at=_NOW),
            TaskExecutionMutationOutcome.APPLIED,
        )
        command = TaskTerminalCommand(
            authority=authority,
            transition=TaskTransition.BUSINESS_SUCCEEDED,
            public_status="completed",
            message="done",
            result_ref="result:task-terminal",
            completed_at="2026-08-12T00:00:01.000000Z",
        )
        self.assertEqual(control.finish(command), TaskExecutionMutationOutcome.APPLIED)
        self.assertEqual(
            control.finish(command),
            TaskExecutionMutationOutcome.DUPLICATE_TERMINAL,
        )

    def test_heartbeat_rotates_authority_expiry_without_reusing_old_capability(self) -> None:
        control = StrictTaskControlFake(FakeClock(_NOW))
        authority = _claim(control, _request("task-heartbeat", "heartbeat"))
        renewed_until = "2026-08-12T00:01:00.000000Z"
        result = control.heartbeat(
            TaskHeartbeatCommand(
                authority=authority,
                heartbeat_at="2026-08-12T00:00:05.000000Z",
                lease_expires_at=renewed_until,
            )
        )
        self.assertEqual(result.outcome, TaskExecutionMutationOutcome.APPLIED)
        self.assertIsNotNone(result.authority)
        assert result.authority is not None
        self.assertEqual(result.authority.lease_expires_at, renewed_until)

        old_progress = TaskProgressCommand(
            authority=authority,
            progress=0.1,
            message="old authority",
            public_status="processing",
            updated_at="2026-08-12T00:00:05.000000Z",
        )
        self.assertEqual(
            control.update_progress(old_progress),
            TaskExecutionMutationOutcome.AUTHORITY_LOST,
        )

    def test_poisoned_return_is_exposed_without_fake_normalization(self) -> None:
        control = StrictTaskControlFake(FakeClock(_NOW))
        poison = {"unexpected": "mapping"}
        control.poison_next_return("admit_one", poison)
        self.assertIs(control.admit_one(_request("task-poison", "poison")), poison)

    def test_recovery_cleanup_callback_and_key_isolation_matrix(self) -> None:
        scenarios = (
            ("recovery_same_key", TaskAdmissionOutcome.ACTIVE_TASK_CONFLICT),
            ("recovery_different_key", TaskAdmissionOutcome.ACCEPTED),
            ("terminal_cleanup_same_key", TaskAdmissionOutcome.ACCEPTED),
            ("callback_unknown_same_key", TaskAdmissionOutcome.CALLBACK_OUTCOME_UNKNOWN),
            ("callback_unknown_different_key", TaskAdmissionOutcome.ACCEPTED),
        )
        for scenario, expected in scenarios:
            with self.subTest(scenario=scenario):
                control = StrictTaskControlFake(FakeClock(_NOW))
                guarded_ref = TaskBusinessRef("report", "guarded")
                if scenario.startswith("recovery"):
                    control.put_task(
                        TaskRecord(
                            task_id=TaskId("old-recovery"),
                            task_type="report",
                            business_ref=guarded_ref,
                            state=TaskState.RECOVERY_REQUIRED,
                            current_attempt_no=1,
                            fencing_token=1,
                            row_version=2,
                            recovery_generation=1,
                            current_recovery_case_id="case-1",
                            recovery_reason_code="outcome_unknown",
                        )
                    )
                elif scenario == "terminal_cleanup_same_key":
                    terminal_id = TaskId("old-terminal")
                    control.put_task(
                        TaskRecord(
                            task_id=terminal_id,
                            task_type="report",
                            business_ref=guarded_ref,
                            state=TaskState.SUCCEEDED,
                            current_attempt_no=1,
                            fencing_token=1,
                            row_version=2,
                            recovery_generation=0,
                        )
                    )
                    control.set_cleanup_unknown(terminal_id)
                else:
                    control.set_callback_conflict(
                        guarded_ref,
                        CallbackAdmissionConflict.OUTCOME_UNKNOWN,
                    )

                same_key = scenario.endswith("same_key")
                business_key = "guarded" if same_key else "unrelated"
                result = control.admit_one(_request(f"new-{scenario}", business_key))
                self.assertEqual(result.outcome, expected)

    def test_batch_conflict_rolls_back_other_items(self) -> None:
        control = StrictTaskControlFake(FakeClock(_NOW))
        blocked_ref = TaskBusinessRef("report", "blocked")
        control.put_task(
            TaskRecord(
                task_id=TaskId("old-running"),
                task_type="report",
                business_ref=blocked_ref,
                state=TaskState.RUNNING,
                current_attempt_no=1,
                fencing_token=1,
                row_version=2,
                recovery_generation=0,
            )
        )
        blocked = _request("new-blocked", "blocked")
        otherwise_allowed = _request("new-allowed", "allowed")
        results = control.admit_many((blocked, otherwise_allowed))
        self.assertEqual(results[0].outcome, TaskAdmissionOutcome.ACTIVE_TASK_CONFLICT)
        self.assertEqual(results[1].outcome, TaskAdmissionOutcome.BATCH_REJECTED)
        self.assertIsNone(control.get_task(otherwise_allowed.task_id))


if __name__ == "__main__":
    unittest.main()
