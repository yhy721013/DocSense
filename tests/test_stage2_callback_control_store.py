"""阶段 2-4 第 1 步 Callback Delivery Control Store 离线合同测试。"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTransactionManager,
    bootstrap_task_control_database,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.domain import (
    TaskBusinessRef,
    TaskId,
    TaskOwnerIdentity,
    TaskTransition,
)
from app.modules.tasks.ports import (
    CallbackAcquireCommand,
    CallbackAcquireOutcome,
    CallbackCompleteCommand,
    CallbackControlMutationOutcome,
    CallbackDeliveryOutcome,
    CallbackDeliveryTrigger,
    CallbackEligibilityCommand,
    CallbackGuardSweepCommand,
    CallbackHeartbeatCommand,
    CallbackReleaseOutcome,
    CallbackReleaseUnknownCommand,
    CallbackValidationCommand,
    CallbackValidationOutcome,
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
    TaskClaimRequest,
    TaskExecutionMutationOutcome,
    TaskTerminalCommand,
)


_T0 = "2026-08-13T00:00:00.000000Z"
_T1 = "2026-08-13T00:00:01.000000Z"
_T2 = "2026-08-13T00:00:02.000000Z"
_T3 = "2026-08-13T00:00:03.000000Z"
_T4 = "2026-08-13T00:00:04.000000Z"
_T5 = "2026-08-13T00:00:05.000000Z"
_T10 = "2026-08-13T00:00:10.000000Z"
_T20 = "2026-08-13T00:00:20.000000Z"
_T30 = "2026-08-13T00:00:30.000000Z"


def _request(task_id: str, business_key: str) -> TaskAdmissionRequest[dict[str, str]]:
    return TaskAdmissionRequest(
        task_id=TaskId(task_id),
        task_type="report",
        business_ref=TaskBusinessRef("report", business_key),
        input_schema_version=2,
        input_snapshot={"report_id": business_key},
        input_payload={"report_id": business_key},
        public_request_payload={"reportId": business_key},
        initial_public_status="0",
        trace_id=f"trace-{task_id}",
        accepted_at=_T0,
    )


def _owner() -> TaskOwnerIdentity:
    return TaskOwnerIdentity(
        instance_start_id="12345678-1234-4234-8234-123456789abc",
        process_id=101,
        executor_name="ReportExecutor",
        worker_slot="worker-0",
    )


class SQLiteCallbackControlStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        old_path = root / "old.sqlite3"
        self.database_path = root / "task-control-v2.sqlite3"
        sqlite3.connect(old_path).close()
        bootstrap = bootstrap_task_control_database(old_path, self.database_path)
        manager = SQLiteTransactionManager(
            SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
        )
        self.factories = build_sqlite_task_control_uow_factories(manager)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _admit_and_claim(self, request: TaskAdmissionRequest[object]):
        with self.factories.admission() as unit_of_work:
            admitted = unit_of_work.admission.admit_one(request)
            self.assertIs(TaskAdmissionOutcome.ACCEPTED, admitted.outcome)
            unit_of_work.commit()
        with self.factories.execution() as unit_of_work:
            claimed = unit_of_work.execution.claim(
                TaskClaimRequest(
                    task_id=request.task_id,
                    task_type="report",
                    owner=_owner(),
                    lease_token=f"task-lease-{request.task_id.value}",
                    claimed_at=_T1,
                    lease_expires_at=_T30,
                )
            )
            self.assertIs(TaskExecutionMutationOutcome.APPLIED, claimed.outcome)
            assert claimed.attempt is not None
            authority = claimed.attempt.authority
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.start(authority, started_at=_T2),
            )
            unit_of_work.commit()
        return authority

    def _finish_with_eligibility(self, request, authority, *, commit: bool = True) -> None:
        with self.factories.execution() as unit_of_work:
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.finish(
                    TaskTerminalCommand(
                        authority=authority,
                        transition=TaskTransition.BUSINESS_SUCCEEDED,
                        public_status="1",
                        message="生成成功",
                        result_ref=f"report-result:{request.business_ref.business_key}",
                        completed_at=_T3,
                    )
                ),
            )
            self.assertIs(
                CallbackControlMutationOutcome.APPLIED,
                unit_of_work.callback_delivery.mark_eligible(
                    CallbackEligibilityCommand(
                        authority=authority,
                        business_ref=request.business_ref,
                        eligible_at=_T3,
                    )
                ),
            )
            if commit:
                unit_of_work.commit()

    def _acquire_initial(self, request, *, token: str = "callback-lease-1"):
        with self.factories.callback_delivery() as unit_of_work:
            acquired = unit_of_work.callback_delivery.acquire(
                CallbackAcquireCommand(
                    task_id=request.task_id,
                    business_ref=request.business_ref,
                    trigger=CallbackDeliveryTrigger.INITIAL_DELIVERY,
                    lease_token=token,
                    acquired_at=_T4,
                    lease_expires_at=_T20,
                )
            )
            unit_of_work.commit()
        self.assertIs(CallbackAcquireOutcome.ACQUIRED, acquired.outcome)
        assert acquired.lease is not None
        return acquired.lease

    def test_terminal_and_callback_eligibility_share_one_rollback_boundary(self) -> None:
        request = _request("report-callback-rollback", "4201")
        authority = self._admit_and_claim(request)
        self._finish_with_eligibility(request, authority, commit=False)

        with closing(sqlite3.connect(self.database_path)) as connection:
            execution_state = connection.execute(
                "SELECT execution_state FROM llm_task_executions WHERE execution_id = ?",
                (request.task_id.value,),
            ).fetchone()[0]
            guard_count = connection.execute(
                "SELECT COUNT(*) FROM callback_delivery_guards WHERE business_type='report' AND business_key='4201'"
            ).fetchone()[0]
        self.assertEqual("running", execution_state)
        self.assertEqual(0, guard_count)

        self._finish_with_eligibility(request, authority)
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT owner_execution_id, state FROM callback_delivery_guards WHERE business_type='report' AND business_key='4201'"
            ).fetchone()
        self.assertEqual((request.task_id.value, "idle"), row)

    def test_claim_validate_complete_and_old_fencing_are_strict(self) -> None:
        request = _request("report-callback-success", "4202")
        authority = self._admit_and_claim(request)
        self._finish_with_eligibility(request, authority)
        lease = self._acquire_initial(request)

        with self.factories.callback_delivery() as unit_of_work:
            self.assertIs(
                CallbackValidationOutcome.VALID,
                unit_of_work.callback_delivery.validate(
                    CallbackValidationCommand(lease=lease, observed_at=_T5)
                ),
            )
            self.assertIs(
                CallbackControlMutationOutcome.APPLIED,
                unit_of_work.callback_delivery.complete(
                    CallbackCompleteCommand(
                        lease=lease,
                        outcome=CallbackDeliveryOutcome.SUCCESS,
                        detail="http_status=200",
                        completed_at=_T10,
                    )
                ),
            )
            unit_of_work.commit()

        with self.factories.callback_delivery() as unit_of_work:
            self.assertIs(
                CallbackControlMutationOutcome.AUTHORITY_LOST,
                unit_of_work.callback_delivery.complete(
                    CallbackCompleteCommand(
                        lease=lease,
                        outcome=CallbackDeliveryOutcome.SUCCESS,
                        detail="late duplicate",
                        completed_at=_T20,
                    )
                ),
            )
            unit_of_work.rollback()
        with closing(sqlite3.connect(self.database_path)) as connection:
            projection = connection.execute(
                "SELECT callback_status, callback_attempts FROM llm_tasks WHERE business_type='report' AND business_key='4202'"
            ).fetchone()
            events = connection.execute(
                "SELECT event_type, delivery_outcome FROM callback_delivery_attempt_events ORDER BY event_id"
            ).fetchall()
        self.assertEqual(("success", 1), projection)
        self.assertEqual(
            [("authorized", ""), ("completed", "success")],
            events,
        )

    def test_unknown_release_keeps_old_fact_but_unblocks_new_admission(self) -> None:
        request = _request("report-callback-unknown", "4203")
        authority = self._admit_and_claim(request)
        self._finish_with_eligibility(request, authority)
        lease = self._acquire_initial(request)
        with self.factories.callback_delivery() as unit_of_work:
            self.assertIs(
                CallbackControlMutationOutcome.APPLIED,
                unit_of_work.callback_delivery.complete(
                    CallbackCompleteCommand(
                        lease=lease,
                        outcome=CallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
                        detail="ReadTimeout",
                        completed_at=_T10,
                    )
                ),
            )
            unit_of_work.commit()

        blocked = _request("report-callback-blocked", "4203")
        with self.factories.admission() as unit_of_work:
            self.assertIs(
                TaskAdmissionOutcome.CALLBACK_OUTCOME_UNKNOWN,
                unit_of_work.admission.admit_one(blocked).outcome,
            )
            unit_of_work.rollback()
        with self.factories.callback_delivery() as unit_of_work:
            released = unit_of_work.callback_delivery.release_unknown(
                CallbackReleaseUnknownCommand(
                    business_ref=request.business_ref,
                    released_by="operator-4203",
                    reason="已确认旧 Worker 停止并隔离",
                    worker_stopped_confirmed=True,
                    released_at=_T20,
                )
            )
            unit_of_work.commit()
        self.assertIs(CallbackReleaseOutcome.RELEASED, released)

        accepted = _request("report-callback-reaccepted", "4203")
        with self.factories.admission() as unit_of_work:
            result = unit_of_work.admission.admit_one(accepted)
            unit_of_work.commit()
        self.assertIs(TaskAdmissionOutcome.ACCEPTED, result.outcome)
        with closing(sqlite3.connect(self.database_path)) as connection:
            old_status = connection.execute(
                "SELECT callback_status FROM llm_task_executions WHERE execution_id = ?",
                (request.task_id.value,),
            ).fetchone()[0]
            guard = connection.execute(
                "SELECT state, released_by FROM callback_delivery_guards WHERE business_type='report' AND business_key='4203'"
            ).fetchone()
        self.assertEqual("outcome_unknown", old_status)
        self.assertEqual(("idle", "operator-4203"), guard)

    def test_explicit_check_task_can_recover_unknown_without_second_guard(self) -> None:
        request = _request("report-callback-explicit", "4204")
        authority = self._admit_and_claim(request)
        self._finish_with_eligibility(request, authority)
        first_lease = self._acquire_initial(request)
        with self.factories.callback_delivery() as unit_of_work:
            unit_of_work.callback_delivery.complete(
                CallbackCompleteCommand(
                    lease=first_lease,
                    outcome=CallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
                    detail="ReadTimeout",
                    completed_at=_T10,
                )
            )
            unit_of_work.commit()
        with self.factories.callback_delivery() as unit_of_work:
            recovered = unit_of_work.callback_delivery.acquire(
                CallbackAcquireCommand(
                    task_id=request.task_id,
                    business_ref=request.business_ref,
                    trigger=CallbackDeliveryTrigger.EXPLICIT_CHECK_TASK_RECOVERY,
                    lease_token="callback-lease-2",
                    acquired_at=_T20,
                    lease_expires_at=_T30,
                    expected_callback_attempts=1,
                    request_trace_id="check-task-4204",
                )
            )
            unit_of_work.commit()
        self.assertIs(CallbackAcquireOutcome.ACQUIRED, recovered.outcome)
        assert recovered.lease is not None
        self.assertEqual(2, recovered.lease.fencing_token)

    def test_heartbeat_rotates_deadline_and_old_lease_immediately_loses_authority(self) -> None:
        request = _request("report-callback-heartbeat", "4205")
        authority = self._admit_and_claim(request)
        self._finish_with_eligibility(request, authority)
        lease = self._acquire_initial(request)
        with self.factories.callback_delivery() as unit_of_work:
            renewed = unit_of_work.callback_delivery.heartbeat(
                CallbackHeartbeatCommand(
                    lease=lease,
                    heartbeat_at=_T5,
                    lease_expires_at=_T30,
                )
            )
            unit_of_work.commit()
        self.assertIs(CallbackControlMutationOutcome.APPLIED, renewed.outcome)
        assert renewed.lease is not None
        with self.factories.callback_delivery() as unit_of_work:
            self.assertIs(
                CallbackValidationOutcome.AUTHORITY_LOST,
                unit_of_work.callback_delivery.validate(
                    CallbackValidationCommand(lease=lease, observed_at=_T10)
                ),
            )
            self.assertIs(
                CallbackValidationOutcome.VALID,
                unit_of_work.callback_delivery.validate(
                    CallbackValidationCommand(lease=renewed.lease, observed_at=_T10)
                ),
            )
            unit_of_work.rollback()

    def test_expired_sweep_freezes_unknown_and_never_reacquires(self) -> None:
        request = _request("report-callback-expired", "4206")
        authority = self._admit_and_claim(request)
        self._finish_with_eligibility(request, authority)
        self._acquire_initial(request)
        with self.factories.callback_delivery() as unit_of_work:
            sweep = unit_of_work.callback_delivery.freeze_expired(
                CallbackGuardSweepCommand(
                    business_type="report",
                    observed_at=_T20,
                    limit=10,
                )
            )
            unit_of_work.commit()
        self.assertEqual((1, 1), (sweep.scanned_count, sweep.frozen_count))
        with self.factories.callback_delivery() as unit_of_work:
            reacquire = unit_of_work.callback_delivery.acquire(
                CallbackAcquireCommand(
                    task_id=request.task_id,
                    business_ref=request.business_ref,
                    trigger=CallbackDeliveryTrigger.INITIAL_DELIVERY,
                    lease_token="must-not-be-used",
                    acquired_at=_T20,
                    lease_expires_at=_T30,
                )
            )
            unit_of_work.rollback()
        self.assertIs(CallbackAcquireOutcome.OUTCOME_UNKNOWN, reacquire.outcome)


if __name__ == "__main__":
    unittest.main()
