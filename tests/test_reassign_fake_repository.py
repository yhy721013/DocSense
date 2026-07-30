"""阶段 1E-2：Repository 严格 Fake 与 SQLite 兼容语义的离线回归。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.modules.reassign.domain import (
    ReassignDocumentCommand,
    ReassignmentContractError,
    ReassignmentDomainValidationError,
    ReassignmentDocumentSnapshot,
    ReassignmentOperationStatus,
    ReassignmentStepName,
    ReassignmentTerminalEvidence,
    ReassignmentTerminalEvidenceKind,
)
from app.modules.reassign.ports import (
    ReassignmentLocalCommitRequest,
    ReassignmentOperationRecord,
    ReassignmentOperationTransition,
    ReassignmentReservationRequest,
)
from tests.fakes.reassign import FakeReassignmentRepository


class FakeReassignmentRepositoryCompatibilityTests(unittest.TestCase):
    """Fake 必须复现本阶段已冻结的原始 ID 兼容边界，不能比 SQLite 更严格。"""

    def setUp(self) -> None:
        # 固定时钟使 lease 用例不依赖执行机器的实际日期或时区。
        self.clock = lambda: datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

    def _create_repository(self) -> FakeReassignmentRepository:
        """构造带源 workspace 和单条权威文档的最小离线现场。"""

        return FakeReassignmentRepository(
            documents=(
                ReassignmentDocumentSnapshot(
                    document_row_id=1,
                    file_name="document.pdf",
                    source_architecture_id=11,
                    anything_doc_id="doc-1",
                    doc_path="",
                    original_file_name="原始文件.pdf",
                ),
            ),
            workspace_mappings=((11, "source-workspace"),),
            clock=self.clock,
        )

    @staticmethod
    def _reservation_request(
        *,
        operation_id: str,
        old_raw: object = 11,
        target_raw: object,
    ) -> ReassignmentReservationRequest:
        """沿用接口的旧 ID 查询值与原始新 ID，避免测试偷偷规范化入参。"""

        return ReassignmentReservationRequest(
            command=ReassignDocumentCommand(
                file_name="document.pdf",
                old_architecture_id_raw=old_raw,
                new_architecture_id_raw=target_raw,
                old_architecture_id_query_value=11,
            ),
            operation_id=operation_id,
            lease_owner="test-owner",
            lease_token=f"token-{operation_id}",
            lease_expires_at="2026-07-24T13:00:00.000000Z",
        )

    def _promote_and_begin_local_commit(
        self,
        repository: FakeReassignmentRepository,
        record: ReassignmentOperationRecord,
    ) -> ReassignmentOperationRecord:
        """把最小 Operation 推进到本地 CAS 前的合法检查点。"""

        with repository.unit_of_work() as unit_of_work:
            running = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=record.lease,
                    next_status=ReassignmentOperationStatus.RUNNING,
                    current_step=ReassignmentStepName.RESERVE_DOCUMENT,
                )
            )
        self.assertIsInstance(running, ReassignmentOperationRecord)
        with repository.unit_of_work() as unit_of_work:
            unit_of_work.begin_step_mutation(
                lease=running.lease,
                step_name=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
            )
        return running

    def test_source_workspace_uses_old_id_query_value_not_raw_string_key(self) -> None:
        """旧 ID 原始值为字符串时，Fake 仍须命中 SQLite 会命中的整数 workspace 映射。"""

        repository = self._create_repository()
        with repository.unit_of_work() as unit_of_work:
            result = unit_of_work.reserve(
                self._reservation_request(
                    operation_id="raw-source-string",
                    old_raw="11",
                    target_raw=12,
                )
            )

        self.assertIsNotNone(result.record)
        self.assertEqual("source-workspace", result.record.source_workspace_slug)

    def test_local_commit_keeps_non_strict_new_id_compatibility_for_numeric_string_and_boolean(self) -> None:
        """``\"12\"`` 与 ``false`` 必须按现有 SQLite 存储亲和性完成本地提交。"""

        cases = (("12", 12), (False, 0))
        for index, (target_raw, persisted_architecture_id) in enumerate(cases):
            with self.subTest(target_raw=target_raw):
                repository = self._create_repository()
                with repository.unit_of_work() as unit_of_work:
                    reservation = unit_of_work.reserve(
                        self._reservation_request(
                            operation_id=f"raw-target-{index}",
                            target_raw=target_raw,
                        )
                    )
                self.assertIsNotNone(reservation.record)
                running = self._promote_and_begin_local_commit(
                    repository,
                    reservation.record,
                )
                with repository.unit_of_work() as unit_of_work:
                    result = unit_of_work.commit_local_architecture(
                        ReassignmentLocalCommitRequest(
                            lease=running.lease,
                            expected_document=running.operation.document,
                            target_architecture_raw=target_raw,
                            terminal_evidence=ReassignmentTerminalEvidence(
                                ReassignmentTerminalEvidenceKind.FORWARD_SUCCESS_CONFIRMED
                            ),
                        )
                    )
                    persisted = unit_of_work.get_document_snapshot(
                        file_name="document.pdf",
                        source_architecture_id=persisted_architecture_id,
                    )

                self.assertIsInstance(result, ReassignmentOperationRecord)
                self.assertEqual(
                    ReassignmentOperationStatus.SUCCEEDED,
                    result.operation.status,
                )
                self.assertIsNotNone(persisted)

    def test_fake_rejects_target_outside_sqlite_integer_range(self) -> None:
        """Fake 不得接受生产 SQLite 无法绑定的超大整数并伪造成功。"""

        target_raw = 2**63
        with self.assertRaisesRegex(
            ReassignmentDomainValidationError,
            "new_architecture_id_raw.*64",
        ):
            self._reservation_request(
                operation_id="oversized-target",
                target_raw=target_raw,
            )

    def test_duplicate_operation_id_fails_fast_instead_of_overwriting_audit_history(self) -> None:
        """严格 Fake 不得允许重复 ID 覆盖已有 Operation、步骤和只追加审计。"""

        repository = self._create_repository()
        request = self._reservation_request(
            operation_id="duplicate-operation",
            target_raw=12,
        )
        with repository.unit_of_work() as unit_of_work:
            unit_of_work.reserve(request)

        with self.assertRaisesRegex(ReassignmentContractError, "operation_id"):
            with repository.unit_of_work() as unit_of_work:
                unit_of_work.reserve(request)

    def test_fake_audit_keeps_fencing_on_reservation_and_transition(self) -> None:
        """严格 Fake 的恢复审计最小事实必须与 SQLite Adapter 一致。"""

        repository = self._create_repository()
        with repository.unit_of_work() as unit_of_work:
            reservation = unit_of_work.reserve(
                self._reservation_request(
                    operation_id="audit-fencing",
                    target_raw=12,
                )
            )
        self.assertIsNotNone(reservation.record)
        with repository.unit_of_work() as unit_of_work:
            unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=reservation.record.lease,
                    next_status=ReassignmentOperationStatus.RUNNING,
                    current_step=ReassignmentStepName.RESERVE_DOCUMENT,
                )
            )
        with repository.unit_of_work(read_only=True) as unit_of_work:
            events = unit_of_work.list_events("audit-fencing")

        self.assertEqual(2, len(events))
        self.assertEqual(
            [reservation.record.lease.fencing_token] * 2,
            [event.fencing_token for event in events],
        )


if __name__ == "__main__":
    unittest.main()
