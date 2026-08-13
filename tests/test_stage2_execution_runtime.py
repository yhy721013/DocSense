"""阶段 2-3 Authority Runtime、heartbeat 与失权停止定向验收。"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, Lock
import unittest

from app.modules.tasks.adapters import ThreadedLeaseHeartbeatSupervisor
from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTransactionManager,
    bootstrap_task_control_database,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.application import (
    TaskExecutionAuthoritySession,
    TaskExecutionRuntime,
)
from app.modules.tasks.domain import (
    TaskBusinessRef,
    TaskExecutionAuthority,
    TaskId,
    TaskLeaseRuntimeSettings,
    TaskOwnerIdentity,
)
from app.modules.tasks.ports import (
    ClockAnomalyError,
    LeaseSupervisorOutcome,
    LeaseSupervisorResult,
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
    TaskExecutionMutationOutcome,
    TaskExecutionRuntimeOutcome,
    TaskExecutionStopRequested,
    TaskHeartbeatResult,
    TaskProgressCommand,
)
from tests.fakes import (
    FakeClock,
    FakeLeaseHeartbeatSupervisor,
    FixedTaskLeaseTokenFactory,
    ManualLeaseHeartbeatPulse,
    StrictTaskControlFake,
    StrictTaskWorkflowRunnerFake,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = (
    PROJECT_ROOT / "tests" / "contracts" / "stage2_execution_runtime_contract.json"
)
_T0 = "2026-08-13T00:00:00.000000Z"
_T5 = "2026-08-13T00:00:05.000000Z"
_T6 = "2026-08-13T00:00:06.000000Z"
_T30 = "2026-08-13T00:00:30.000000Z"
_T35 = "2026-08-13T00:00:35.000000Z"


def _owner() -> TaskOwnerIdentity:
    return TaskOwnerIdentity(
        instance_start_id="12345678-1234-4234-8234-123456789abc",
        process_id=100,
        executor_name="report",
        worker_slot="worker-0",
    )


def _admission(task_id: str) -> TaskAdmissionRequest[tuple[str, ...]]:
    return TaskAdmissionRequest(
        task_id=TaskId(task_id),
        task_type="report",
        business_ref=TaskBusinessRef("report", f"business-{task_id}"),
        input_schema_version=1,
        input_snapshot=(task_id,),
        input_payload={"business_key": f"business-{task_id}"},
        public_request_payload={"reportId": task_id},
        initial_public_status="waiting",
        trace_id=f"trace-{task_id}",
        accepted_at=_T0,
    )


def _authority() -> TaskExecutionAuthority:
    return TaskExecutionAuthority(
        task_id=TaskId("task-session"),
        attempt_no=1,
        owner_id=_owner().owner_id,
        lease_token="secret-never-log",
        fencing_token=1,
        lease_expires_at=_T30,
    )


def _lease_settings() -> TaskLeaseRuntimeSettings:
    return TaskLeaseRuntimeSettings()


class _FakeExecutionUnitOfWork:
    """只用于 Runtime 编排测试；严格状态语义由 StrictTaskControlFake 提供。"""

    def __init__(self, execution: StrictTaskControlFake) -> None:
        self.execution = execution
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.committed = False


class _FakeExecutionUnitOfWorkFactory:
    def __init__(self, execution: StrictTaskControlFake) -> None:
        self._execution = execution
        self.created: list[_FakeExecutionUnitOfWork] = []

    def __call__(self) -> _FakeExecutionUnitOfWork:
        unit_of_work = _FakeExecutionUnitOfWork(self._execution)
        self.created.append(unit_of_work)
        return unit_of_work


class _StartRejectingExecution:
    """保留真实 claim，只在 start 注入有限并发拒绝。"""

    def __init__(self, delegate: StrictTaskControlFake) -> None:
        self._delegate = delegate

    def claim(self, request):
        return self._delegate.claim(request)

    def start(self, authority, *, started_at):
        return TaskExecutionMutationOutcome.AUTHORITY_LOST


class _NotifyingExecutionUnitOfWorkFactory:
    """第三次 UoW 是首次 heartbeat；Event 只做确定性线程编排。"""

    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self._lock = Lock()
        self._count = 0
        self.heartbeat_entered = Event()

    def __call__(self):
        with self._lock:
            self._count += 1
            if self._count == 3:
                self.heartbeat_entered.set()
        return self._delegate()


class Stage2ExecutionRuntimeContractTests(unittest.TestCase):
    def test_machine_contract_freezes_v2_only_authority_chain(self) -> None:
        contract = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertFalse(contract["publicContractChanged"])
        self.assertFalse(contract["productionWired"])
        self.assertFalse(contract["legacyRunnerChanged"])
        self.assertFalse(contract["callbackSemanticsChanged"])
        self.assertEqual(
            {
                "task_id",
                "attempt_no",
                "lease_token",
                "fencing_token",
                "lease_expires_at",
            },
            set(contract["authority"]["completeFields"]),
        )
        self.assertTrue(
            contract["authoritySession"]["renewalCommitAndSnapshotSwapSerialized"]
        )
        self.assertFalse(
            contract["authoritySession"]["externalIoInsideAuthorityGuardAllowed"]
        )
        self.assertFalse(contract["cooperativeStop"]["runtimeSendsCallback"])
        self.assertTrue(contract["leaseSettings"]["validatedBeforeThreadStart"])

    def test_lease_settings_reject_unsafe_combinations_before_thread_start(self) -> None:
        settings = TaskLeaseRuntimeSettings()
        self.assertEqual(5.0, settings.heartbeat_interval_seconds)
        self.assertEqual(30.0, settings.lease_duration_seconds)
        self.assertEqual(
            0.0,
            TaskLeaseRuntimeSettings(
                max_clock_jitter_seconds=0
            ).max_clock_jitter_seconds,
        )
        with self.assertRaisesRegex(ValueError, "lease >="):
            TaskLeaseRuntimeSettings(lease_duration_seconds=21)
        with self.assertRaisesRegex(ValueError, "stop_grace >="):
            TaskLeaseRuntimeSettings(stop_grace_seconds=6)
        with self.assertRaisesRegex(ValueError, "正有限数"):
            TaskLeaseRuntimeSettings(heartbeat_interval_seconds=float("nan"))


class TaskExecutionAuthoritySessionTests(unittest.TestCase):
    def test_authority_repr_never_contains_lease_token(self) -> None:
        authority = _authority()
        self.assertNotIn(authority.lease_token, repr(authority))

    def test_heartbeat_atomically_rotates_expiry_and_stop_is_monotonic(self) -> None:
        initial = _authority()
        renewed = replace(initial, lease_expires_at=_T35)
        session = TaskExecutionAuthoritySession(initial)

        result = session.renew_authority(
            lambda actual: TaskHeartbeatResult(
                TaskExecutionMutationOutcome.APPLIED,
                renewed if actual == initial else None,
            )
        )
        self.assertIs(TaskExecutionMutationOutcome.APPLIED, result.outcome)
        self.assertEqual(_T35, session.current_authority().lease_expires_at)
        self.assertEqual(
            renewed,
            session.run_authorized(lambda actual: actual),
        )

        loss = LeaseSupervisorResult(
            LeaseSupervisorOutcome.AUTHORITY_LOST,
            TaskExecutionMutationOutcome.AUTHORITY_LOST,
        )
        self.assertTrue(session.request_stop(loss))
        self.assertFalse(session.request_stop(loss))
        with self.assertRaises(TaskExecutionStopRequested):
            session.run_authorized(lambda actual: actual)

    def test_non_applied_heartbeat_sets_stop_before_releasing_authority_gate(self) -> None:
        session = TaskExecutionAuthoritySession(_authority())
        result = session.renew_authority(
            lambda _actual: TaskHeartbeatResult(
                TaskExecutionMutationOutcome.LEASE_EXPIRED
            )
        )
        self.assertIs(TaskExecutionMutationOutcome.LEASE_EXPIRED, result.outcome)
        self.assertTrue(session.stop_requested())
        self.assertIs(
            LeaseSupervisorOutcome.AUTHORITY_LOST,
            session.stop_result().outcome,
        )
        with self.assertRaises(TaskExecutionStopRequested):
            session.run_authorized(lambda actual: actual)

    def test_clock_and_contract_errors_fail_closed_inside_renewal_gate(self) -> None:
        clock_session = TaskExecutionAuthoritySession(_authority())

        def raise_clock(_authority):
            raise ClockAnomalyError("fake clock rollback")

        with self.assertRaises(ClockAnomalyError):
            clock_session.renew_authority(raise_clock)
        self.assertIs(
            LeaseSupervisorOutcome.CLOCK_UNSAFE,
            clock_session.stop_result().outcome,
        )

        invalid_session = TaskExecutionAuthoritySession(_authority())
        invalid = replace(
            _authority(),
            fencing_token=2,
            lease_expires_at=_T35,
        )
        with self.assertRaisesRegex(ValueError, "越界变化"):
            invalid_session.renew_authority(
                lambda _actual: TaskHeartbeatResult(
                    TaskExecutionMutationOutcome.APPLIED,
                    invalid,
                )
            )
        self.assertIs(
            LeaseSupervisorOutcome.INFRASTRUCTURE_ERROR,
            invalid_session.stop_result().outcome,
        )


class TaskExecutionRuntimeFakeTests(unittest.TestCase):
    def _runtime(
        self,
        *,
        clock: FakeClock,
        control: StrictTaskControlFake,
        runner: StrictTaskWorkflowRunnerFake,
        supervisor: FakeLeaseHeartbeatSupervisor,
    ) -> TaskExecutionRuntime:
        return TaskExecutionRuntime(
            task_type="report",
            owner=_owner(),
            clock=clock,
            execution_uow_factory=_FakeExecutionUnitOfWorkFactory(control),
            lease_token_factory=FixedTaskLeaseTokenFactory(("fake-secret-token",)),
            heartbeat_supervisor_factory=lambda: supervisor,
            workflow_runner=runner,
            lease_settings=_lease_settings(),
        )

    def test_runtime_passes_claim_authority_to_start_and_v2_runner(self) -> None:
        clock = FakeClock(_T0)
        control = StrictTaskControlFake(clock)
        request = _admission("task-runtime-fake")
        self.assertIs(
            TaskAdmissionOutcome.ACCEPTED,
            control.admit_one(request).outcome,
        )
        observed: list[TaskExecutionAuthority] = []
        runner = StrictTaskWorkflowRunnerFake(
            lambda session: observed.append(
                session.run_authorized(lambda authority: authority)
            )
        )
        supervisor = FakeLeaseHeartbeatSupervisor()

        result = self._runtime(
            clock=clock,
            control=control,
            runner=runner,
            supervisor=supervisor,
        ).run(request.task_id)

        self.assertIs(TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED, result.outcome)
        self.assertTrue(supervisor.started)
        self.assertTrue(supervisor.stopped)
        self.assertEqual(1, len(observed))
        self.assertEqual(request.task_id, observed[0].task_id)
        self.assertEqual(1, observed[0].attempt_no)
        self.assertEqual(1, observed[0].fencing_token)
        self.assertEqual(_T30, observed[0].lease_expires_at)

    def test_runtime_maps_supervisor_loss_and_runner_cannot_write(self) -> None:
        clock = FakeClock(_T0)
        control = StrictTaskControlFake(clock)
        request = _admission("task-runtime-lost")
        control.admit_one(request)
        loss = LeaseSupervisorResult(
            LeaseSupervisorOutcome.AUTHORITY_LOST,
            TaskExecutionMutationOutcome.AUTHORITY_LOST,
        )
        runner = StrictTaskWorkflowRunnerFake(
            lambda session: session.run_authorized(lambda _authority: None)
        )
        supervisor = FakeLeaseHeartbeatSupervisor(start_result=loss)

        result = self._runtime(
            clock=clock,
            control=control,
            runner=runner,
            supervisor=supervisor,
        ).run(request.task_id)

        self.assertIs(TaskExecutionRuntimeOutcome.AUTHORITY_LOST, result.outcome)
        self.assertIs(TaskExecutionMutationOutcome.AUTHORITY_LOST, result.mutation_outcome)
        self.assertTrue(supervisor.stopped)

    def test_runtime_clock_anomaly_fails_before_claim_and_runner(self) -> None:
        clock = FakeClock(_T0)
        clock.rollback(seconds=1)
        control = StrictTaskControlFake(clock)
        runner = StrictTaskWorkflowRunnerFake()
        supervisor = FakeLeaseHeartbeatSupervisor()

        result = self._runtime(
            clock=clock,
            control=control,
            runner=runner,
            supervisor=supervisor,
        ).run(TaskId("task-runtime-clock-unsafe"))

        self.assertIs(TaskExecutionRuntimeOutcome.CLOCK_UNSAFE, result.outcome)
        self.assertFalse(supervisor.started)
        self.assertEqual([], runner.sessions)

    def test_start_rejection_does_not_start_supervisor_or_runner(self) -> None:
        clock = FakeClock(_T0)
        control = StrictTaskControlFake(clock)
        request = _admission("task-runtime-start-rejected")
        control.admit_one(request)
        runner = StrictTaskWorkflowRunnerFake()
        supervisor = FakeLeaseHeartbeatSupervisor()
        rejecting = _StartRejectingExecution(control)

        runtime = TaskExecutionRuntime(
            task_type="report",
            owner=_owner(),
            clock=clock,
            execution_uow_factory=_FakeExecutionUnitOfWorkFactory(rejecting),
            lease_token_factory=FixedTaskLeaseTokenFactory(("fake-secret-token",)),
            heartbeat_supervisor_factory=lambda: supervisor,
            workflow_runner=runner,
            lease_settings=_lease_settings(),
        )
        result = runtime.run(request.task_id)

        self.assertIs(TaskExecutionRuntimeOutcome.START_REJECTED, result.outcome)
        self.assertIs(TaskExecutionMutationOutcome.AUTHORITY_LOST, result.mutation_outcome)
        self.assertFalse(supervisor.started)
        self.assertEqual([], runner.sessions)


class TaskExecutionRuntimeSQLiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        old_path = root / "old.sqlite3"
        database_path = root / "task-control-v2.sqlite3"
        sqlite3.connect(old_path).close()
        bootstrap = bootstrap_task_control_database(old_path, database_path)
        connection_factory = SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
        transaction_manager = SQLiteTransactionManager(connection_factory)
        self.factories = build_sqlite_task_control_uow_factories(transaction_manager)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _admit(self, request: TaskAdmissionRequest[object]) -> None:
        with self.factories.admission() as unit_of_work:
            result = unit_of_work.admission.admit_one(request)
            self.assertIs(TaskAdmissionOutcome.ACCEPTED, result.outcome)
            unit_of_work.commit()

    def _runtime(
        self,
        *,
        clock: FakeClock,
        pulse: ManualLeaseHeartbeatPulse,
        runner: StrictTaskWorkflowRunnerFake,
        notifying_factory: _NotifyingExecutionUnitOfWorkFactory,
    ) -> TaskExecutionRuntime:
        return TaskExecutionRuntime(
            task_type="report",
            owner=_owner(),
            clock=clock,
            execution_uow_factory=notifying_factory,
            lease_token_factory=FixedTaskLeaseTokenFactory(("sqlite-secret-token",)),
            heartbeat_supervisor_factory=lambda: ThreadedLeaseHeartbeatSupervisor(
                clock=clock,
                execution_uow_factory=notifying_factory,
                lease_settings=_lease_settings(),
                pulse=pulse,
                thread_name="test-task-lease-heartbeat",
            ),
            workflow_runner=runner,
            lease_settings=_lease_settings(),
        )

    def test_claim_start_heartbeat_and_old_expiry_rejection_use_one_control_store(self) -> None:
        request = _admission("task-runtime-sqlite")
        self._admit(request)
        clock = FakeClock(_T0)
        pulse = ManualLeaseHeartbeatPulse()
        notifying = _NotifyingExecutionUnitOfWorkFactory(self.factories.execution)
        observed: list[TaskExecutionAuthority] = []

        def run_workflow(session) -> None:
            observed.append(session.current_authority())
            self.assertTrue(pulse.waiting.wait(timeout=2))
            clock.advance(seconds=5)
            pulse.pulse()
            self.assertTrue(notifying.heartbeat_entered.wait(timeout=2))

            def update_progress(authority: TaskExecutionAuthority):
                observed.append(authority)
                with self.factories.execution() as unit_of_work:
                    outcome = unit_of_work.execution.update_progress(
                        TaskProgressCommand(
                            authority=authority,
                            progress=0.25,
                            message="runtime-test",
                            public_status="running",
                            updated_at=_T5,
                        )
                    )
                    if outcome is TaskExecutionMutationOutcome.APPLIED:
                        unit_of_work.commit()
                    return outcome

            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                session.run_authorized(update_progress),
            )

        runner = StrictTaskWorkflowRunnerFake(run_workflow)
        result = self._runtime(
            clock=clock,
            pulse=pulse,
            runner=runner,
            notifying_factory=notifying,
        ).run(request.task_id)

        self.assertIs(TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED, result.outcome)
        self.assertEqual(_T30, observed[0].lease_expires_at)
        self.assertEqual(_T35, observed[1].lease_expires_at)
        self.assertEqual(
            observed[0].task_id,
            observed[1].task_id,
        )
        self.assertEqual(observed[0].attempt_no, observed[1].attempt_no)
        self.assertEqual(observed[0].fencing_token, observed[1].fencing_token)

        # heartbeat 已提交新 expiry 后，旧 Authority 永久不能再写；Store 是最终裁决者。
        with self.factories.execution() as unit_of_work:
            outcome = unit_of_work.execution.update_progress(
                TaskProgressCommand(
                    authority=observed[0],
                    progress=0.5,
                    message="stale-expiry",
                    public_status="running",
                    updated_at=_T6,
                )
            )
        self.assertIs(TaskExecutionMutationOutcome.AUTHORITY_LOST, outcome)

    def test_expired_heartbeat_requests_stop_before_runner_gets_next_authority(self) -> None:
        request = _admission("task-runtime-expired")
        self._admit(request)
        clock = FakeClock(_T0)
        pulse = ManualLeaseHeartbeatPulse()
        notifying = _NotifyingExecutionUnitOfWorkFactory(self.factories.execution)

        def run_workflow(session) -> None:
            self.assertTrue(pulse.waiting.wait(timeout=2))
            clock.advance(seconds=31)
            pulse.pulse()
            self.assertTrue(notifying.heartbeat_entered.wait(timeout=2))
            # heartbeat 持有同一能力门；它提交失权分类并设置 stop 后，本调用才会继续，
            # 因而不可能在失败结果与 stop 信号之间偷取一次旧 Authority。
            session.run_authorized(lambda _authority: None)

        result = self._runtime(
            clock=clock,
            pulse=pulse,
            runner=StrictTaskWorkflowRunnerFake(run_workflow),
            notifying_factory=notifying,
        ).run(request.task_id)

        self.assertIs(TaskExecutionRuntimeOutcome.AUTHORITY_LOST, result.outcome)
        self.assertIs(TaskExecutionMutationOutcome.LEASE_EXPIRED, result.mutation_outcome)


if __name__ == "__main__":
    unittest.main()
