"""阶段 1E-2：分类节点变更 SQLite 事实、CAS 与并发门禁。"""

from __future__ import annotations

import ast
import gc
import math
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.modules.reassign.adapters import SQLiteReassignmentRepository
from app.modules.reassign.domain import (
    ReassignDocumentCommand,
    ReassignmentContractError,
    ReassignmentDomainValidationError,
    ReassignmentMutationOutcome,
    ReassignmentOperationStatus,
    ReassignmentRawValue,
    ReassignmentStepName,
    ReassignmentStepState,
    ReassignmentTerminalEvidence,
    ReassignmentTerminalEvidenceKind,
    operation_holds_document_protection,
)
from app.modules.reassign.ports import (
    ReassignmentEventType,
    ReassignmentExpiredLeaseTakeoverRequest,
    ReassignmentLocalCommitRequest,
    ReassignmentOperationRecord,
    ReassignmentOperationTransition,
    ReassignmentRecoveryCursor,
    ReassignmentReservationOutcome,
    ReassignmentReservationRequest,
    ReassignmentStepCompletion,
    ReassignmentWorkspacePreparationClaim,
    ReassignmentWorkspacePreparationClaimOutcome,
    ReassignmentWorkspacePreparationClaimRequest,
    ReassignmentWorkspaceMappingRequest,
    ReassignmentWorkspaceOwnership,
    ReassignmentWorkspacePreparationFactRequest,
    ReassignmentWriteOutcome,
)
from app.services.core.database import DatabaseService


class AdjustableClock:
    """为 lease 与过期接管测试提供稳定、可推进的 UTC 时钟。"""

    def __init__(self) -> None:
        self.value = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)

    def expires_after(self, *, seconds: int = 3600) -> str:
        return (
            (self.value + timedelta(seconds=seconds))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )


class SQLiteReassignmentRepositoryTests(unittest.TestCase):
    """所有测试使用临时 SQLite；不调用 Flask、run.py 或任何网络服务。"""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="docsense-reassign-")
        self.db_path = Path(self._temp_dir.name) / "knowledge.sqlite3"
        self.database = DatabaseService(str(self.db_path))
        self.clock = AdjustableClock()
        self.repository = SQLiteReassignmentRepository(
            self.db_path,
            clock=self.clock,
        )

    def tearDown(self) -> None:
        # DatabaseService 的既有 sqlite3 事务上下文会在 Windows 上延迟释放
        # Cursor/Connection 引用环；这里先回收测试创建的临时对象，再删除临时库。
        # 这不会参与生产流程，也不会放宽任何业务断言。
        gc.collect()
        self._temp_dir.cleanup()

    def test_database_delete_guard_statuses_match_domain_protection_rule(self) -> None:
        """冻结跨模块删除门禁，防止未来新增活动状态时漏放受保护文档。"""

        expected = {
            status.value
            for status in ReassignmentOperationStatus
            if operation_holds_document_protection(status)
        }
        self.assertEqual(
            expected,
            set(DatabaseService._ACTIVE_REASSIGN_STATUSES),
        )

    def _save_document(
        self,
        *,
        file_name: str = "document.pdf",
        architecture_id: int = 11,
        doc_path: str | None = "/documents/document.pdf",
    ) -> None:
        self.database.save_document_record(
            file_name,
            architecture_id,
            anything_doc_id=f"doc-{architecture_id}-{file_name}",
            doc_path=doc_path or "",
            original_name="原始文件.pdf",
            # 与生产写入链路一致：文档记录必须保留实际入库文件名，
            # 否则会绕过 DatabaseService 对本地权威数据的完整性校验。
            ingested_file_name=f"ingested-{architecture_id}-{file_name}",
        )

    def _reservation(
        self,
        *,
        operation_id: str = "operation-1",
        file_name: str = "document.pdf",
        source_id: int = 11,
        target_id: object = 12,
        owner: str = "request-owner",
        token: str = "lease-token-1",
    ) -> ReassignmentOperationRecord:
        command = ReassignDocumentCommand(
            file_name=file_name,
            old_architecture_id_raw=source_id,
            new_architecture_id_raw=target_id,
            old_architecture_id_query_value=source_id,
        )
        request = ReassignmentReservationRequest(
            command=command,
            operation_id=operation_id,
            lease_owner=owner,
            lease_token=token,
            lease_expires_at=self.clock.expires_after(),
        )
        with self.repository.unit_of_work() as unit_of_work:
            result = unit_of_work.reserve(request)
        self.assertIs(result.outcome, ReassignmentReservationOutcome.ACQUIRED)
        self.assertIsInstance(result.record, ReassignmentOperationRecord)
        return result.record

    def _promote_to_running(
        self,
        record: ReassignmentOperationRecord,
    ) -> ReassignmentOperationRecord:
        with self.repository.unit_of_work() as unit_of_work:
            result = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=record.lease,
                    next_status=ReassignmentOperationStatus.RUNNING,
                    current_step=ReassignmentStepName.RESERVE_DOCUMENT,
                )
            )
        self.assertIsInstance(result, ReassignmentOperationRecord)
        return result

    def _begin_step(
        self,
        record: ReassignmentOperationRecord,
        step_name: ReassignmentStepName,
    ) -> None:
        with self.repository.unit_of_work() as unit_of_work:
            result = unit_of_work.begin_step_mutation(
                lease=record.lease,
                step_name=step_name,
            )
        self.assertNotIsInstance(result, ReassignmentWriteOutcome)

    def _acquire_workspace_preparation_claim(
        self,
        record: ReassignmentOperationRecord,
        *,
        target_id: object = 12,
        claim_token: str = "workspace-claim-token-1",
    ) -> ReassignmentWorkspacePreparationClaim:
        """测试新目标 mapping 前必须先取得跨实例持久化准备权。"""

        with self.repository.unit_of_work() as unit_of_work:
            result = unit_of_work.acquire_workspace_preparation_claim(
                ReassignmentWorkspacePreparationClaimRequest(
                    operation_lease=record.lease,
                    target_architecture_raw=target_id,
                    claim_token=claim_token,
                    claim_expires_at=record.lease.expires_at,
                )
            )
        self.assertNotIsInstance(result, ReassignmentWriteOutcome)
        self.assertIs(
            ReassignmentWorkspacePreparationClaimOutcome.ACQUIRED,
            result.outcome,
        )
        self.assertIsInstance(result.claim, ReassignmentWorkspacePreparationClaim)
        return result.claim

    def _complete_step_success(
        self,
        record: ReassignmentOperationRecord,
        step_name: ReassignmentStepName,
    ) -> None:
        """完成一个已开始步骤，并保存可审计的确定副作用事实。"""

        with self.repository.unit_of_work() as unit_of_work:
            result = unit_of_work.complete_step(
                ReassignmentStepCompletion(
                    lease=record.lease,
                    step_name=step_name,
                    next_state=ReassignmentStepState.SUCCEEDED,
                    probe_outcome=ReassignmentMutationOutcome.CONFIRMED_EFFECT,
                )
            )
        self.assertNotIsInstance(result, ReassignmentWriteOutcome)

    @staticmethod
    def _success_evidence() -> ReassignmentTerminalEvidence:
        return ReassignmentTerminalEvidence(
            ReassignmentTerminalEvidenceKind.FORWARD_SUCCESS_CONFIRMED
        )

    def test_schema_is_idempotent_and_audit_table_is_append_only(self) -> None:
        """重复构造 Adapter 不重建 documents，审计表禁止底层 UPDATE/DELETE。"""

        SQLiteReassignmentRepository(self.db_path, clock=self.clock)
        # sqlite3 的事务上下文只负责提交/回滚，不会关闭连接；Windows 下
        # 临时目录清理前必须显式关闭，避免把测试环境的文件句柄误报为实现缺陷。
        with closing(sqlite3.connect(self.db_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }
        self.assertTrue(
            {"reassign_operations", "reassign_steps", "reassign_events"} <= tables
        )
        self.assertTrue(
            {
                "reassign_events_append_only_update",
                "reassign_events_append_only_delete",
            }
            <= triggers
        )

    def test_schema_rebuilds_stale_active_operation_index_predicate(self) -> None:
        """旧库部分索引谓词过期时必须重建，不能静默保留错误并发约束。"""

        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute("DROP INDEX uq_reassign_active_document")
                connection.execute(
                    """
                    CREATE UNIQUE INDEX uq_reassign_active_document
                    ON reassign_operations(document_row_id)
                    WHERE status IN ('reserved', 'running')
                    """
                )

        SQLiteReassignmentRepository(self.db_path, clock=self.clock)

        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'index' AND name = 'uq_reassign_active_document'
                """
            ).fetchone()
        self.assertIsNotNone(row)
        index_sql = str(row[0])
        for status in (
            "reserved",
            "running",
            "compensating",
            "recovery_required",
        ):
            self.assertIn(f"'{status}'", index_sql)

    def test_schema_additively_upgrades_early_stage1e2_recovery_columns(self) -> None:
        """早期 1E-2 现场升级后必须保守冻结恢复和步骤尝试 fencing。"""

        self._save_document()
        record = self._promote_to_running(self._reservation())
        self._begin_step(record, ReassignmentStepName.DETACH_SOURCE_DOCUMENT)
        with self.repository.unit_of_work() as unit_of_work:
            unit_of_work.complete_step(
                ReassignmentStepCompletion(
                    lease=record.lease,
                    step_name=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                    next_state=ReassignmentStepState.OUTCOME_UNKNOWN,
                    probe_outcome=ReassignmentMutationOutcome.OUTCOME_UNKNOWN,
                )
            )
        with self.repository.unit_of_work() as unit_of_work:
            isolated = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=record.lease,
                    next_status=ReassignmentOperationStatus.RECOVERY_REQUIRED,
                    current_step=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                )
            )
        self.assertIsInstance(isolated, ReassignmentOperationRecord)

        # 模拟已经由早期 1E-2 初始化、但还没有 1E-2R 新列的开发库。这里只移除
        # 加法列，不删除 Operation/Step/Event 现场。
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                for table_name, column_name in (
                    ("reassign_operations", "recovery_required_fencing_token"),
                    ("reassign_steps", "last_attempt_fencing_token"),
                    ("reassign_events", "fencing_token"),
                    ("reassign_events", "attempt_count"),
                    ("reassign_events", "probe_outcome"),
                    ("reassign_events", "actor_digest"),
                    ("reassign_events", "reason_code"),
                ):
                    connection.execute(
                        f"ALTER TABLE {table_name} DROP COLUMN {column_name}"
                    )

        SQLiteReassignmentRepository(self.db_path, clock=self.clock)

        with self.repository.unit_of_work(read_only=True) as unit_of_work:
            upgraded_operation = unit_of_work.get_operation(
                record.operation.operation_id
            )
            upgraded_step = unit_of_work.get_step(
                operation_id=record.operation.operation_id,
                step_name=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            )
        self.assertIsNotNone(upgraded_operation)
        self.assertIsNotNone(upgraded_step)
        self.assertEqual(
            record.lease.fencing_token,
            upgraded_operation.recovery_required_fencing_token,
        )
        self.assertEqual(
            record.lease.fencing_token,
            upgraded_step.last_attempt_fencing_token,
        )

    def test_repository_rejects_infinite_busy_timeout(self) -> None:
        """启动配置不能把 Infinity 延迟到 PRAGMA 转换时才抛出底层异常。"""

        with self.assertRaisesRegex(ValueError, "busy_timeout_seconds"):
            SQLiteReassignmentRepository(
                self.db_path,
                clock=self.clock,
                busy_timeout_seconds=math.inf,
            )

    def test_same_thread_nested_sqlite_uow_is_rejected_immediately(self) -> None:
        """SQLite 与严格 Fake 都必须显式拒绝嵌套短事务，不能等待 busy timeout。"""

        with self.repository.unit_of_work():
            with self.assertRaisesRegex(ReassignmentContractError, "嵌套"):
                with self.repository.unit_of_work(read_only=True):
                    self.fail("嵌套 UoW 不应进入")

    def test_documents_rebuild_requires_coordinated_reassign_migration(self) -> None:
        """存在 reassign 外键事实后不得单独重建 documents 并改变冻结行 ID。"""

        legacy_path = Path(self._temp_dir.name) / "legacy-with-reassign.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE workspaces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    architecture_id INTEGER NOT NULL UNIQUE,
                    workspace_slug TEXT NOT NULL UNIQUE
                );
                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_name TEXT NOT NULL UNIQUE,
                    original_name TEXT NOT NULL DEFAULT '',
                    ingested_file_name TEXT NOT NULL DEFAULT '',
                    architecture_id INTEGER NOT NULL,
                    anything_doc_id TEXT NOT NULL,
                    doc_path TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
        SQLiteReassignmentRepository(legacy_path, clock=self.clock)
        with self.assertRaisesRegex(RuntimeError, "协调数据库迁移"):
            DatabaseService(str(legacy_path))

    def test_sqlite_adapter_has_no_network_or_knowledge_client_import(self) -> None:
        """持久化 Adapter 只能处理本地事实，防止后续把网络 I/O 偷放进短事务。"""

        source_path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "modules"
            / "reassign"
            / "adapters"
            / "sqlite_repository.py"
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

        forbidden_prefixes = (
            "requests",
            "httpx",
            "urllib",
            "socket",
            "flask",
            "app.integrations.anythingllm",
            "app.services.utils.anythingllm_client",
        )
        violations = sorted(
            module
            for module in imported_modules
            if any(
                module == prefix or module.startswith(f"{prefix}.")
                for prefix in forbidden_prefixes
            )
        )
        self.assertEqual([], violations)

    def test_reservation_freezes_document_creates_all_steps_and_records_source_mapping(self) -> None:
        """保留动作应一次性创建 Operation、8 个固定 Step 与第一个审计事件。"""

        self._save_document()
        self.database.add_workspace(11, "source-workspace")

        record = self._reservation()

        self.assertEqual("source-workspace", record.source_workspace_slug)
        self.assertEqual(ReassignmentOperationStatus.RESERVED, record.operation.status)
        self.assertEqual(ReassignmentStepName.RESERVE_DOCUMENT, record.operation.current_step)
        with self.repository.unit_of_work() as unit_of_work:
            steps = unit_of_work.list_steps(record.operation.operation_id)
            events = unit_of_work.list_events(record.operation.operation_id)
        self.assertEqual(len(ReassignmentStepName), len(steps))
        reserve_step = next(
            item
            for item in steps
            if item.step.step_name is ReassignmentStepName.RESERVE_DOCUMENT
        )
        self.assertEqual(ReassignmentStepState.SUCCEEDED, reserve_step.step.state)
        self.assertTrue(reserve_step.step.write_intent_recorded)
        self.assertEqual([1], [event.sequence_no for event in events])

    def test_fifty_concurrent_same_document_requests_have_exactly_one_owner(self) -> None:
        """部分唯一索引与 BEGIN IMMEDIATE 必须共同阻止同文档双 owner。"""

        self._save_document()
        barrier = threading.Barrier(50)

        def reserve(index: int) -> ReassignmentReservationOutcome:
            command = ReassignDocumentCommand(
                file_name="document.pdf",
                old_architecture_id_raw=11,
                new_architecture_id_raw=12,
                old_architecture_id_query_value=11,
            )
            request = ReassignmentReservationRequest(
                command=command,
                operation_id=f"concurrent-operation-{index}",
                lease_owner=f"owner-{index}",
                lease_token=f"token-{index}",
                lease_expires_at=self.clock.expires_after(),
            )
            barrier.wait(timeout=20)
            with self.repository.unit_of_work() as unit_of_work:
                return unit_of_work.reserve(request).outcome

        with ThreadPoolExecutor(max_workers=50) as executor:
            outcomes = list(executor.map(reserve, range(50)))

        self.assertEqual(1, outcomes.count(ReassignmentReservationOutcome.ACQUIRED))
        self.assertEqual(
            49,
            outcomes.count(ReassignmentReservationOutcome.ACTIVE_OPERATION_EXISTS),
        )

    def test_fifty_distinct_documents_can_be_reserved_independently(self) -> None:
        """不同文档不应因为全局内存锁或 file_name 误用而互相阻塞。"""

        for index in range(50):
            self._save_document(file_name=f"document-{index}.pdf")
        barrier = threading.Barrier(50)

        def reserve(index: int) -> ReassignmentReservationOutcome:
            file_name = f"document-{index}.pdf"
            command = ReassignDocumentCommand(
                file_name=file_name,
                old_architecture_id_raw=11,
                new_architecture_id_raw=12,
                old_architecture_id_query_value=11,
            )
            request = ReassignmentReservationRequest(
                command=command,
                operation_id=f"distinct-operation-{index}",
                lease_owner=f"owner-{index}",
                lease_token=f"token-{index}",
                lease_expires_at=self.clock.expires_after(),
            )
            barrier.wait(timeout=20)
            with self.repository.unit_of_work() as unit_of_work:
                return unit_of_work.reserve(request).outcome

        with ThreadPoolExecutor(max_workers=50) as executor:
            outcomes = list(executor.map(reserve, range(50)))

        self.assertEqual(
            [ReassignmentReservationOutcome.ACQUIRED] * 50,
            outcomes,
        )

    def test_same_file_name_in_different_architectures_does_not_share_operation_protection(self) -> None:
        """活动保护必须以 documents.id 为单位，不能误用 file_name。"""

        self._save_document(architecture_id=11)
        self._save_document(architecture_id=21)

        first = self._reservation(operation_id="operation-source-11")
        second = self._reservation(
            operation_id="operation-source-21",
            source_id=21,
            target_id=22,
            token="lease-token-21",
        )

        self.assertNotEqual(
            first.operation.document.document_row_id,
            second.operation.document.document_row_id,
        )

    def test_workspace_mapping_and_prepare_step_commit_atomically(self) -> None:
        """新 mapping、prepare Step 成功与 Operation 引用必须同一短事务收口。"""

        self._save_document()
        record = self._promote_to_running(self._reservation())
        self._begin_step(record, ReassignmentStepName.PREPARE_TARGET_WORKSPACE)
        claim = self._acquire_workspace_preparation_claim(record)

        with self.repository.unit_of_work() as unit_of_work:
            result = unit_of_work.record_workspace_mapping(
                ReassignmentWorkspaceMappingRequest(
                    lease=record.lease,
                    target_architecture_raw=12,
                    workspace_slug="target-workspace",
                    ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
                    preparation_claim=claim,
                )
            )
        self.assertIsInstance(result, ReassignmentOperationRecord)
        self.assertEqual("target-workspace", result.target_workspace_slug)
        self.assertIs(
            ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            result.target_workspace_ownership,
        )
        with self.repository.unit_of_work() as unit_of_work:
            self.assertEqual(
                "target-workspace",
                unit_of_work.get_workspace_slug(ReassignmentRawValue(12)),
            )
            step = unit_of_work.get_step(
                operation_id=record.operation.operation_id,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            )
            events = unit_of_work.list_events(record.operation.operation_id)
        self.assertIsNotNone(step)
        self.assertEqual(ReassignmentStepState.SUCCEEDED, step.step.state)
        self.assertEqual(
            ReassignmentMutationOutcome.CONFIRMED_EFFECT,
            step.probe_outcome,
        )
        self.assertEqual(record.lease.fencing_token, events[-1].fencing_token)
        self.assertEqual(step.attempt_count, events[-1].attempt_count)
        self.assertEqual(step.probe_outcome, events[-1].probe_outcome)

    def test_new_workspace_mapping_requires_persistent_target_claim(self) -> None:
        """没有目标分类唯一准备权时，Repository 必须拒绝直接写入新 mapping。"""

        self._save_document()
        record = self._promote_to_running(self._reservation())
        self._begin_step(record, ReassignmentStepName.PREPARE_TARGET_WORKSPACE)

        with self.repository.unit_of_work() as unit_of_work:
            result = unit_of_work.record_workspace_mapping(
                ReassignmentWorkspaceMappingRequest(
                    lease=record.lease,
                    target_architecture_raw=12,
                    workspace_slug="target-workspace",
                    ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
                )
            )
            persisted_mapping = unit_of_work.get_workspace_slug(
                ReassignmentRawValue(12)
            )

        self.assertIs(ReassignmentWriteOutcome.CONFLICT, result)
        self.assertIsNone(persisted_mapping)

    def test_mapping_conflict_can_persist_exact_remote_preparation_fact(
        self,
    ) -> None:
        """远端创建成功但 mapping 冲突时，恢复所需事实必须落库且 claim 保持活动。"""

        self._save_document()
        self.database.add_workspace(99, "CONFLICTING-TARGET-SLUG")
        record = self._promote_to_running(self._reservation())
        self._begin_step(record, ReassignmentStepName.PREPARE_TARGET_WORKSPACE)
        claim = self._acquire_workspace_preparation_claim(record)
        with self.repository.unit_of_work() as unit_of_work:
            conflict = unit_of_work.record_workspace_mapping(
                ReassignmentWorkspaceMappingRequest(
                    lease=record.lease,
                    target_architecture_raw=12,
                    workspace_slug="conflicting-target-slug",
                    ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
                    preparation_claim=claim,
                )
            )
        self.assertIs(ReassignmentWriteOutcome.CONFLICT, conflict)

        with self.repository.unit_of_work() as unit_of_work:
            fact = unit_of_work.record_workspace_preparation_fact(
                ReassignmentWorkspacePreparationFactRequest(
                    lease=record.lease,
                    workspace_slug="conflicting-target-slug",
                    ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
                    error_code="workspace_mapping_conflict",
                )
            )
        self.assertIsInstance(fact, ReassignmentOperationRecord)
        with self.repository.unit_of_work(read_only=True) as unit_of_work:
            step = unit_of_work.get_step(
                operation_id=record.operation.operation_id,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            )
            events = unit_of_work.list_events(record.operation.operation_id)
        self.assertEqual("conflicting-target-slug", fact.target_workspace_slug)
        self.assertIs(
            ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            fact.target_workspace_ownership,
        )
        self.assertEqual(ReassignmentStepState.MUTATION_STARTED, step.step.state)
        self.assertEqual("conflicting-target-slug", step.step.external_reference)
        fact_events = [
            event
            for event in events
            if event.event_type
            is ReassignmentEventType.WORKSPACE_PREPARATION_FACT_RECORDED
        ]
        self.assertEqual(1, len(fact_events))
        self.assertIs(
            ReassignmentMutationOutcome.CONFIRMED_EFFECT,
            fact_events[0].probe_outcome,
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            state = connection.execute(
                """
                SELECT state FROM reassign_workspace_preparation_claims
                WHERE operation_id = ?
                """,
                (record.operation.operation_id,),
            ).fetchone()
        self.assertEqual("active", state[0])

    def test_lease_renewal_extends_active_workspace_claim_atomically(self) -> None:
        """步骤边界续租必须同步延长当前 claim，不能让活请求被并发接管。"""

        self._save_document()
        record = self._promote_to_running(self._reservation())
        claim = self._acquire_workspace_preparation_claim(record)
        self.clock.advance(seconds=30)
        new_expiry = self.clock.expires_after(seconds=7200)

        with self.repository.unit_of_work() as unit_of_work:
            renewed = unit_of_work.renew_lease(
                lease=record.lease,
                lease_expires_at=new_expiry,
            )

        self.assertIs(ReassignmentWriteOutcome.APPLIED, renewed.outcome)
        self.assertEqual(new_expiry, renewed.lease.expires_at)
        self.assertIsNotNone(renewed.workspace_preparation_claim)
        self.assertEqual(
            claim.token,
            renewed.workspace_preparation_claim.token,
        )
        self.assertEqual(
            new_expiry,
            renewed.workspace_preparation_claim.expires_at,
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            persisted = connection.execute(
                """
                SELECT lease_expires_at, state
                FROM reassign_workspace_preparation_claims
                WHERE operation_id = ?
                """,
                (record.operation.operation_id,),
            ).fetchone()
        self.assertEqual((new_expiry, "active"), persisted)

    def test_target_preparation_claim_blocks_peer_and_supports_expired_takeover(self) -> None:
        """不同文档同目标分类只能串行创建；过期 owner 必须由更大 fencing 接管。"""

        self._save_document(file_name="first.pdf")
        self._save_document(file_name="second.pdf")
        first = self._promote_to_running(
            self._reservation(operation_id="operation-first", file_name="first.pdf")
        )
        second = self._promote_to_running(
            self._reservation(
                operation_id="operation-second",
                file_name="second.pdf",
                token="lease-token-second",
            )
        )
        with self.repository.unit_of_work() as unit_of_work:
            first_claim = unit_of_work.acquire_workspace_preparation_claim(
                ReassignmentWorkspacePreparationClaimRequest(
                    operation_lease=first.lease,
                    target_architecture_raw=12,
                    claim_token="claim-first",
                    # 故意小于 Operation lease，模拟创建进程失联后的独立 claim 过期。
                    claim_expires_at=self.clock.expires_after(seconds=10),
                )
            )
        self.assertIs(
            ReassignmentWorkspacePreparationClaimOutcome.ACQUIRED,
            first_claim.outcome,
        )
        with self.repository.unit_of_work() as unit_of_work:
            blocked = unit_of_work.acquire_workspace_preparation_claim(
                ReassignmentWorkspacePreparationClaimRequest(
                    operation_lease=second.lease,
                    target_architecture_raw=12,
                    claim_token="claim-second",
                    claim_expires_at=second.lease.expires_at,
                )
            )
        self.assertIs(
            ReassignmentWorkspacePreparationClaimOutcome.ACTIVE_CLAIM_EXISTS,
            blocked.outcome,
        )

        self.clock.advance(seconds=11)
        with self.repository.unit_of_work() as unit_of_work:
            taken_over = unit_of_work.acquire_workspace_preparation_claim(
                ReassignmentWorkspacePreparationClaimRequest(
                    operation_lease=second.lease,
                    target_architecture_raw=12,
                    claim_token="claim-second",
                    claim_expires_at=second.lease.expires_at,
                )
            )
        self.assertIs(
            ReassignmentWorkspacePreparationClaimOutcome.ACQUIRED,
            taken_over.outcome,
        )
        self.assertEqual(2, taken_over.claim.fencing_token)

    def test_unknown_workspace_ownership_is_persisted_without_fabricating_effect(
        self,
    ) -> None:
        """唯一 workspace 可继续使用，但未知创建者不能被写成“本次创建”或“明确复用”。"""

        self._save_document()
        record = self._promote_to_running(self._reservation())
        self._begin_step(record, ReassignmentStepName.PREPARE_TARGET_WORKSPACE)
        claim = self._acquire_workspace_preparation_claim(record)

        with self.repository.unit_of_work() as unit_of_work:
            result = unit_of_work.record_workspace_mapping(
                ReassignmentWorkspaceMappingRequest(
                    lease=record.lease,
                    target_architecture_raw=12,
                    workspace_slug="target-workspace",
                    ownership=ReassignmentWorkspaceOwnership.UNKNOWN,
                    preparation_claim=claim,
                )
            )
        self.assertIsInstance(result, ReassignmentOperationRecord)
        self.assertIs(
            ReassignmentWorkspaceOwnership.UNKNOWN,
            result.target_workspace_ownership,
        )
        with self.repository.unit_of_work(read_only=True) as unit_of_work:
            step = unit_of_work.get_step(
                operation_id=record.operation.operation_id,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            )
            reloaded = unit_of_work.get_operation(record.operation.operation_id)
        self.assertIsNotNone(step)
        self.assertIsNone(step.probe_outcome)
        self.assertIs(
            ReassignmentWorkspaceOwnership.UNKNOWN,
            reloaded.target_workspace_ownership,
        )

    def test_legacy_boolean_workspace_ownership_is_additively_migrated(self) -> None:
        """已初始化的早期 1E 开发库只加列回填，不重建或丢弃待恢复 Operation。"""

        self._save_document()
        record = self._promote_to_running(self._reservation())
        self._begin_step(record, ReassignmentStepName.PREPARE_TARGET_WORKSPACE)
        claim = self._acquire_workspace_preparation_claim(record)
        with self.repository.unit_of_work() as unit_of_work:
            mapped = unit_of_work.record_workspace_mapping(
                ReassignmentWorkspaceMappingRequest(
                    lease=record.lease,
                    target_architecture_raw=12,
                    workspace_slug="target-workspace",
                    ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
                    preparation_claim=claim,
                )
            )
        self.assertIsInstance(mapped, ReassignmentOperationRecord)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "ALTER TABLE reassign_operations "
                "ADD COLUMN target_workspace_created INTEGER"
            )
            connection.execute(
                """
                UPDATE reassign_operations
                SET target_workspace_created = 1,
                    target_workspace_ownership = NULL
                WHERE operation_id = ?
                """,
                (record.operation.operation_id,),
            )
            connection.commit()

        migrated_repository = SQLiteReassignmentRepository(
            self.db_path,
            clock=self.clock,
        )
        with migrated_repository.unit_of_work(read_only=True) as unit_of_work:
            migrated = unit_of_work.get_operation(record.operation.operation_id)
        self.assertIs(
            ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            migrated.target_workspace_ownership,
        )

    def test_existing_workspace_mapping_cannot_be_claimed_as_created_by_current_operation(self) -> None:
        """已有映射的创建归属必须可审计，不能让补偿误删共享 workspace。"""

        self._save_document()
        self.database.add_workspace(12, "existing-target-workspace")
        record = self._promote_to_running(self._reservation())
        self._begin_step(record, ReassignmentStepName.PREPARE_TARGET_WORKSPACE)

        with self.repository.unit_of_work() as unit_of_work:
            result = unit_of_work.record_workspace_mapping(
                ReassignmentWorkspaceMappingRequest(
                    lease=record.lease,
                    target_architecture_raw=12,
                    workspace_slug="existing-target-workspace",
                    ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
                )
            )
            step = unit_of_work.get_step(
                operation_id=record.operation.operation_id,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            )

        self.assertIs(result, ReassignmentWriteOutcome.CONFLICT)
        self.assertIsNotNone(step)
        self.assertEqual(ReassignmentStepState.MUTATION_STARTED, step.step.state)

    def test_operation_history_is_not_a_document_foreign_key(self) -> None:
        """终态 Operation 是独立审计快照，不应在业务文档删除后形成悬空外键。"""

        with closing(sqlite3.connect(self.db_path)) as connection:
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(reassign_operations)"
            ).fetchall()
        self.assertNotIn("documents", {str(row[2]) for row in foreign_keys})

    def test_legacy_document_foreign_key_is_removed_without_losing_audit_facts(
        self,
    ) -> None:
        """升级早期 1E 库时必须保留 Operation 与子表事实，只移除错误的文档外键。"""

        self._save_document()
        record = self._reservation(operation_id="legacy-document-fk")
        self._begin_step(record, ReassignmentStepName.DETACH_SOURCE_DOCUMENT)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            before_step_count = connection.execute(
                "SELECT COUNT(*) FROM reassign_steps WHERE operation_id = ?",
                (record.operation.operation_id,),
            ).fetchone()[0]
            before_event_count = connection.execute(
                "SELECT COUNT(*) FROM reassign_events WHERE operation_id = ?",
                (record.operation.operation_id,),
            ).fetchone()[0]
            create_sql = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'reassign_operations'"
            ).fetchone()[0]
            legacy_create_sql = create_sql.replace(
                "CREATE TABLE reassign_operations",
                "CREATE TABLE reassign_operations_with_document_fk",
                1,
            )
            closing_parenthesis = legacy_create_sql.rfind(")")
            legacy_create_sql = (
                legacy_create_sql[:closing_parenthesis]
                + ", FOREIGN KEY(document_row_id) REFERENCES documents(id)"
                + legacy_create_sql[closing_parenthesis:]
            )
            connection.execute(legacy_create_sql)
            columns = [
                str(row[1])
                for row in connection.execute("PRAGMA table_info(reassign_operations)")
            ]
            column_sql = ", ".join(columns)
            connection.execute(
                f"INSERT INTO reassign_operations_with_document_fk ({column_sql}) "
                f"SELECT {column_sql} FROM reassign_operations"
            )
            connection.execute("DROP TABLE reassign_operations")
            connection.execute(
                "ALTER TABLE reassign_operations_with_document_fk "
                "RENAME TO reassign_operations"
            )
            connection.commit()

        SQLiteReassignmentRepository(self.db_path, clock=self.clock)
        with closing(sqlite3.connect(self.db_path)) as connection:
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(reassign_operations)"
            ).fetchall()
            operation_count = connection.execute(
                "SELECT COUNT(*) FROM reassign_operations WHERE operation_id = ?",
                (record.operation.operation_id,),
            ).fetchone()[0]
            step_count = connection.execute(
                "SELECT COUNT(*) FROM reassign_steps WHERE operation_id = ?",
                (record.operation.operation_id,),
            ).fetchone()[0]
            event_count = connection.execute(
                "SELECT COUNT(*) FROM reassign_events WHERE operation_id = ?",
                (record.operation.operation_id,),
            ).fetchone()[0]
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()

        self.assertNotIn("documents", {str(row[2]) for row in foreign_keys})
        self.assertEqual(1, operation_count)
        self.assertEqual(before_step_count, step_count)
        self.assertEqual(before_event_count, event_count)
        self.assertGreater(event_count, 0)
        self.assertEqual([], violations)

    def test_document_delete_rejects_active_operation_but_allows_terminal_history(self) -> None:
        """删除门禁只保护活动 Saga；成功终态保留后仍允许删除业务行。"""

        self._save_document(doc_path="")
        record = self._promote_to_running(self._reservation(operation_id="delete-guard"))
        with self.assertRaisesRegex(RuntimeError, "活动中的分类变更"):
            self.database.delete_document_record(
                "document.pdf",
                architecture_id=11,
            )

        self._begin_step(record, ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE)
        with self.repository.unit_of_work() as unit_of_work:
            committed = unit_of_work.commit_local_architecture(
                ReassignmentLocalCommitRequest(
                    lease=record.lease,
                    expected_document=record.operation.document,
                    target_architecture_raw=record.operation.target_architecture_raw,
                    terminal_evidence=self._success_evidence(),
                )
            )
        self.assertIsInstance(committed, ReassignmentOperationRecord)

        self.database.delete_document_record("document.pdf", architecture_id=12)
        self.assertIsNone(
            self.database.get_document_record("document.pdf", architecture_id=12)
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            operation_count = connection.execute(
                "SELECT COUNT(*) FROM reassign_operations WHERE operation_id = ?",
                ("delete-guard",),
            ).fetchone()[0]
        self.assertEqual([], violations)
        self.assertEqual(1, operation_count)

    def test_document_delete_holds_write_lock_while_checking_reassign_guard(self) -> None:
        """活动门禁与 DELETE 必须共用写事务，禁止并发保留从检查/删除间隙穿过。"""

        guard_entered = threading.Event()
        allow_delete = threading.Event()

        class BlockingDeleteDatabase(DatabaseService):
            """只在测试中暂停门禁，暴露删除事务是否已经持有写锁。"""

            def _ensure_documents_not_reassigning(
                self,
                connection: sqlite3.Connection,
                document_row_ids: tuple[int, ...],
            ) -> None:
                super()._ensure_documents_not_reassigning(
                    connection,
                    document_row_ids,
                )
                guard_entered.set()
                if not allow_delete.wait(timeout=2):
                    raise TimeoutError("测试未及时释放删除事务")

        self._save_document()
        blocking_database = BlockingDeleteDatabase(str(self.db_path))
        with ThreadPoolExecutor(max_workers=1) as executor:
            deletion = executor.submit(
                blocking_database.delete_document_record,
                "document.pdf",
                architecture_id=11,
            )
            self.assertTrue(guard_entered.wait(timeout=2))
            try:
                with closing(
                    sqlite3.connect(self.db_path, timeout=0, isolation_level=None)
                ) as contender:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        contender.execute("BEGIN IMMEDIATE")
            finally:
                allow_delete.set()
            deletion.result(timeout=2)

    def test_remote_document_cannot_commit_before_required_steps_are_confirmed(self) -> None:
        """可构造的终态枚举不能替代目标 workspace 与成员关系持久事实。"""

        self._save_document()
        record = self._promote_to_running(self._reservation())
        self._begin_step(record, ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE)

        with self.assertRaisesRegex(
            ReassignmentContractError,
            "目标 workspace",
        ):
            with self.repository.unit_of_work() as unit_of_work:
                unit_of_work.commit_local_architecture(
                    ReassignmentLocalCommitRequest(
                        lease=record.lease,
                        expected_document=record.operation.document,
                        target_architecture_raw=12,
                        terminal_evidence=self._success_evidence(),
                    )
                )

    def test_generic_transition_cannot_release_document_protection(self) -> None:
        """调用方不能用一个终态枚举绕过专用成功/补偿事实校验。"""

        self._save_document(doc_path="")
        record = self._promote_to_running(self._reservation())
        with self.assertRaisesRegex(ReassignmentContractError, "专用提交方法"):
            with self.repository.unit_of_work() as unit_of_work:
                unit_of_work.transition_operation(
                    ReassignmentOperationTransition(
                        lease=record.lease,
                        next_status=ReassignmentOperationStatus.SUCCEEDED,
                        terminal_evidence=self._success_evidence(),
                    )
                )

    def test_remote_document_commits_after_required_steps_are_confirmed(self) -> None:
        """来源解绑、目标准备和目标加入全部确认后才允许原子提交本地成功。"""

        self._save_document()
        self.database.add_workspace(11, "source-workspace")
        record = self._promote_to_running(self._reservation())
        self._begin_step(record, ReassignmentStepName.DETACH_SOURCE_DOCUMENT)
        self._complete_step_success(
            record,
            ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
        )
        self._begin_step(record, ReassignmentStepName.PREPARE_TARGET_WORKSPACE)
        claim = self._acquire_workspace_preparation_claim(record)
        with self.repository.unit_of_work() as unit_of_work:
            mapped = unit_of_work.record_workspace_mapping(
                ReassignmentWorkspaceMappingRequest(
                    lease=record.lease,
                    target_architecture_raw=12,
                    workspace_slug="target-workspace",
                    ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
                    preparation_claim=claim,
                )
            )
        self.assertIsInstance(mapped, ReassignmentOperationRecord)
        self._begin_step(mapped, ReassignmentStepName.ATTACH_TARGET_DOCUMENT)
        self._complete_step_success(
            mapped,
            ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
        )
        self._begin_step(mapped, ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE)
        with self.repository.unit_of_work() as unit_of_work:
            result = unit_of_work.commit_local_architecture(
                ReassignmentLocalCommitRequest(
                    lease=mapped.lease,
                    expected_document=mapped.operation.document,
                    target_architecture_raw=12,
                    terminal_evidence=self._success_evidence(),
                )
            )
        self.assertIsInstance(result, ReassignmentOperationRecord)
        self.assertIs(
            ReassignmentOperationStatus.SUCCEEDED,
            result.operation.status,
        )

    def test_terminal_operation_rejects_lease_renewal_and_new_steps(self) -> None:
        """终态保留 lease 历史字段，但不得据此继续获得任何写入权。"""

        self._save_document(doc_path="")
        record = self._promote_to_running(self._reservation())
        self._begin_step(record, ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE)
        with self.repository.unit_of_work() as unit_of_work:
            succeeded = unit_of_work.commit_local_architecture(
                ReassignmentLocalCommitRequest(
                    lease=record.lease,
                    expected_document=record.operation.document,
                    target_architecture_raw=12,
                    terminal_evidence=self._success_evidence(),
                )
            )
        self.assertIsInstance(succeeded, ReassignmentOperationRecord)
        with self.repository.unit_of_work() as unit_of_work:
            renewed = unit_of_work.renew_lease(
                lease=succeeded.lease,
                lease_expires_at=self.clock.expires_after(seconds=7200),
            )
            late_step = unit_of_work.begin_step_mutation(
                lease=succeeded.lease,
                step_name=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            )
        self.assertIs(ReassignmentWriteOutcome.CONFLICT, renewed.outcome)
        self.assertIs(ReassignmentWriteOutcome.CONFLICT, late_step)

    def test_recovery_required_needs_takeover_fencing_before_reentry(self) -> None:
        """原 owner 不能靠 recovery_authorized 布尔值自行离开恢复隔离。"""

        self._save_document()
        record = self._reservation()
        with self.repository.unit_of_work() as unit_of_work:
            quarantined = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=record.lease,
                    next_status=ReassignmentOperationStatus.RECOVERY_REQUIRED,
                    current_step=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                )
            )
        self.assertIsInstance(quarantined, ReassignmentOperationRecord)
        with self.repository.unit_of_work() as unit_of_work:
            denied = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=quarantined.lease,
                    next_status=ReassignmentOperationStatus.RUNNING,
                    recovery_authorized=True,
                )
            )
        self.assertIs(ReassignmentWriteOutcome.CONFLICT, denied)

        self.clock.advance(seconds=3601)
        with self.repository.unit_of_work() as unit_of_work:
            takeover = unit_of_work.take_over_expired_lease(
                ReassignmentExpiredLeaseTakeoverRequest(
                    operation_id=record.operation.operation_id,
                    expected_fencing_token=record.lease.fencing_token,
                    lease_owner="recovery-owner",
                    lease_token="recovery-token",
                    lease_expires_at=self.clock.expires_after(),
                    reason_code="recovery_scan",
                )
            )
        self.assertIs(ReassignmentWriteOutcome.APPLIED, takeover.outcome)
        with self.repository.unit_of_work() as unit_of_work:
            resumed = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=takeover.lease,
                    next_status=ReassignmentOperationStatus.RUNNING,
                    recovery_authorized=True,
                )
            )
        self.assertIsInstance(resumed, ReassignmentOperationRecord)
        self.assertEqual(
            record.lease.fencing_token + 1,
            resumed.lease.fencing_token,
        )

    def test_local_cas_conflict_does_not_change_document_or_mark_operation_success(self) -> None:
        """冻结快照发生变化时，rowcount=0 只能返回冲突并保留可恢复现场。"""

        # 本用例只验证本地 CAS；空 doc_path 明确选择既有 local-only 兼容路径。
        self._save_document(doc_path="")
        record = self._promote_to_running(self._reservation())
        self._begin_step(record, ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE)
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    "UPDATE documents SET doc_path = ? WHERE file_name = ? AND architecture_id = ?",
                    ("/documents/changed.pdf", "document.pdf", 11),
                )

        with self.repository.unit_of_work() as unit_of_work:
            result = unit_of_work.commit_local_architecture(
                ReassignmentLocalCommitRequest(
                    lease=record.lease,
                    expected_document=record.operation.document,
                    target_architecture_raw=12,
                    terminal_evidence=self._success_evidence(),
                )
            )
        self.assertIs(result, ReassignmentWriteOutcome.CONFLICT)
        document = self.database.get_document_record(
            "document.pdf",
            architecture_id=11,
        )
        self.assertIsNotNone(document)
        with self.repository.unit_of_work() as unit_of_work:
            operation = unit_of_work.get_operation(record.operation.operation_id)
        self.assertIsNotNone(operation)
        self.assertEqual(ReassignmentOperationStatus.RUNNING, operation.operation.status)

    def test_local_cas_success_is_atomic_and_preserves_frozen_non_strict_target_compatibility(self) -> None:
        """CAS 成功必须同时迁移文档、收口 Step/Operation，并兼容已冻结的新 ID 原始值。"""

        # 1E-0 已冻结 ``"12"`` 与 ``false`` 不触发新的严格参数校验。
        # 此处验证的是 SQLite 实际存储亲和性，而不是向公开接口增加新解释规则。
        cases = ((12, 12), ("12", 12), (False, 0))
        for index, (target_raw, persisted_architecture_id) in enumerate(cases):
            with self.subTest(target_raw=target_raw):
                file_name = f"success-{index}.pdf"
                self._save_document(file_name=file_name, doc_path="")
                record = self._promote_to_running(
                    self._reservation(
                        operation_id=f"success-operation-{index}",
                        file_name=file_name,
                        target_id=target_raw,
                    )
                )
                self._begin_step(
                    record,
                    ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                )

                with self.repository.unit_of_work() as unit_of_work:
                    result = unit_of_work.commit_local_architecture(
                        ReassignmentLocalCommitRequest(
                            lease=record.lease,
                            expected_document=record.operation.document,
                            target_architecture_raw=target_raw,
                            terminal_evidence=self._success_evidence(),
                        )
                    )
                    step = unit_of_work.get_step(
                        operation_id=record.operation.operation_id,
                        step_name=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                    )

                self.assertIsInstance(result, ReassignmentOperationRecord)
                self.assertEqual(
                    ReassignmentOperationStatus.SUCCEEDED,
                    result.operation.status,
                )
                self.assertIsNotNone(result.finished_at)
                self.assertIsNotNone(step)
                self.assertEqual(
                    ReassignmentStepState.SUCCEEDED,
                    step.step.state,
                )
                self.assertIsNone(
                    self.database.get_document_record(file_name, architecture_id=11)
                )
                self.assertIsNotNone(
                    self.database.get_document_record(
                        file_name,
                        architecture_id=persisted_architecture_id,
                    )
                )

    def test_local_cas_rejects_target_outside_sqlite_integer_range(self) -> None:
        """超大整数必须在保留 Operation 前失败，不能污染恢复队列。"""

        self._save_document(doc_path="")
        with self.assertRaises(ReassignmentDomainValidationError):
            self._reservation(
                operation_id="oversized-target",
                target_id=2**63,
            )
        with self.repository.unit_of_work(read_only=True) as unit_of_work:
            self.assertIsNone(unit_of_work.get_operation("oversized-target"))
        self.assertIsNotNone(
            self.database.get_document_record("document.pdf", architecture_id=11)
        )

    def test_local_cas_step_and_operation_roll_back_together_when_checkpoint_write_fails(self) -> None:
        """documents 更新后 Step 写失败时，事务必须回滚，不能留下虚假本地成功。"""

        self._save_document(doc_path="")
        record = self._promote_to_running(self._reservation(operation_id="atomic-op"))
        self._begin_step(record, ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE)
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TRIGGER inject_reassign_step_failure
                    BEFORE UPDATE ON reassign_steps
                    WHEN NEW.operation_id = 'atomic-op'
                    BEGIN
                        SELECT RAISE(ABORT, 'injected step checkpoint failure');
                    END
                    """
                )

        with self.assertRaises(sqlite3.IntegrityError):
            with self.repository.unit_of_work() as unit_of_work:
                unit_of_work.commit_local_architecture(
                    ReassignmentLocalCommitRequest(
                        lease=record.lease,
                        expected_document=record.operation.document,
                        target_architecture_raw=12,
                        terminal_evidence=self._success_evidence(),
                    )
                )

        self.assertIsNotNone(
            self.database.get_document_record("document.pdf", architecture_id=11)
        )
        self.assertIsNone(
            self.database.get_document_record("document.pdf", architecture_id=12)
        )
        with self.repository.unit_of_work() as unit_of_work:
            operation = unit_of_work.get_operation("atomic-op")
            step = unit_of_work.get_step(
                operation_id="atomic-op",
                step_name=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
            )
        self.assertEqual(ReassignmentOperationStatus.RUNNING, operation.operation.status)
        self.assertEqual(ReassignmentStepState.MUTATION_STARTED, step.step.state)

    def test_expired_lease_takeover_rejects_old_fencing_token(self) -> None:
        """接管后旧请求即使仍持有 operation_id 也不能继续写入步骤。"""

        self._save_document()
        record = self._reservation()
        self.clock.advance(seconds=3601)

        with self.repository.unit_of_work() as unit_of_work:
            result = unit_of_work.take_over_expired_lease(
                ReassignmentExpiredLeaseTakeoverRequest(
                    operation_id=record.operation.operation_id,
                    expected_fencing_token=record.lease.fencing_token,
                    lease_owner="recovery-owner",
                    lease_token="recovery-token",
                    lease_expires_at=self.clock.expires_after(),
                )
            )
        self.assertIs(result.outcome, ReassignmentWriteOutcome.APPLIED)
        self.assertIsNotNone(result.lease)
        self.assertEqual(record.lease.fencing_token + 1, result.lease.fencing_token)

        with self.repository.unit_of_work() as unit_of_work:
            stale_result = unit_of_work.begin_step_mutation(
                lease=record.lease,
                step_name=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            )
        self.assertIs(stale_result, ReassignmentWriteOutcome.STALE_LEASE)

    def test_known_failure_retry_requires_new_fencing_and_clears_probe_fact(self) -> None:
        """受控重试必须来自接管后的新 fencing，且不能继承上一轮探测结果。"""

        self._save_document()
        record = self._promote_to_running(self._reservation())
        self._begin_step(record, ReassignmentStepName.DETACH_SOURCE_DOCUMENT)
        with self.repository.unit_of_work() as unit_of_work:
            unit_of_work.complete_step(
                ReassignmentStepCompletion(
                    lease=record.lease,
                    step_name=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                    next_state=ReassignmentStepState.KNOWN_FAILED,
                    probe_outcome=(
                        ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
                    ),
                )
            )
        with self.repository.unit_of_work() as unit_of_work:
            denied = unit_of_work.begin_step_mutation(
                lease=record.lease,
                step_name=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                recovery_authorized=True,
            )
        self.assertIs(ReassignmentWriteOutcome.CONFLICT, denied)

        self.clock.advance(seconds=3601)
        with self.repository.unit_of_work() as unit_of_work:
            takeover = unit_of_work.take_over_expired_lease(
                ReassignmentExpiredLeaseTakeoverRequest(
                    operation_id=record.operation.operation_id,
                    expected_fencing_token=record.lease.fencing_token,
                    lease_owner="recovery-owner",
                    lease_token="recovery-token",
                    lease_expires_at=self.clock.expires_after(),
                )
            )
        with self.repository.unit_of_work() as unit_of_work:
            retried = unit_of_work.begin_step_mutation(
                lease=takeover.lease,
                step_name=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                recovery_authorized=True,
            )
        self.assertNotIsInstance(retried, ReassignmentWriteOutcome)
        self.assertIsNone(retried.probe_outcome)
        self.assertEqual(
            takeover.lease.fencing_token,
            retried.last_attempt_fencing_token,
        )

    def test_read_only_recovery_scan_is_bounded_and_rejects_writes(self) -> None:
        """恢复发现使用稳定游标与只读事务，不应获取写权限或接受无界 limit。"""

        for index in range(3):
            self._save_document(file_name=f"recover-{index}.pdf")
            self._reservation(
                operation_id=f"recover-operation-{index}",
                file_name=f"recover-{index}.pdf",
                token=f"recover-token-{index}",
            )
        self.clock.advance(seconds=3601)
        with self.repository.unit_of_work(read_only=True) as unit_of_work:
            first_page = unit_of_work.list_recoverable_operations(limit=2)
            with self.assertRaisesRegex(ReassignmentContractError, "只读"):
                unit_of_work.renew_lease(
                    lease=first_page[0].lease,
                    lease_expires_at=self.clock.expires_after(),
                )
        self.assertEqual(2, len(first_page))
        cursor = ReassignmentRecoveryCursor(
            lease_expires_at=first_page[-1].lease.expires_at,
            operation_id=first_page[-1].operation.operation_id,
        )
        with self.repository.unit_of_work(read_only=True) as unit_of_work:
            second_page = unit_of_work.list_recoverable_operations(
                limit=2,
                cursor=cursor,
            )
            with self.assertRaisesRegex(ValueError, "1 到 500"):
                unit_of_work.list_recoverable_operations(limit=501)
        self.assertEqual(1, len(second_page))
        self.assertTrue(unit_of_work.read_only)

    def test_duplicate_operation_id_is_contract_error_not_concurrency_result(self) -> None:
        """SQLite 与严格 Fake 必须一致拒绝 operation_id 复用。"""

        self._save_document()
        self._reservation(operation_id="duplicate-operation")
        with self.assertRaisesRegex(ReassignmentContractError, "operation_id"):
            self._reservation(operation_id="duplicate-operation")

    def test_audit_events_have_strict_sequence_and_database_rejects_mutation(self) -> None:
        """审计序号只能递增，底层 UPDATE/DELETE 同样被触发器拒绝。"""

        self._save_document()
        record = self._reservation()
        with self.repository.unit_of_work() as unit_of_work:
            unit_of_work.renew_lease(
                lease=record.lease,
                lease_expires_at=self.clock.expires_after(seconds=7200),
            )
        with self.repository.unit_of_work() as unit_of_work:
            events = unit_of_work.list_events(record.operation.operation_id)
        self.assertEqual([1, 2], [event.sequence_no for event in events])
        self.assertEqual(record.lease.fencing_token, events[-1].fencing_token)
        self.assertIsNotNone(events[-1].actor_digest)

        with self.assertRaises(sqlite3.IntegrityError):
            with closing(sqlite3.connect(self.db_path)) as connection:
                with connection:
                    connection.execute(
                        "UPDATE reassign_events SET detail_code = 'tamper' WHERE operation_id = ?",
                        (record.operation.operation_id,),
                    )
        with self.assertRaises(sqlite3.IntegrityError):
            with closing(sqlite3.connect(self.db_path)) as connection:
                with connection:
                    connection.execute(
                        "DELETE FROM reassign_events WHERE operation_id = ?",
                        (record.operation.operation_id,),
                    )


if __name__ == "__main__":
    unittest.main()
