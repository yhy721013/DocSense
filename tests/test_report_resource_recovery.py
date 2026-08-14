"""阶段 1C-5 报告 Artifact 所有权、清理事实与故障恢复验收。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
from types import SimpleNamespace
import unittest

from app.modules.report.application import ReportResourceRecoveryService
from app.modules.report.domain import (
    ReportResourceConcurrencyError,
)
from app.modules.report.ports import (
    ReportArtifactCategory,
    ReportArtifactCleanupResult,
    ReportArtifactRef,
    ReportArtifactScope,
    ReportAuditReceipt,
    ReportRagCleanupRef,
    ReportRagLifecycleEvent,
    ReportResourceCleanupOutcome,
    ReportResourceRecord,
    ReportResourceState,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from tests.fakes import (
    FakeReportArtifactPort,
    FakeReportAuditPort,
    FakeReportRagPort,
    FakeReportResourceStorePort,
    InvocationRecorder,
)


class _ResourceHarness:
    """组合真实恢复 Application 与严格 Fake I/O/Store。"""

    def __init__(self, *, execution_state: str = "succeeded") -> None:
        self.task_id = TaskId("report-resource-001")
        self.business_ref = TaskBusinessRef("report", "132")
        self.scope = ReportArtifactScope(self.task_id, "report/resource-001")
        self.execution = SimpleNamespace(execution_state=execution_state)
        self.recorder = InvocationRecorder()
        self.artifacts = FakeReportArtifactPort(self.recorder)
        self.rag = FakeReportRagPort(self.recorder)
        self.audit = FakeReportAuditPort(self.recorder)
        self.store = FakeReportResourceStorePort(lambda _: self.execution)
        self.service = ReportResourceRecoveryService(
            store=self.store,
            artifacts=self.artifacts,
            rag=self.rag,
            audit=self.audit,
        )
        self.final_artifact = ReportArtifactRef(
            self.task_id,
            "output/report.html",
            ReportArtifactCategory.REPORT_HTML,
            size_bytes=12,
            checksum="report-checksum",
        )
        self.receipt = ReportAuditReceipt(
            self.task_id,
            "report-rag:report-resource-001",
            "audit-001",
        )
        self.service.register(self.task_id, self.business_ref, self.scope)

    def track_complete_set(self, *, include_rag: bool = True) -> None:
        if include_rag:
            self.service.track_rag_cleanup(
                self.task_id,
                ReportRagCleanupRef("cleanup:resource-001"),
            )
        self.service.track_audit(self.receipt)
        self.service.track_final_artifact(self.final_artifact)


class ReportResourceRecoveryTests(unittest.TestCase):
    def test_bounded_sweep_isolates_one_task_failure_and_continues_batch(self) -> None:
        harness = _ResourceHarness()
        second_task_id = TaskId("report-resource-002")
        harness.store.create(
            ReportResourceRecord(
                task_id=second_task_id,
                business_ref=TaskBusinessRef("report", "133"),
                scope=ReportArtifactScope(second_task_id, "report/resource-002"),
            )
        )

        class _OneTaskFailsService(ReportResourceRecoveryService):
            def recover(self, task_id):
                if task_id == harness.task_id:
                    raise RuntimeError("isolated test failure")
                return super().recover(task_id)

        service = _OneTaskFailsService(
            store=harness.store,
            artifacts=harness.artifacts,
            rag=harness.rag,
            audit=harness.audit,
        )

        with self.assertLogs(
            "app.modules.report.application.resource_recovery",
            level="ERROR",
        ):
            result = service.sweep(limit=10)

        self.assertEqual(2, result.scanned_count)
        self.assertEqual(1, result.cleaned_count)
        self.assertEqual((harness.task_id,), result.failed_task_ids)
        self.assertEqual(
            ReportResourceState.CLEANED,
            harness.store.records[second_task_id].state,
        )

    def test_failed_first_page_is_deferred_so_later_records_are_not_starved(self) -> None:
        """固定 limit 下的损坏首记录必须让出下一轮首页。"""

        harness = _ResourceHarness()
        second_task_id = TaskId("report-resource-002")

        class _DeferringStore(FakeReportResourceStorePort):
            def __init__(self):
                super().__init__(lambda _: harness.execution)
                self.deferred_task_ids: set[TaskId] = set()

            def list_recoverable(self, *, limit: int):
                return tuple(
                    task_id
                    for task_id, record in self.records.items()
                    if task_id not in self.deferred_task_ids
                    and record.state
                    in {
                        ReportResourceState.TRACKING,
                        ReportResourceState.CLEANUP_PENDING,
                        ReportResourceState.AUDIT_PENDING,
                    }
                )[:limit]

            def defer_recovery(self, task_id, *, retry_at: str, reason: str):
                deferred = super().defer_recovery(
                    task_id,
                    retry_at=retry_at,
                    reason=reason,
                )
                if deferred:
                    self.deferred_task_ids.add(task_id)
                return deferred

        store = _DeferringStore()
        store.records = dict(harness.store.records)
        store.create(
            ReportResourceRecord(
                task_id=second_task_id,
                business_ref=TaskBusinessRef("report", "133"),
                scope=ReportArtifactScope(second_task_id, "report/resource-002"),
            )
        )

        class _FirstTaskAlwaysFails(ReportResourceRecoveryService):
            def recover(self, task_id):
                if task_id == harness.task_id:
                    raise RuntimeError("permanent corrupt record")
                return super().recover(task_id)

        service = _FirstTaskAlwaysFails(
            store=store,
            artifacts=harness.artifacts,
            rag=harness.rag,
            audit=harness.audit,
            sweep_retry_delay_seconds=30,
        )

        with self.assertLogs(
            "app.modules.report.application.resource_recovery",
            level="ERROR",
        ):
            first = service.sweep(limit=1)
        second = service.sweep(limit=1)

        self.assertEqual((harness.task_id,), first.failed_task_ids)
        self.assertEqual(1, second.cleaned_count)
        self.assertEqual(
            ReportResourceState.CLEANED,
            store.records[second_task_id].state,
        )

    def test_audit_append_failure_replays_exact_events_without_second_delete(self) -> None:
        """外部删除成功但审计追加失败时，恢复只能重放事件，不能再次调用删除。"""

        harness = _ResourceHarness()
        harness.track_complete_set()
        harness.audit.append_error = RuntimeError("audit temporarily unavailable")

        with self.assertLogs(
            "app.modules.report.application.resource_recovery",
            level="ERROR",
        ):
            first = harness.service.cleanup(harness.task_id)
        pending = harness.store.records[harness.task_id]

        harness.audit.append_error = None
        second = harness.service.recover(harness.task_id)
        completed = harness.store.records[harness.task_id]

        self.assertEqual(ReportResourceCleanupOutcome.PENDING, first.outcome)
        self.assertEqual(ReportResourceState.AUDIT_PENDING, pending.state)
        self.assertTrue(pending.pending_events)
        self.assertEqual(1, len(harness.rag.cleanup_calls))
        self.assertEqual(ReportResourceCleanupOutcome.CLEANED, second.outcome)
        self.assertEqual(1, len(harness.rag.cleanup_calls))
        self.assertEqual(2, len(harness.audit.append_calls))
        self.assertEqual(
            harness.audit.append_calls[0].events,
            harness.audit.append_calls[1].events,
        )
        self.assertEqual(1, len(harness.artifacts.cleanup_calls))
        self.assertEqual(
            (harness.final_artifact,),
            harness.artifacts.cleanup_calls[0][1],
        )
        self.assertEqual(ReportResourceState.CLEANED, completed.state)

    def test_failed_external_cleanup_retries_with_next_audit_sequence(self) -> None:
        """明确失败可恢复；第二次事件必须接续序号而不是覆盖第一次证据。"""

        harness = _ResourceHarness()
        harness.track_complete_set()
        harness.rag.cleanup_results = [
            (
                ReportRagLifecycleEvent(
                    sequence_no=2,
                    operation="context_delete",
                    attempt_no=1,
                    success=False,
                    external_ref="context-001",
                    failure_stage="cleanup_context",
                    error_message="temporarily unavailable",
                ),
            ),
            (
                ReportRagLifecycleEvent(
                    sequence_no=3,
                    operation="context_delete",
                    # attempt_no 是同一外部操作的持久化重试序号，恢复后必须接续而非重置。
                    attempt_no=2,
                    success=True,
                    external_ref="context-001",
                ),
            ),
        ]

        first = harness.service.cleanup(harness.task_id)
        second = harness.service.recover(harness.task_id)

        self.assertEqual(ReportResourceCleanupOutcome.PENDING, first.outcome)
        self.assertEqual(ReportResourceCleanupOutcome.CLEANED, second.outcome)
        self.assertEqual(2, len(harness.rag.cleanup_commands))
        self.assertIsNone(harness.rag.cleanup_commands[0].sequence_start)
        self.assertEqual(3, harness.rag.cleanup_commands[1].sequence_start)
        self.assertEqual(2, len(harness.audit.append_calls))
        self.assertEqual(1, len(harness.artifacts.cleanup_calls))

    def test_conversation_delete_failure_is_not_misreported_as_cleaned(self) -> None:
        """线程也是独立外部资源；仅 Workspace/文档成功不能宣称整体已清理。"""

        harness = _ResourceHarness()
        harness.track_complete_set()
        harness.rag.cleanup_results = [
            (
                ReportRagLifecycleEvent(
                    sequence_no=2,
                    operation="conversation_delete",
                    attempt_no=1,
                    success=False,
                    external_ref="thread-001",
                    failure_stage="cleanup_conversation",
                    error_message="temporarily unavailable",
                ),
                ReportRagLifecycleEvent(
                    sequence_no=3,
                    operation="context_delete",
                    attempt_no=1,
                    success=True,
                    external_ref="context-001",
                ),
            ),
            (
                ReportRagLifecycleEvent(
                    sequence_no=4,
                    operation="conversation_delete",
                    attempt_no=2,
                    success=True,
                    external_ref="thread-001",
                ),
            ),
        ]

        first = harness.service.cleanup(harness.task_id)
        after_first = harness.store.records[harness.task_id]
        second = harness.service.recover(harness.task_id)

        self.assertEqual(ReportResourceCleanupOutcome.PENDING, first.outcome)
        self.assertEqual("failed", after_first.external_state.value)
        self.assertEqual(ReportResourceCleanupOutcome.CLEANED, second.outcome)
        self.assertEqual(4, harness.rag.cleanup_commands[1].sequence_start)

    def test_artifact_pending_is_retried_without_repeating_external_cleanup(self) -> None:
        harness = _ResourceHarness()
        harness.track_complete_set()
        pending_ref = ReportArtifactRef(
            harness.task_id,
            "scratch/source/locked.pdf",
            ReportArtifactCategory.SOURCE,
        )
        harness.artifacts.cleanup_result = ReportArtifactCleanupResult(
            pending=(pending_ref,)
        )

        first = harness.service.cleanup(harness.task_id)
        harness.artifacts.cleanup_result = ReportArtifactCleanupResult()
        second = harness.service.recover(harness.task_id)

        self.assertEqual(ReportResourceCleanupOutcome.PENDING, first.outcome)
        self.assertEqual(1, first.pending_artifact_count)
        self.assertEqual(ReportResourceCleanupOutcome.CLEANED, second.outcome)
        self.assertEqual(1, len(harness.rag.cleanup_calls))
        self.assertEqual(2, len(harness.artifacts.cleanup_calls))

    def test_concurrent_recovery_runs_local_artifact_cleanup_only_once(self) -> None:
        """独立 sweeper 与执行 Worker 命中同一终态时不得重复文件副作用。"""

        harness = _ResourceHarness()
        harness.store.prepare_cleanup(harness.task_id)
        entered = threading.Event()
        release = threading.Event()

        class _BlockingArtifactPort(FakeReportArtifactPort):
            def cleanup_unretained(self, scope, *, retain):
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("测试未释放 Artifact cleanup")
                return super().cleanup_unretained(scope, retain=retain)

        artifacts = _BlockingArtifactPort(harness.recorder)
        service = ReportResourceRecoveryService(
            store=harness.store,
            artifacts=artifacts,
            rag=harness.rag,
            audit=harness.audit,
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(service.recover, harness.task_id)
            self.assertTrue(entered.wait(timeout=5))
            second = executor.submit(service.recover, harness.task_id)
            # 第二恢复者应等待同 TaskId 的 Artifact 临界区，而不是发起第二次删除。
            self.assertEqual(0, len(artifacts.cleanup_calls))
            release.set()
            first_result = first.result(timeout=5)
            second_result = second.result(timeout=5)

        self.assertEqual(ReportResourceCleanupOutcome.CLEANED, first_result.outcome)
        self.assertEqual(ReportResourceCleanupOutcome.CLEANED, second_result.outcome)
        self.assertEqual(1, len(artifacts.cleanup_calls))

    def test_expired_cleanup_lease_is_retried_under_idempotent_delete_contract(self) -> None:
        harness = _ResourceHarness()
        harness.track_complete_set()
        now = 1_000.0
        harness.service = ReportResourceRecoveryService(
            store=harness.store,
            artifacts=harness.artifacts,
            rag=harness.rag,
            audit=harness.audit,
            clock=lambda: now,
            external_attempt_timeout_seconds=30,
        )
        current = harness.store.records[harness.task_id]
        harness.store.save(
            replace(
                current,
                state=ReportResourceState.CLEANUP_PENDING,
                external_attempt_open=True,
                external_attempt_token="expired-attempt",
                external_attempt_started_at=now - 31,
                external_attempt_heartbeat_at=now - 31,
                attempt_count=1,
            ),
            expected_version=current.version,
        )

        with self.assertLogs(
            "app.modules.report.application.resource_recovery",
            level="WARNING",
        ):
            result = harness.service.recover(harness.task_id)
        record = harness.store.records[harness.task_id]

        self.assertEqual(ReportResourceCleanupOutcome.CLEANED, result.outcome)
        self.assertEqual(ReportResourceState.CLEANED, record.state)
        self.assertEqual(1, len(harness.rag.cleanup_calls))
        self.assertEqual(1, len(harness.artifacts.cleanup_calls))

    def test_expired_lease_takeover_yields_when_old_worker_heartbeats_first(self) -> None:
        """到期边界上的旧 Worker 续心优先时，新恢复者不得覆盖或重复删除。"""

        harness = _ResourceHarness()
        harness.track_complete_set()
        now = 1_000.0

        class _HeartbeatWinsStore(FakeReportResourceStorePort):
            def __init__(self):
                super().__init__(lambda _: harness.execution)
                self.inject_heartbeat_race = False

            def save(self, record, *, expected_version):
                current = self.records.get(record.task_id)
                if (
                    self.inject_heartbeat_race
                    and current is not None
                    and current.external_attempt_open
                    and not record.external_attempt_open
                ):
                    self.inject_heartbeat_race = False
                    # 模拟旧 Worker 在新恢复者关闭过期租约之前先完成一次 CAS 续心。
                    self.records[record.task_id] = replace(
                        current,
                        external_attempt_heartbeat_at=now,
                        version=current.version + 1,
                    )
                    raise ReportResourceConcurrencyError("旧 Worker 已先续心")
                return super().save(record, expected_version=expected_version)

        store = _HeartbeatWinsStore()
        store.records = dict(harness.store.records)
        service = ReportResourceRecoveryService(
            store=store,
            artifacts=harness.artifacts,
            rag=harness.rag,
            audit=harness.audit,
            clock=lambda: now,
            external_attempt_timeout_seconds=30,
        )
        current = store.records[harness.task_id]
        store.save(
            replace(
                current,
                state=ReportResourceState.CLEANUP_PENDING,
                external_attempt_open=True,
                external_attempt_token="boundary-attempt",
                external_attempt_started_at=now - 31,
                external_attempt_heartbeat_at=now - 31,
                attempt_count=1,
            ),
            expected_version=current.version,
        )
        store.inject_heartbeat_race = True

        result = service.recover(harness.task_id)
        latest = store.records[harness.task_id]

        self.assertEqual(ReportResourceCleanupOutcome.PENDING, result.outcome)
        self.assertTrue(latest.external_attempt_open)
        self.assertEqual(now, latest.external_attempt_heartbeat_at)
        self.assertEqual([], harness.rag.cleanup_calls)
        self.assertEqual([], harness.artifacts.cleanup_calls)

    def test_each_cleanup_event_is_persisted_before_the_next_external_step(self) -> None:
        """进程在第一项删除后退出时，已完成事件可审计，下一轮从后续序号恢复。"""

        harness = _ResourceHarness()
        harness.track_complete_set()

        class _InterruptAfterCheckpoint(FakeReportRagPort):
            def __init__(self, recorder):
                super().__init__(recorder)
                self.interrupted = False

            def cleanup(self, command):
                if not self.interrupted:
                    self.interrupted = True
                    self.cleanup_calls.append(command.cleanup_ref)
                    self.cleanup_commands.append(command)
                    event = ReportRagLifecycleEvent(
                        sequence_no=command.sequence_start or 2,
                        operation="context_delete",
                        attempt_no=1,
                        success=True,
                        external_ref="context-001",
                    )
                    assert command.event_checkpoint is not None
                    command.event_checkpoint(event)
                    raise RuntimeError("worker interrupted after checkpoint")
                return super().cleanup(command)

        harness.rag = _InterruptAfterCheckpoint(harness.recorder)
        harness.service = ReportResourceRecoveryService(
            store=harness.store,
            artifacts=harness.artifacts,
            rag=harness.rag,
            audit=harness.audit,
        )

        with self.assertLogs(
            "app.modules.report.application.resource_recovery",
            level="ERROR",
        ):
            first = harness.service.cleanup(harness.task_id)
        checkpointed = harness.store.records[harness.task_id]
        second = harness.service.recover(harness.task_id)

        self.assertEqual(ReportResourceCleanupOutcome.PENDING, first.outcome)
        self.assertEqual(ReportResourceState.AUDIT_PENDING, checkpointed.state)
        self.assertEqual(1, len(checkpointed.pending_events))
        self.assertEqual(2, checkpointed.pending_events[0].sequence_no)
        self.assertEqual(ReportResourceCleanupOutcome.CLEANED, second.outcome)
        self.assertEqual(3, harness.rag.cleanup_commands[1].sequence_start)
        self.assertEqual(2, len(harness.audit.append_calls))

    def test_live_external_cleanup_attempt_is_pending_instead_of_false_quarantine(self) -> None:
        """并发恢复遇到仍在租期内的调用时，只观察 pending 且绝不重复删除。"""

        harness = _ResourceHarness()
        entered = threading.Event()
        release = threading.Event()

        class _BlockingRagPort(FakeReportRagPort):
            def cleanup(self, command):
                entered.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("测试未释放外部清理")
                return super().cleanup(command)

        harness.rag = _BlockingRagPort(harness.recorder)
        harness.service = ReportResourceRecoveryService(
            store=harness.store,
            artifacts=harness.artifacts,
            rag=harness.rag,
            audit=harness.audit,
            external_attempt_timeout_seconds=30,
        )
        harness.track_complete_set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            first_future = executor.submit(harness.service.cleanup, harness.task_id)
            self.assertTrue(entered.wait(timeout=10))
            observed = harness.service.recover(harness.task_id)
            release.set()
            completed = first_future.result(timeout=10)

        self.assertEqual(ReportResourceCleanupOutcome.PENDING, observed.outcome)
        self.assertEqual(ReportResourceCleanupOutcome.CLEANED, completed.outcome)
        self.assertEqual(1, len(harness.rag.cleanup_calls))
        self.assertEqual(
            ReportResourceState.CLEANED,
            harness.store.records[harness.task_id].state,
        )

    def test_tracking_record_with_running_execution_is_not_cleaned(self) -> None:
        harness = _ResourceHarness(execution_state="running")
        result = harness.service.recover(harness.task_id)

        self.assertEqual(ReportResourceCleanupOutcome.NOT_READY, result.outcome)
        self.assertEqual(
            ReportResourceState.TRACKING,
            harness.store.records[harness.task_id].state,
        )
        self.assertEqual([], harness.artifacts.cleanup_calls)

    def test_repeated_quarantine_preserves_first_audit_reason(self) -> None:
        harness = _ResourceHarness()

        harness.service.quarantine(
            harness.task_id,
            stage="audit_gate",
            reason="first evidence is incomplete",
        )
        harness.service.quarantine(
            harness.task_id,
            stage="later_error",
            reason="must not overwrite first reason",
        )
        record = harness.store.records[harness.task_id]

        self.assertEqual(ReportResourceState.QUARANTINED, record.state)
        self.assertEqual("audit_gate", record.last_error_stage)
        self.assertEqual("first evidence is incomplete", record.last_error_message)

if __name__ == "__main__":
    unittest.main()
