"""阶段 2-1：Task Control 新 Port 的纯边界与严格 DTO 测试。"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
import unittest

from app.modules.tasks.domain import (
    RecoveryClassification,
    RecoveryAuthority,
    RecoveryOperationKind,
    RecoveryOperationState,
    StepEffectKind,
    StepReplayPolicy,
    TaskBatchRef,
    TaskBusinessRef,
    TaskExecutionAuthority,
    TaskId,
    TaskOwnerIdentity,
    TaskRecord,
    TaskRecoveryCandidate,
    TaskRecoveryIsolation,
    TaskRecoveryOperation,
    TaskState,
    TaskStep,
    TaskStepCheckpoint,
    TaskStepState,
    TaskStepTransition,
    TaskTransition,
)
from app.modules.tasks.ports import (
    ClockPort,
    TaskAdmissionRequest,
    TaskClaimRequest,
    TaskExecutionPort,
    TaskHeartbeatCommand,
    TaskRecoveryPort,
    TaskRecoveryClassificationCommand,
    TaskRecoveryClassificationResult,
    TaskRecoveryMutationOutcome,
    TaskRecoveryClaimRequest,
    TaskRecoveryOperationIntentCommand,
    TaskRecoveryHeartbeatCommand,
    TaskRecoveryHeartbeatResult,
    TaskStepCompletionCommand,
    TaskStepSkipCommand,
    TaskTerminalCommand,
    require_persisted_utc,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PORT_ROOT = _PROJECT_ROOT / "app" / "modules" / "tasks" / "ports"
_NEW_PORT_FILES = (
    "clock.py",
    "task_admission.py",
    "task_execution.py",
    "task_recovery.py",
    "task_events.py",
    "callback_delivery_control.py",
    "runtime.py",
    "unit_of_work.py",
)
_FORBIDDEN_IMPORT_PREFIXES = (
    "sqlite3",
    "threading",
    "flask",
    "celery",
    "app.modules.tasks.adapters",
    "app.modules.report",
    "app.modules.weaponry",
    "app.modules.analysis",
)


class TaskControlPortTests(unittest.TestCase):
    """锁定供应商无关、可替换且不读取系统时间的抽象边界。"""

    def test_persisted_utc_is_strict(self) -> None:
        value = "2026-08-12T12:00:00.000001Z"
        self.assertEqual(require_persisted_utc(value), value)
        for invalid in (
            "2026-08-12T12:00:00Z",
            "2026-08-12 12:00:00.000001",
            "2026-08-12T20:00:00.000001+08:00",
        ):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                require_persisted_utc(invalid)

    def test_admission_and_claim_reject_ambiguous_inputs(self) -> None:
        business_ref = TaskBusinessRef("report", "101")
        task_id = TaskId("task-101")
        with self.assertRaises(ValueError):
            TaskAdmissionRequest(
                task_id=task_id,
                task_type="report",
                business_ref=business_ref,
                input_schema_version=1,
                input_snapshot=object(),
                input_payload={},
                public_request_payload={},
                initial_public_status="waiting",
                trace_id="trace-101",
                accepted_at="2026-08-12T12:00:00Z",
            )
        with self.assertRaises(ValueError):
            TaskClaimRequest(
                task_id=task_id,
                task_type="report",
                owner=TaskOwnerIdentity(
                    instance_start_id="12345678-1234-4234-8234-123456789abc",
                    process_id=1,
                    executor_name="report",
                    worker_slot="worker-0",
                ),
                lease_token="secret-token",
                claimed_at="2026-08-12T12:00:30.000000Z",
                lease_expires_at="2026-08-12T12:00:30.000000Z",
            )

        with self.assertRaisesRegex(ValueError, "business_ref.business_type"):
            TaskAdmissionRequest(
                task_id=task_id,
                task_type="file",
                business_ref=business_ref,
                input_schema_version=1,
                input_snapshot=object(),
                input_payload={},
                public_request_payload={},
                initial_public_status="waiting",
                trace_id="trace-101",
                accepted_at="2026-08-12T12:00:00.000000Z",
                batch=TaskBatchRef(batch_id="analysis-batch-1", sequence=1),
            )

    def test_recovery_lease_tokens_are_hidden_from_repr(self) -> None:
        request = TaskRecoveryClaimRequest(
            case_id="case-1",
            generation=1,
            owner_id="instance/recovery/worker-0",
            lease_token="recovery-secret-token",
            claimed_at="2026-08-12T12:00:00.000000Z",
            lease_expires_at="2026-08-12T12:00:30.000000Z",
        )
        authority = RecoveryAuthority(
            case_id="case-1",
            generation=1,
            owner_id="instance/recovery/worker-0",
            lease_token="recovery-secret-token",
            fencing_token=1,
            lease_expires_at="2026-08-12T12:00:30.000000Z",
        )
        self.assertNotIn("recovery-secret-token", repr(request))
        self.assertNotIn("recovery-secret-token", repr(authority))

    def test_file_admission_requires_explicit_batch_identity(self) -> None:
        common = {
            "task_id": TaskId("task-file-1"),
            "task_type": "file",
            "business_ref": TaskBusinessRef("file", "file-1"),
            "input_schema_version": 1,
            "input_snapshot": object(),
            "input_payload": {},
            "public_request_payload": {},
            "initial_public_status": "waiting",
            "trace_id": "trace-file-1",
            "accepted_at": "2026-08-12T12:00:00.000000Z",
        }
        with self.assertRaisesRegex(ValueError, "必须且只能携带批次身份"):
            TaskAdmissionRequest(**common)

        request = TaskAdmissionRequest(
            **common,
            batch=TaskBatchRef(batch_id="analysis-batch-1", sequence=1),
        )
        self.assertEqual("analysis-batch-1", request.batch.batch_id)

        report_common = dict(common)
        report_common.update(
            task_id=TaskId("task-report-1"),
            task_type="report",
            business_ref=TaskBusinessRef("report", "report-1"),
        )
        with self.assertRaisesRegex(ValueError, "必须且只能携带批次身份"):
            TaskAdmissionRequest(
                **report_common,
                batch=TaskBatchRef(batch_id="invalid-report-batch", sequence=1),
            )

    @staticmethod
    def _recovery_candidate() -> TaskRecoveryCandidate:
        task = TaskRecord(
            task_id=TaskId("task-recovery-1"),
            task_type="report",
            business_ref=TaskBusinessRef("report", "report-recovery-1"),
            state=TaskState.RUNNING,
            current_attempt_no=1,
            fencing_token=1,
            row_version=2,
            recovery_generation=0,
        )
        return TaskRecoveryCandidate(
            task=task,
            source_attempt_no=1,
            source_fencing_token=1,
            reason_code="lease_expired",
            latest_is_current=True,
            evidence_digest="a" * 64,
        )

    def test_recovery_classification_command_covers_all_five_actions(self) -> None:
        candidate = self._recovery_candidate()
        now = "2026-08-12T12:00:00.000000Z"
        later = "2026-08-12T12:00:30.000000Z"
        cases = (
            (RecoveryClassification.RETRY_SAFE, "", later),
            (RecoveryClassification.DEFER, "", later),
            (RecoveryClassification.RECONCILE_REQUIRED, "case-reconcile", ""),
            (RecoveryClassification.FINALIZE_FROM_CHECKPOINT, "case-finalize", ""),
            (RecoveryClassification.MARK_STALE, "", ""),
        )
        for classification, case_id, next_action_at in cases:
            with self.subTest(classification=classification):
                command = TaskRecoveryClassificationCommand(
                    candidate=candidate,
                    classification=classification,
                    policy_version="report-recovery-v1",
                    classified_at=now,
                    case_id=case_id,
                    next_action_at=next_action_at,
                )
                self.assertIs(classification, command.classification)

        with self.assertRaisesRegex(ValueError, "case_id"):
            TaskRecoveryClassificationCommand(
                candidate=candidate,
                classification=RecoveryClassification.RECONCILE_REQUIRED,
                policy_version="report-recovery-v1",
                classified_at=now,
            )
        with self.assertRaisesRegex(ValueError, "next_action_at"):
            TaskRecoveryClassificationCommand(
                candidate=candidate,
                classification=RecoveryClassification.DEFER,
                policy_version="report-recovery-v1",
                classified_at=now,
            )
        with self.assertRaises(ValueError):
            TaskRecoveryClassificationResult(
                outcome=TaskRecoveryMutationOutcome.APPLIED,
                classification=RecoveryClassification.RECONCILE_REQUIRED,
            )

    def test_terminal_command_requires_execution_authority_and_business_terminal(self) -> None:
        authority = TaskExecutionAuthority(
            task_id=TaskId("task-terminal"),
            attempt_no=1,
            owner_id="start-id/1/report/worker-0",
            lease_token="secret-token",
            fencing_token=1,
            lease_expires_at="2026-08-12T12:00:30.000000Z",
        )
        command = TaskTerminalCommand(
            authority=authority,
            transition=TaskTransition.BUSINESS_SUCCEEDED,
            public_status="completed",
            message="done",
            result_ref="result:task-terminal",
            completed_at="2026-08-12T12:00:20.000000Z",
        )
        self.assertEqual(command.transition, TaskTransition.BUSINESS_SUCCEEDED)
        with self.assertRaisesRegex(ValueError, "严格晚于当前"):
            TaskHeartbeatCommand(
                authority=authority,
                heartbeat_at="2026-08-12T12:00:10.000000Z",
                lease_expires_at=authority.lease_expires_at,
            )
        with self.assertRaises(ValueError):
            TaskTerminalCommand(
                authority=authority,
                transition=TaskTransition.ISOLATE_FOR_RECOVERY,
                public_status="failed",
                message="unknown",
                result_ref="",
                completed_at="2026-08-12T12:00:20.000000Z",
            )

    def test_step_completion_is_explicit_and_unknown_requires_atomic_isolation(self) -> None:
        authority = TaskExecutionAuthority(
            task_id=TaskId("task-step"),
            attempt_no=1,
            owner_id="start-id/1/report/worker-0",
            lease_token="secret-token",
            fencing_token=1,
            lease_expires_at="2026-08-12T12:00:30.000000Z",
        )
        checkpoint = TaskStepCheckpoint(
            code="result_persisted",
            result_ref="result:task-step",
            result_digest="b" * 64,
        )
        succeeded = TaskStepCompletionCommand(
            authority=authority,
            step_key="terminal.commit",
            step_attempt_no=1,
            transition=TaskStepTransition.SUCCEED,
            checkpoint=checkpoint,
            error_code="",
            completed_at="2026-08-12T12:00:20.000000Z",
        )
        self.assertIs(TaskStepTransition.SUCCEED, succeeded.transition)

        with self.assertRaisesRegex(ValueError, "Recovery Isolation"):
            TaskStepCompletionCommand(
                authority=authority,
                step_key="rag.generate",
                step_attempt_no=1,
                transition=TaskStepTransition.MARK_OUTCOME_UNKNOWN,
                checkpoint=None,
                error_code="provider_outcome_unknown",
                completed_at="2026-08-12T12:00:20.000000Z",
            )

        unknown = TaskStepCompletionCommand(
            authority=authority,
            step_key="rag.generate",
            step_attempt_no=1,
            transition=TaskStepTransition.MARK_OUTCOME_UNKNOWN,
            checkpoint=None,
            error_code="provider_outcome_unknown",
            completed_at="2026-08-12T12:00:20.000000Z",
            recovery_isolation=TaskRecoveryIsolation(
                case_id="case-task-step",
                reason_code="provider_outcome_unknown",
                policy_version="report-recovery-v1",
            ),
        )
        self.assertIsNotNone(unknown.recovery_isolation)

        pending = TaskStep(
            task_id=authority.task_id,
            step_key="optional.audit",
            definition_version=1,
            effect_kind=StepEffectKind.LOCAL_WRITE,
            replay_policy=StepReplayPolicy.IDEMPOTENT_AFTER_PROBE,
            state=TaskStepState.PENDING,
            current_step_attempt_no=0,
            idempotency_key="task-step:optional.audit",
            checkpoint=None,
            row_version=0,
        )
        skipped = TaskStepSkipCommand(
            authority=authority,
            step=pending,
            reason_code="not_applicable",
            skipped_at="2026-08-12T12:00:20.000000Z",
        )
        self.assertEqual("not_applicable", skipped.reason_code)

    def test_recovery_heartbeat_rotates_authority_expiry(self) -> None:
        authority = RecoveryAuthority(
            case_id="case-heartbeat",
            generation=1,
            owner_id="start-id/1/recovery/worker-0",
            lease_token="recovery-secret-token",
            fencing_token=1,
            lease_expires_at="2026-08-12T12:00:30.000000Z",
        )
        command = TaskRecoveryHeartbeatCommand(
            authority=authority,
            heartbeat_at="2026-08-12T12:00:10.000000Z",
            lease_expires_at="2026-08-12T12:00:40.000000Z",
        )
        with self.assertRaisesRegex(ValueError, "严格晚于当前"):
            TaskRecoveryHeartbeatCommand(
                authority=authority,
                heartbeat_at="2026-08-12T12:00:10.000000Z",
                lease_expires_at=authority.lease_expires_at,
            )
        renewed = RecoveryAuthority(
            case_id=authority.case_id,
            generation=authority.generation,
            owner_id=authority.owner_id,
            lease_token=authority.lease_token,
            fencing_token=authority.fencing_token,
            lease_expires_at=command.lease_expires_at,
        )
        result = TaskRecoveryHeartbeatResult(
            outcome=TaskRecoveryMutationOutcome.APPLIED,
            authority=renewed,
        )
        self.assertEqual(command.lease_expires_at, result.authority.lease_expires_at)

    def test_recovery_operation_intent_is_bound_to_current_authority(self) -> None:
        authority = RecoveryAuthority(
            case_id="case-operation",
            generation=1,
            owner_id="start-id/1/recovery/worker-0",
            lease_token="recovery-secret-token",
            fencing_token=2,
            lease_expires_at="2026-08-12T12:00:30.000000Z",
        )
        operation = TaskRecoveryOperation(
            operation_id="operation-probe",
            case_id=authority.case_id,
            generation=authority.generation,
            recovery_fencing_token=authority.fencing_token,
            kind=RecoveryOperationKind.PROBE,
            step_key="rag.generate",
            idempotency_key="case-operation:rag.generate:probe",
            intent_digest="a" * 64,
            external_ref="provider:request-1",
            state=RecoveryOperationState.INTENT_RECORDED,
            intent_at="2026-08-12T12:00:10.000000Z",
        )
        command = TaskRecoveryOperationIntentCommand(
            authority=authority,
            operation=operation,
        )
        self.assertEqual(authority.case_id, command.operation.case_id)
        with self.assertRaisesRegex(ValueError, "Authority 身份不一致"):
            TaskRecoveryOperationIntentCommand(
                authority=replace(authority, fencing_token=3),
                operation=operation,
            )

    def test_protocols_are_owned_by_ports(self) -> None:
        self.assertEqual(ClockPort.__module__, "app.modules.tasks.ports.clock")
        self.assertEqual(TaskExecutionPort.__module__, "app.modules.tasks.ports.task_execution")
        self.assertEqual(TaskRecoveryPort.__module__, "app.modules.tasks.ports.task_recovery")
        self.assertIn("get_step", TaskExecutionPort.__dict__)
        self.assertIn("get_step_attempt", TaskExecutionPort.__dict__)

    def test_new_ports_have_no_infrastructure_or_system_clock_imports(self) -> None:
        for filename in _NEW_PORT_FILES:
            with self.subTest(filename=filename):
                source_path = _PORT_ROOT / filename
                syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
                imported_modules: list[str] = []
                for node in ast.walk(syntax_tree):
                    if isinstance(node, ast.Import):
                        imported_modules.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported_modules.append(node.module)
                for module_name in imported_modules:
                    self.assertFalse(
                        module_name.startswith(_FORBIDDEN_IMPORT_PREFIXES),
                        f"{filename} 禁止导入 {module_name}",
                    )
                # 只检查实际调用节点，注释中说明禁令的文字不应触发误报。
                calls = {
                    f"{node.func.value.id}.{node.func.attr}"
                    for node in ast.walk(syntax_tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                }
                self.assertNotIn("datetime.now", calls)
                self.assertNotIn("time.time", calls)


if __name__ == "__main__":
    unittest.main()
