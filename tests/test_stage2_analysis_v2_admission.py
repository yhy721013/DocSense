"""阶段 2-6 步骤 3：Analysis v5 批量 Admission 原子性。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import unittest

from app.modules.analysis.adapters import (
    AnalysisV5TaskCommandCodec,
    SQLiteAnalysisV2BatchAdmissionAdapter,
)
from app.modules.analysis.adapters.sqlite import (
    bootstrap_analysis_task_control_database,
)
from app.modules.analysis.domain.execution_profile import (
    ANALYSIS_EXECUTION_PROFILE_SCHEMA_NAME,
    AnalysisExecutionProfile,
)
from app.modules.analysis.domain.task_inputs import (
    ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V5,
    AnalysisPolicySnapshot,
    AnalysisSubmissionSnapshot,
    FrozenJsonArray,
    FrozenJsonObject,
)
from app.modules.analysis.ports import (
    AnalysisBatchAdmissionOutcome,
    AnalysisBatchCommand,
)
from app.modules.tasks.adapters import CodecTaskExecutionSnapshotLoader
from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.domain import (
    TaskId,
    TaskLeaseRuntimeSettings,
    TaskOwnerIdentity,
    TaskTransition,
)
from app.modules.tasks.application import TaskExecutionRuntime
from app.modules.tasks.ports import (
    TaskClaimRequest,
    TaskDispatchDeferralCommand,
    TaskExecutionMutationOutcome,
    TaskExecutionRuntimeOutcome,
    TaskTerminalCommand,
)
from app.modules.translation.domain import (
    TranslationFailurePolicy,
    TranslationMode,
    TranslationProfile,
)
from tests import workspace_tempdir
from tests.fakes import FakeLeaseHeartbeatSupervisor, FixedTaskLeaseTokenFactory
from tests.fakes.task_execution import FakeClock


def _execution_profile() -> AnalysisExecutionProfile:
    return AnalysisExecutionProfile(
        schema_name=ANALYSIS_EXECUTION_PROFILE_SCHEMA_NAME,
        schema_version=1,
        source_transport_profile_id="http-source-v1",
        max_download_bytes=1024 * 1024,
        rag_provider_id="anythingllm",
        rag_provider_fingerprint="1" * 64,
        rag_model_fingerprint="2" * 64,
        rag_workspace_profile_id="analysis-workspace-v1",
        rag_projection_profile_id="3" * 64,
        prompt_profile_id="analysis-prompts-v1",
        knowledge_provider_id="anythingllm",
        knowledge_provider_fingerprint="1" * 64,
        knowledge_protocol_version="v1.15",
    )


def _translation_profile() -> TranslationProfile:
    return TranslationProfile.create(
        engine_id="fake-machine-translation",
        engine_fingerprint="4" * 64,
        renderer_id="bilingual-html-v1",
        renderer_fingerprint="5" * 64,
        mode=TranslationMode.MACHINE,
        failure_policy=TranslationFailurePolicy.PLACEHOLDER,
    )


def _command(count: int, *, prefix: str) -> AnalysisBatchCommand:
    params = [
        {
            "fileName": f"{prefix}-{index:02d}.txt",
            "filePath": f"https://example.invalid/{prefix}/{index}.txt",
            "futureExtension": {"ordinal": index},
        }
        for index in range(1, count + 1)
    ]
    projection = FrozenJsonObject.from_mapping(
        {"businessType": "file", "params": params},
        name="analysis_v2_batch",
    )
    frozen = projection.get("params")
    if not isinstance(frozen, FrozenJsonArray):  # pragma: no cover - 夹具保护。
        raise AssertionError("测试 params 未冻结为数组")
    submissions = tuple(
        AnalysisSubmissionSnapshot.from_frozen_params(
            item,
            policy_snapshot=AnalysisPolicySnapshot.default(),
        )
        for item in frozen.values
        if isinstance(item, FrozenJsonObject)
    )
    return AnalysisBatchCommand(projection, submissions, f"trace-{prefix}")


class _IdentityFactory:
    def __init__(self) -> None:
        self.task_sequence = 0
        self.batch_sequence = 0

    def task_id(self) -> TaskId:
        self.task_sequence += 1
        return TaskId(f"analysis-v2-task-{self.task_sequence}")

    def batch_id(self) -> str:
        self.batch_sequence += 1
        return f"{self.batch_sequence:032x}"


class _NeverCalledWorkflow:
    """毒输入必须在 claim 前失败；若进入 Workflow，测试立即失败。"""

    def run(self, _context) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("毒输入不得进入 Analysis Workflow")


class AnalysisV2AdmissionTests(unittest.TestCase):
    def _infrastructure(self, root: Path):
        old_path = root / "old.sqlite3"
        target_path = root / "task-control.sqlite3"
        sqlite3.connect(old_path).close()
        bootstrap = bootstrap_analysis_task_control_database(old_path, target_path)
        manager = SQLiteTransactionManager(
            SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
        )
        factories = build_sqlite_task_control_uow_factories(manager)
        identities = _IdentityFactory()
        adapter = SQLiteAnalysisV2BatchAdmissionAdapter(
            admission_uow_factory=factories.admission,
            codec=AnalysisV5TaskCommandCodec(
                execution_profile=_execution_profile(),
                translation_profile=_translation_profile(),
            ),
            clock=FakeClock("2026-08-15T00:00:00.000000Z"),
            task_id_factory=identities.task_id,
            batch_id_factory=identities.batch_id,
        )
        return target_path, adapter

    @staticmethod
    def _owner(slot: str) -> TaskOwnerIdentity:
        return TaskOwnerIdentity(
            instance_start_id="12345678-1234-4234-8234-123456789abc",
            process_id=100,
            executor_name="file",
            worker_slot=slot,
        )

    def test_every_supported_batch_size_is_atomic_and_writes_v5_profiles(self) -> None:
        """逐个覆盖公开允许的 1～32 项，而不是只抽测边界值。"""

        with workspace_tempdir() as temporary_root:
            database_path, adapter = self._infrastructure(Path(temporary_root))
            expected_total = 0
            for count in range(1, 33):
                with self.subTest(count=count):
                    admission = adapter.create_batch_if_allowed(
                        _command(count, prefix=f"size-{count:02d}")
                    )
                    self.assertIs(
                        AnalysisBatchAdmissionOutcome.ACCEPTED,
                        admission.outcome,
                    )
                    self.assertEqual(count, len(admission.executions))
                    self.assertEqual(
                        tuple(range(1, count + 1)),
                        tuple(item.batch_sequence for item in admission.executions),
                    )
                    expected_total += count

            connection = sqlite3.connect(database_path)
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    "SELECT input_schema_version, input_payload, "
                    "dispatch_sequence FROM llm_task_executions "
                    "WHERE business_type = 'file' ORDER BY dispatch_sequence"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(expected_total, len(rows))
            self.assertEqual(
                list(range(1, expected_total + 1)),
                [row["dispatch_sequence"] for row in rows],
            )
            self.assertTrue(
                all(
                    row["input_schema_version"]
                    == ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V5
                    for row in rows
                )
            )
            self.assertIn('"execution_profile"', rows[-1]["input_payload"])
            self.assertIn('"translation_profile"', rows[-1]["input_payload"])

    def test_one_active_conflict_rolls_back_every_new_batch_member(self) -> None:
        with workspace_tempdir() as temporary_root:
            database_path, adapter = self._infrastructure(Path(temporary_root))
            first = adapter.create_batch_if_allowed(_command(1, prefix="conflict"))
            self.assertIs(AnalysisBatchAdmissionOutcome.ACCEPTED, first.outcome)

            projection = FrozenJsonObject.from_mapping(
                {
                    "businessType": "file",
                    "params": [
                        {
                            "fileName": "conflict-01.txt",
                            "filePath": "https://example.invalid/conflict/reused.txt",
                        },
                        {
                            "fileName": "must-rollback.txt",
                            "filePath": "https://example.invalid/conflict/new.txt",
                        },
                    ],
                },
                name="analysis_conflict_batch",
            )
            frozen = projection.get("params")
            assert isinstance(frozen, FrozenJsonArray)
            command = AnalysisBatchCommand(
                projection,
                tuple(
                    AnalysisSubmissionSnapshot.from_frozen_params(
                        item,
                        policy_snapshot=AnalysisPolicySnapshot.default(),
                    )
                    for item in frozen.values
                    if isinstance(item, FrozenJsonObject)
                ),
                "trace-conflict-second",
            )
            rejected = adapter.create_batch_if_allowed(command)
            self.assertIs(
                AnalysisBatchAdmissionOutcome.CONFLICT_ACTIVE,
                rejected.outcome,
            )
            with sqlite3.connect(database_path) as connection:
                execution_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_task_executions"
                ).fetchone()[0]
                rolled_back = connection.execute(
                    "SELECT COUNT(*) FROM llm_tasks WHERE business_key = ?",
                    ("must-rollback.txt",),
                ).fetchone()[0]
            self.assertEqual(1, execution_count)
            self.assertEqual(0, rolled_back)

    def test_persisted_fifo_and_predecessor_gate_hold_across_workers(self) -> None:
        """跨批候选按 dispatch FIFO；同批后继不能被并发 Worker 越过。"""

        with workspace_tempdir() as temporary_root:
            root = Path(temporary_root)
            _, adapter = self._infrastructure(root)
            first_batch = adapter.create_batch_if_allowed(
                _command(3, prefix="ordered-a")
            )
            second_batch = adapter.create_batch_if_allowed(
                _command(2, prefix="ordered-b")
            )
            self.assertIs(AnalysisBatchAdmissionOutcome.ACCEPTED, first_batch.outcome)
            self.assertIs(AnalysisBatchAdmissionOutcome.ACCEPTED, second_batch.outcome)

            # 重新构造 Manager，明确证明顺序来自持久数据库，而不是 Adapter 的内存列表。
            bootstrap = bootstrap_analysis_task_control_database(
                root / "old.sqlite3",
                root / "task-control.sqlite3",
            )
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=500)
            )
            factories = build_sqlite_task_control_uow_factories(manager)
            with factories.queries() as unit_of_work:
                runnable = unit_of_work.queries.scan_runnable(
                    "file",
                    not_after="2026-08-15T00:00:01.000000Z",
                    limit=10,
                )
            self.assertEqual(
                (
                    first_batch.executions[0].task_id,
                    second_batch.executions[0].task_id,
                ),
                runnable,
            )

            def claim(execution_index: int, slot: str):
                execution = first_batch.executions[execution_index]
                with factories.execution() as unit_of_work:
                    result = unit_of_work.execution.claim(
                        TaskClaimRequest(
                            task_id=execution.task_id,
                            task_type="file",
                            owner=self._owner(slot),
                            lease_token=f"lease-{slot}",
                            claimed_at="2026-08-15T00:00:01.000000Z",
                            lease_expires_at="2026-08-15T00:01:00.000000Z",
                        )
                    )
                    if result.outcome is TaskExecutionMutationOutcome.APPLIED:
                        unit_of_work.commit()
                    else:
                        unit_of_work.rollback()
                    return result

            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(claim, 0, "worker-a")
                second_future = pool.submit(claim, 1, "worker-b")
                claims = (first_future.result(), second_future.result())
            self.assertEqual(
                1,
                sum(
                    item.outcome is TaskExecutionMutationOutcome.APPLIED
                    for item in claims
                ),
            )
            self.assertIs(TaskExecutionMutationOutcome.APPLIED, claims[0].outcome)
            self.assertIs(
                TaskExecutionMutationOutcome.NOT_RUNNABLE,
                claims[1].outcome,
            )
            assert claims[0].attempt is not None
            authority = claims[0].attempt.authority
            with factories.execution() as unit_of_work:
                self.assertIs(
                    TaskExecutionMutationOutcome.APPLIED,
                    unit_of_work.execution.start(
                        authority,
                        started_at="2026-08-15T00:00:02.000000Z",
                    ),
                )
                self.assertIs(
                    TaskExecutionMutationOutcome.APPLIED,
                    unit_of_work.execution.finish(
                        TaskTerminalCommand(
                            authority=authority,
                            transition=TaskTransition.BUSINESS_SUCCEEDED,
                            public_status="2",
                            message="",
                            result_ref="analysis-result:test",
                            completed_at="2026-08-15T00:00:03.000000Z",
                        )
                    ),
                )
                unit_of_work.commit()
            next_claim = claim(1, "worker-c")
            self.assertIs(TaskExecutionMutationOutcome.APPLIED, next_claim.outcome)

    def test_poison_input_is_cooled_without_claim_and_blocks_batch_successor(self) -> None:
        """毒输入保持 accepted 并持久冷却，同批后继不能绕过它领取。"""

        with workspace_tempdir() as temporary_root:
            root = Path(temporary_root)
            database_path, adapter = self._infrastructure(root)
            batch = adapter.create_batch_if_allowed(_command(2, prefix="poison"))
            first, second = batch.executions
            # 保留合法 JSON 以绕开 SQLite CHECK，只破坏业务 v5 契约；这模拟磁盘、
            # 人工运维或旧版本缺陷留下的“可解析但不可执行”冻结输入。
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "UPDATE llm_task_executions SET input_payload = ? WHERE execution_id = ?",
                    ('{"schema_version":5}', first.task_id.value),
                )
                connection.commit()

            bootstrap = bootstrap_analysis_task_control_database(
                root / "old.sqlite3",
                database_path,
            )
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
            )
            factories = build_sqlite_task_control_uow_factories(manager)
            runtime = TaskExecutionRuntime(
                task_type="file",
                owner=self._owner("poison-worker"),
                clock=FakeClock("2026-08-15T00:00:01.000000Z"),
                execution_uow_factory=factories.execution,
                lease_token_factory=FixedTaskLeaseTokenFactory(("poison-lease",)),
                heartbeat_supervisor_factory=lambda: FakeLeaseHeartbeatSupervisor(),
                workflow_runner=_NeverCalledWorkflow(),
                snapshot_loader=CodecTaskExecutionSnapshotLoader(
                    query_uow_factory=factories.queries,
                    codec=AnalysisV5TaskCommandCodec(
                        execution_profile=_execution_profile(),
                        translation_profile=_translation_profile(),
                    ),
                ),
                lease_settings=TaskLeaseRuntimeSettings(
                    lease_duration_seconds=60.0,
                    heartbeat_interval_seconds=10.0,
                    stop_grace_seconds=15.0,
                ),
            )
            result = runtime.run(first.task_id)
            self.assertIs(TaskExecutionRuntimeOutcome.INPUT_ERROR, result.outcome)

            # LocalTaskExecutor 对 INPUT_ERROR 的正式动作就是这笔 accepted-only
            # 冷却条件写；这里直接调用 Store，避免离线测试引入后台线程时序。
            with factories.execution() as unit_of_work:
                outcome = unit_of_work.execution.defer_dispatch(
                    TaskDispatchDeferralCommand(
                        task_id=first.task_id,
                        task_type="file",
                        reason_code="runtime_input_error",
                        deferred_at="2026-08-15T00:00:01.000000Z",
                        next_dispatch_at="2026-08-15T00:01:01.000000Z",
                    )
                )
                self.assertIs(TaskExecutionMutationOutcome.APPLIED, outcome)
                unit_of_work.commit()
            with factories.queries() as unit_of_work:
                runnable = unit_of_work.queries.scan_runnable(
                    "file",
                    not_after="2026-08-15T00:00:02.000000Z",
                    limit=10,
                )
            self.assertNotIn(first.task_id, runnable)
            self.assertNotIn(second.task_id, runnable)
            with sqlite3.connect(database_path) as connection:
                first_state = connection.execute(
                    "SELECT execution_state, current_attempt_no, last_dispatch_error "
                    "FROM llm_task_executions WHERE execution_id = ?",
                    (first.task_id.value,),
                ).fetchone()
                second_state = connection.execute(
                    "SELECT execution_state, current_attempt_no "
                    "FROM llm_task_executions WHERE execution_id = ?",
                    (second.task_id.value,),
                ).fetchone()
            self.assertEqual(("accepted", 0, "runtime_input_error"), first_state)
            self.assertEqual(("accepted", 0), second_state)


if __name__ == "__main__":
    unittest.main()
