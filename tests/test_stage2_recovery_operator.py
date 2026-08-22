"""阶段 2-7 恢复运维入口的默认只读和严格参数验收。"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.modules.tasks.application import (
    RecoveryCaseInspection,
    RecoveryOperatorAction,
    StrictRecoveryDecisionCommand,
)
from app.modules.tasks.domain import TaskId
from app.modules.tasks.ports import TaskRecoveryMutationOutcome
from scripts import reconcile_stage2_task


_TASK_ID = "operator-task-1"
_CASE_ID = "operator-case-1"
_DIGEST = "a" * 64


class _RecordingOperatorService:
    """只记录 Application 调用，确保 CLI 自身不偷偷执行第二条写路径。"""

    def __init__(self) -> None:
        self.inspect_calls: list[tuple[TaskId, str]] = []
        self.execute_calls: list[StrictRecoveryDecisionCommand] = []

    def inspect(self, task_id: TaskId, case_id: str) -> RecoveryCaseInspection:
        self.inspect_calls.append((task_id, case_id))
        return RecoveryCaseInspection(
            task_id=task_id.value,
            task_type="report",
            task_state="recovery_required",
            task_row_version=7,
            case_id=case_id,
            generation=1,
            case_state="open",
            source_attempt_no=1,
            source_fencing_token=3,
            recovery_fencing_token=0,
            step_states=(("artifact.scope.begin", "outcome_unknown", 1, 2),),
            operation_count=1,
            observation_count=1,
        )

    def execute(
        self,
        command: StrictRecoveryDecisionCommand,
    ) -> TaskRecoveryMutationOutcome:
        self.execute_calls.append(command)
        return TaskRecoveryMutationOutcome.APPLIED


class Stage2RecoveryOperatorCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary.name) / "task-control.sqlite3"
        # CLI 会先做路径类型门禁；测试用空文件足以证明参数和 Application 调用方向，
        # 不把生产 Schema 初始化混入运维入口单元测试。
        self.database_path.touch()
        self.service = _RecordingOperatorService()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_default_mode_only_inspects_and_emits_dry_run_payload(self) -> None:
        output = StringIO()
        with patch.object(
            reconcile_stage2_task,
            "_service",
            return_value=self.service,
        ), redirect_stdout(output):
            exit_code = reconcile_stage2_task.main(
                (
                    "--db-path",
                    str(self.database_path),
                    "--task-id",
                    _TASK_ID,
                    "--case-id",
                    _CASE_ID,
                )
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("dry_run", payload["mode"])
        self.assertTrue(payload["found"])
        self.assertEqual(
            [(TaskId(_TASK_ID), _CASE_ID)],
            self.service.inspect_calls,
        )
        self.assertEqual([], self.service.execute_calls)

    def test_write_mode_rejects_missing_expected_identity_before_execute(self) -> None:
        # argparse.error 会以稳定退出码 2 拒绝缺失 generation/row version/fencing/
        # operator/reason/evidence 的写请求，且不得进入 Application execute。
        with patch.object(
            reconcile_stage2_task,
            "_service",
            return_value=self.service,
        ), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            reconcile_stage2_task.main(
                (
                    "--db-path",
                    str(self.database_path),
                    "--task-id",
                    _TASK_ID,
                    "--case-id",
                    _CASE_ID,
                    "--write",
                    "--action",
                    "keep_quarantined",
                )
            )

        self.assertEqual(2, raised.exception.code)
        self.assertEqual([], self.service.execute_calls)

    def test_explicit_write_passes_complete_snapshot_bound_command(self) -> None:
        output = StringIO()
        with patch.object(
            reconcile_stage2_task,
            "_service",
            return_value=self.service,
        ), redirect_stdout(output):
            exit_code = reconcile_stage2_task.main(
                (
                    "--db-path",
                    str(self.database_path),
                    "--task-id",
                    _TASK_ID,
                    "--case-id",
                    _CASE_ID,
                    "--write",
                    "--action",
                    "keep_quarantined",
                    "--generation",
                    "1",
                    "--expected-task-row-version",
                    "7",
                    "--source-attempt-no",
                    "1",
                    "--source-fencing-token",
                    "3",
                    "--expected-recovery-fencing-token",
                    "0",
                    "--operator",
                    "operator:test-suite",
                    "--reason-code",
                    "manual_evidence_review",
                    "--evidence-digest",
                    _DIGEST,
                )
            )

        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(self.service.execute_calls))
        command = self.service.execute_calls[0]
        self.assertEqual(TaskId(_TASK_ID), command.task_id)
        self.assertIs(RecoveryOperatorAction.KEEP_QUARANTINED, command.action)
        self.assertEqual(7, command.expected_task_row_version)
        self.assertEqual(3, command.source_fencing_token)
        self.assertEqual(
            {"action": "keep_quarantined", "mode": "write", "outcome": "applied"},
            json.loads(output.getvalue()),
        )


class Stage2RecoveryOperatorCommandTests(unittest.TestCase):
    def test_retry_action_requires_exact_step_operation_and_observation(self) -> None:
        with self.assertRaisesRegex(ValueError, "精确 Step/Operation/Observation"):
            StrictRecoveryDecisionCommand(
                task_id=TaskId(_TASK_ID),
                case_id=_CASE_ID,
                generation=1,
                expected_task_row_version=7,
                source_attempt_no=1,
                source_fencing_token=3,
                expected_recovery_fencing_token=0,
                operator="operator:test-suite",
                reason_code="manual_evidence_review",
                evidence_digest=_DIGEST,
                action=RecoveryOperatorAction.RETRY_AUTHORIZED,
                decided_at="2026-08-21T00:00:00.000000Z",
                retry_from_step_key="artifact.scope.begin",
                source_step_attempt_no=1,
                expected_step_row_version=2,
            )

    def test_evidence_digest_must_be_sha256_hex(self) -> None:
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            StrictRecoveryDecisionCommand(
                task_id=TaskId(_TASK_ID),
                case_id=_CASE_ID,
                generation=1,
                expected_task_row_version=7,
                source_attempt_no=1,
                source_fencing_token=3,
                expected_recovery_fencing_token=0,
                operator="operator:test-suite",
                reason_code="manual_evidence_review",
                evidence_digest="not-a-digest",
                action=RecoveryOperatorAction.KEEP_QUARANTINED,
                decided_at="2026-08-21T00:00:00.000000Z",
            )


if __name__ == "__main__":
    unittest.main()
