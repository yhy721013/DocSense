"""阶段 1E-5：人工恢复诊断脚本的只读边界验收。"""

from __future__ import annotations

import gc
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.modules.reassign.adapters import SQLiteReassignmentRepository
from app.modules.reassign.application import ReassignmentRecoveryResultCategory
from app.modules.reassign.domain import (
    ReassignDocumentCommand,
    ReassignmentOperationStatus,
    ReassignmentStepName,
)
from app.modules.reassign.ports import (
    ReassignmentOperationTransition,
    ReassignmentReservationOutcome,
    ReassignmentReservationRequest,
)
from app.services.core.database import DatabaseService
from scripts.inspect_reassign_operations import _recovery_exit_code


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "inspect_reassign_operations.py"


class AdjustableClock:
    """提供可推进的 UTC 时钟，使 dry-run 无需等待真实 lease 到期。"""

    def __init__(self) -> None:
        self.value = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)

    def expires_after(self, *, seconds: int) -> str:
        return (
            (self.value + timedelta(seconds=seconds))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )


class ReassignmentDiagnosticScriptTests(unittest.TestCase):
    """子进程执行真实脚本，但只使用临时 SQLite 与离线 Python 运行时。"""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="docsense-reassign-diagnostic-"
        )
        self.db_path = Path(self._temporary_directory.name) / "knowledge.sqlite3"
        self.database = DatabaseService(str(self.db_path))

    def tearDown(self) -> None:
        gc.collect()
        self._temporary_directory.cleanup()

    def _run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """以项目 venv 的当前解释器调用脚本，并固定 UTF-8 便于 JSON 断言。"""

        environment = dict(os.environ)
        environment["PYTHONUTF8"] = "1"
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT_PATH), *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def _create_expired_operation(self) -> str:
        """创建一条已过期但未终结的 Operation，供只读扫描验证。"""

        self.database.save_document_record(
            "document.pdf",
            11,
            anything_doc_id="doc-1",
            doc_path="/documents/document.pdf",
            original_name="原始文件.pdf",
            ingested_file_name="ingested-document.pdf",
        )
        clock = AdjustableClock()
        repository = SQLiteReassignmentRepository(self.db_path, clock=clock)
        command = ReassignDocumentCommand(
            file_name="document.pdf",
            old_architecture_id_raw=11,
            old_architecture_id_query_value=11,
            new_architecture_id_raw=12,
        )
        with repository.unit_of_work() as unit_of_work:
            reserved = unit_of_work.reserve(
                ReassignmentReservationRequest(
                    command=command,
                    operation_id="operation-diagnostic-script",
                    lease_owner="forward-owner",
                    lease_token="forward-token",
                    lease_expires_at=clock.expires_after(seconds=30),
                )
            )
        self.assertIs(ReassignmentReservationOutcome.ACQUIRED, reserved.outcome)
        assert reserved.record is not None
        with repository.unit_of_work() as unit_of_work:
            running = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=reserved.record.lease,
                    next_status=ReassignmentOperationStatus.RUNNING,
                    current_step=ReassignmentStepName.RESERVE_DOCUMENT,
                )
            )
        self.assertIs(ReassignmentOperationStatus.RUNNING, running.operation.status)
        clock.advance(seconds=31)
        return reserved.record.operation.operation_id

    def test_dry_run_lists_expired_operation_without_appending_audit_or_changing_state(self) -> None:
        """默认命令只读列出候选，不创建客户端、不追加审计，也不尝试接管。"""

        operation_id = self._create_expired_operation()
        with sqlite3.connect(self.db_path) as connection:
            before_event_count = connection.execute(
                "SELECT COUNT(*) FROM reassign_events"
            ).fetchone()[0]
            before_status = connection.execute(
                "SELECT status FROM reassign_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()[0]

        completed = self._run_script("--database", str(self.db_path), "--limit", "10")

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual("dry_run", payload["mode"])
        self.assertEqual(1, payload["count"])
        self.assertEqual(operation_id, payload["operations"][0]["operationId"])
        self.assertNotIn("fileName", payload["operations"][0])
        self.assertNotIn("docPath", payload["operations"][0])
        self.assertNotIn("leaseToken", payload["operations"][0])

        with sqlite3.connect(self.db_path) as connection:
            after_event_count = connection.execute(
                "SELECT COUNT(*) FROM reassign_events"
            ).fetchone()[0]
            after_status = connection.execute(
                "SELECT status FROM reassign_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()[0]
        self.assertEqual(before_event_count, after_event_count)
        self.assertEqual(before_status, after_status)

    def test_dry_run_does_not_initialize_missing_reassign_schema(self) -> None:
        """未迁移数据库只能报错，不能因一次查看就写入 1E 表或索引。"""

        with sqlite3.connect(self.db_path) as connection:
            before = connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'reassign_operations'"
            ).fetchone()
        self.assertIsNone(before)

        completed = self._run_script("--database", str(self.db_path))

        self.assertNotEqual(0, completed.returncode)
        with sqlite3.connect(self.db_path) as connection:
            after = connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'reassign_operations'"
            ).fetchone()
        self.assertIsNone(after)

    def test_apply_without_explicit_audit_and_fencing_arguments_is_rejected_before_recovery(self) -> None:
        """写模式漏掉任一关键条件时由 argparse 直接拒绝，绝不读取或修改 Operation。"""

        operation_id = self._create_expired_operation()
        completed = self._run_script(
            "--database",
            str(self.db_path),
            "--apply",
            "--operation-id",
            operation_id,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("--expected-fencing-token", completed.stderr)

    def test_apply_exit_codes_distinguish_completed_and_unresolved_recovery(self) -> None:
        """机器调用必须能仅凭退出码区分已收口、未接管和仍待恢复。"""

        completed_categories = (
            ReassignmentRecoveryResultCategory.RECOVERED_SUCCEEDED,
            ReassignmentRecoveryResultCategory.RECOVERED_FAILED_NO_SIDE_EFFECT,
            ReassignmentRecoveryResultCategory.COMPENSATED,
        )
        for category in completed_categories:
            with self.subTest(category=category.value):
                self.assertEqual(0, _recovery_exit_code(category))

        self.assertEqual(
            3,
            _recovery_exit_code(
                ReassignmentRecoveryResultCategory.OPERATION_NOT_FOUND
            ),
        )
        self.assertEqual(
            4,
            _recovery_exit_code(
                ReassignmentRecoveryResultCategory.TAKEOVER_REJECTED
            ),
        )
        self.assertEqual(
            5,
            _recovery_exit_code(
                ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED
            ),
        )


if __name__ == "__main__":  # pragma: no cover - 允许单文件离线执行。
    unittest.main()
