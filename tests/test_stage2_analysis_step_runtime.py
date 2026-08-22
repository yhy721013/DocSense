"""阶段 2-6 步骤 5：Analysis Registry、Authority Step 与原子终态。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import unittest

from app.modules.analysis.adapters import AnalysisV5TaskCommandCodec
from app.modules.analysis.adapters.sqlite import (
    SQLiteAnalysisExecutionUnitOfWorkFactory,
    SQLiteAnalysisResultSnapshotStore,
    SQLiteAnalysisV2ResourceStoreAdapter,
    bootstrap_analysis_task_control_database,
)
from app.modules.analysis.application import (
    ANALYSIS_STEP_REGISTRY,
    AnalysisStepRuntime,
    resolve_analysis_step,
)
from app.modules.analysis.domain.errors import AnalysisContractError
from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.tasks.adapters import CodecTaskExecutionSnapshotLoader
from app.modules.tasks.adapters.sqlite import (
    SQLiteCallbackControlStore,
    SQLiteConnectionFactory,
    SQLiteTaskControlStore,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.application import TaskExecutionRuntime
from app.modules.tasks.domain import (
    TaskLeaseRuntimeSettings,
    TaskOwnerIdentity,
    TaskStepCheckpoint,
)
from app.modules.tasks.ports import TaskExecutionRuntimeOutcome, TaskWorkflowContextPort
from tests import workspace_tempdir
from tests.fakes import (
    FakeClock,
    FakeLeaseHeartbeatSupervisor,
    FixedTaskLeaseTokenFactory,
)
from tests.test_stage2_analysis_v2_admission import (
    _command,
    _execution_profile,
    _translation_profile,
    _IdentityFactory,
)
from app.modules.analysis.adapters import SQLiteAnalysisV2BatchAdmissionAdapter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_T0 = "2026-08-15T00:00:00.000000Z"


class _AllStepsWorkflow:
    """严格 Fake Workflow：不做外部 I/O，只验证全部控制边界。"""

    def __init__(self, steps: AnalysisStepRuntime) -> None:
        self._steps = steps
        self.context: TaskWorkflowContextPort | None = None

    def run(self, context: TaskWorkflowContextPort) -> None:
        self.context = context
        execution = context.loaded_input.snapshot
        for definition in ANALYSIS_STEP_REGISTRY[:-2]:
            step_key = definition.key_pattern.replace("{attempt_number}", "1")
            active = self._steps.begin(
                context,
                step_key=step_key,
                idempotency_key=f"analysis:test:{step_key}",
            )
            digest = hashlib.sha256(step_key.encode("utf-8")).hexdigest()
            self._steps.succeed(
                context,
                active,
                TaskStepCheckpoint(
                    code="analysis_test_checkpoint_v1",
                    result_ref=f"analysis-test:{step_key}",
                    result_digest=digest,
                ),
            )
        payload = FrozenJsonObject.from_mapping(
            {
                "businessType": "file",
                "data": {"fileName": execution.business_ref.business_key, "status": "2"},
                "msg": "解析完成",
            },
            name="analysis_runtime_callback",
        )
        terminal = self._steps.begin(
            context,
            step_key="terminal.commit",
            idempotency_key=f"analysis:{execution.task_id.value}:terminal:test",
        )
        digest = hashlib.sha256(
            json.dumps(
                payload.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._steps.checkpoint_result_snapshot(
            context,
            business_ref=execution.business_ref,
            payload=payload,
            result_digest=digest,
        )
        self._steps.finish(
            context,
            terminal,
            business_ref=execution.business_ref,
            succeeded=True,
            public_status="2",
            message="解析完成",
            result_ref=f"analysis-result:v1:{digest}",
            terminal_checkpoint=TaskStepCheckpoint(
                code="analysis_terminal_committed_v1",
                result_ref=f"analysis-result:v1:{digest}",
                result_digest=digest,
            ),
        )


class _UnknownStepWorkflow:
    """把一次真实 Step unknown 条件写收敛为 recovery_required。"""

    def __init__(self, steps: AnalysisStepRuntime) -> None:
        self._steps = steps

    def run(self, context: TaskWorkflowContextPort) -> None:
        active = self._steps.begin(
            context,
            step_key="rag.document.upload",
            idempotency_key="analysis:test:unknown-upload",
        )
        self._steps.fail(
            context,
            active,
            error_code="rag_document_upload_outcome_unknown",
            outcome_unknown=True,
        )


class AnalysisStepRuntimeTests(unittest.TestCase):
    def test_registry_matches_frozen_stage20_asset_and_rejects_ambiguous_keys(self) -> None:
        asset = json.loads(
            (PROJECT_ROOT / "tests/contracts/stage2_business_step_registry.json")
            .read_text(encoding="utf-8")
        )["businesses"]["analysis"]["steps"]
        expected = {
            (
                item["stepKey"],
                item["definitionVersion"],
                item["effectKind"],
                item["replayPolicy"],
                item["schemaRef"],
                item["recoveryMatrixRef"],
                item["successResultRef"],
            )
            for item in asset
        }
        actual = {
            (
                item.key_pattern,
                item.definition_version,
                item.effect_kind.value,
                item.replay_policy.value,
                item.schema_ref,
                item.recovery_matrix_ref,
                item.success_result_ref,
            )
            for item in ANALYSIS_STEP_REGISTRY
        }
        self.assertEqual(expected, actual)
        for valid in (
            "classification.execute:1",
            "extraction.execute:12",
            "combined.execute:2",
            "identity.reselect",
        ):
            self.assertTrue(resolve_analysis_step(valid).matches(valid))
        for invalid in (
            "classification.execute:0",
            "extraction.execute:01",
            "combined.execute:-1",
            "terminal.commit:extra",
            "unknown.step",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AnalysisContractError):
                    resolve_analysis_step(invalid)

    def test_runtime_executes_all_steps_and_commits_terminal_callback_atomically(self) -> None:
        with workspace_tempdir() as temporary_root:
            root = Path(temporary_root)
            old_path = root / "old.sqlite3"
            database_path = root / "task-control.sqlite3"
            sqlite3.connect(old_path).close()
            bootstrap = bootstrap_analysis_task_control_database(old_path, database_path)
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
            )
            root_uows = build_sqlite_task_control_uow_factories(manager)
            clock = FakeClock(_T0)
            identities = _IdentityFactory()
            codec = AnalysisV5TaskCommandCodec(
                execution_profile=_execution_profile(),
                translation_profile=_translation_profile(),
            )
            admission = SQLiteAnalysisV2BatchAdmissionAdapter(
                admission_uow_factory=root_uows.admission,
                codec=codec,
                clock=clock,
                task_id_factory=identities.task_id,
                batch_id_factory=identities.batch_id,
            ).create_batch_if_allowed(_command(1, prefix="runtime"))
            task_id = admission.executions[0].task_id

            analysis_uows = SQLiteAnalysisExecutionUnitOfWorkFactory(
                manager,
                execution_builder=SQLiteTaskControlStore,
                callback_delivery_builder=SQLiteCallbackControlStore,
                resource_builder=SQLiteAnalysisV2ResourceStoreAdapter.from_connection,
                result_snapshot_builder=SQLiteAnalysisResultSnapshotStore.from_connection,
            )
            workflow = _AllStepsWorkflow(
                AnalysisStepRuntime(uow_factory=analysis_uows, clock=clock)
            )
            runtime = TaskExecutionRuntime(
                task_type="file",
                owner=TaskOwnerIdentity(
                    instance_start_id="12345678-1234-4234-8234-123456789abc",
                    process_id=100,
                    executor_name="file",
                    worker_slot="analysis-test-worker",
                ),
                clock=clock,
                execution_uow_factory=root_uows.execution,
                lease_token_factory=FixedTaskLeaseTokenFactory(("analysis-lease-token",)),
                heartbeat_supervisor_factory=lambda: FakeLeaseHeartbeatSupervisor(),
                workflow_runner=workflow,
                snapshot_loader=CodecTaskExecutionSnapshotLoader(
                    query_uow_factory=root_uows.queries,
                    codec=codec,
                ),
                lease_settings=TaskLeaseRuntimeSettings(
                    lease_duration_seconds=60.0,
                    heartbeat_interval_seconds=10.0,
                    stop_grace_seconds=15.0,
                ),
            )
            result = runtime.run(task_id)
            self.assertIs(TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED, result.outcome)

            with sqlite3.connect(database_path) as connection:
                connection.row_factory = sqlite3.Row
                task = connection.execute(
                    "SELECT execution_state, public_status FROM llm_task_executions "
                    "WHERE execution_id = ?",
                    (task_id.value,),
                ).fetchone()
                step_count = connection.execute(
                    "SELECT COUNT(*) FROM task_steps WHERE task_id = ? AND state = 'succeeded'",
                    (task_id.value,),
                ).fetchone()[0]
                result_count = connection.execute(
                    "SELECT COUNT(*) FROM analysis_result_snapshots WHERE task_id = ?",
                    (task_id.value,),
                ).fetchone()[0]
                callback = connection.execute(
                    "SELECT state FROM callback_delivery_guards "
                    "WHERE owner_execution_id = ?",
                    (task_id.value,),
                ).fetchone()
            self.assertEqual(("succeeded", "2"), tuple(task))
            self.assertEqual(len(ANALYSIS_STEP_REGISTRY), step_count)
            self.assertEqual(1, result_count)
            # eligibility 由 owner_execution_id + idle Guard 表达；真正发送权必须另行
            # acquire 并递增 fencing，不能把 idle 错当成没有回调资格。
            self.assertEqual("idle", callback["state"])

    def test_recovery_required_predecessor_blocks_same_batch_successor(self) -> None:
        """unknown 隔离不是确定终态，数据库门禁必须持续阻塞同批后继。"""

        with workspace_tempdir() as temporary_root:
            root = Path(temporary_root)
            old_path = root / "old.sqlite3"
            database_path = root / "task-control.sqlite3"
            sqlite3.connect(old_path).close()
            bootstrap = bootstrap_analysis_task_control_database(old_path, database_path)
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
            )
            root_uows = build_sqlite_task_control_uow_factories(manager)
            clock = FakeClock(_T0)
            identities = _IdentityFactory()
            codec = AnalysisV5TaskCommandCodec(
                execution_profile=_execution_profile(),
                translation_profile=_translation_profile(),
            )
            admission = SQLiteAnalysisV2BatchAdmissionAdapter(
                admission_uow_factory=root_uows.admission,
                codec=codec,
                clock=clock,
                task_id_factory=identities.task_id,
                batch_id_factory=identities.batch_id,
            ).create_batch_if_allowed(_command(2, prefix="recovery-gate"))
            first, second = admission.executions
            analysis_uows = SQLiteAnalysisExecutionUnitOfWorkFactory(
                manager,
                execution_builder=SQLiteTaskControlStore,
                callback_delivery_builder=SQLiteCallbackControlStore,
                resource_builder=SQLiteAnalysisV2ResourceStoreAdapter.from_connection,
                result_snapshot_builder=SQLiteAnalysisResultSnapshotStore.from_connection,
            )
            workflow = _UnknownStepWorkflow(
                AnalysisStepRuntime(uow_factory=analysis_uows, clock=clock)
            )
            runtime = TaskExecutionRuntime(
                task_type="file",
                owner=TaskOwnerIdentity(
                    instance_start_id="12345678-1234-4234-8234-123456789abc",
                    process_id=100,
                    executor_name="file",
                    worker_slot="analysis-recovery-gate",
                ),
                clock=clock,
                execution_uow_factory=root_uows.execution,
                lease_token_factory=FixedTaskLeaseTokenFactory(("analysis-unknown-lease",)),
                heartbeat_supervisor_factory=lambda: FakeLeaseHeartbeatSupervisor(),
                workflow_runner=workflow,
                snapshot_loader=CodecTaskExecutionSnapshotLoader(
                    query_uow_factory=root_uows.queries,
                    codec=codec,
                ),
                lease_settings=TaskLeaseRuntimeSettings(
                    lease_duration_seconds=60.0,
                    heartbeat_interval_seconds=10.0,
                    stop_grace_seconds=15.0,
                ),
            )
            result = runtime.run(first.task_id)
            self.assertIs(TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED, result.outcome)
            with root_uows.queries() as unit_of_work:
                runnable = unit_of_work.queries.scan_runnable(
                    "file",
                    not_after=_T0,
                    limit=10,
                )
            self.assertNotIn(second.task_id, runnable)
            with sqlite3.connect(database_path) as connection:
                task = connection.execute(
                    "SELECT execution_state, public_status FROM llm_task_executions "
                    "WHERE execution_id = ?",
                    (first.task_id.value,),
                ).fetchone()
                step = connection.execute(
                    "SELECT state FROM task_steps WHERE task_id = ? AND step_key = ?",
                    (first.task_id.value, "rag.document.upload"),
                ).fetchone()
                recovery_count = connection.execute(
                    "SELECT COUNT(*) FROM task_recovery_cases WHERE task_id = ?",
                    (first.task_id.value,),
                ).fetchone()[0]
                result_count = connection.execute(
                    "SELECT COUNT(*) FROM analysis_result_snapshots WHERE task_id = ?",
                    (first.task_id.value,),
                ).fetchone()[0]
            # Runtime start 已把公开投影推进到处理中；unknown 隔离只冻结执行权，
            # 不伪造成功/失败终态，因此继续保留既有处理中状态。
            self.assertEqual(("recovery_required", "1"), task)
            self.assertEqual(("outcome_unknown",), step)
            self.assertEqual(1, recovery_count)
            self.assertEqual(0, result_count)


if __name__ == "__main__":
    unittest.main()
