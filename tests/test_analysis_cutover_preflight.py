"""阶段 1F-7A：Analysis 路由切换前只读门禁的离线验收。"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.services.llm_service.task_service import LLMTaskService
from scripts.inspect_analysis_cutover import inspect_analysis_cutover


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "inspect_analysis_cutover.py"


class AnalysisCutoverPreflightTests(unittest.TestCase):
    """通过子进程执行真实 CLI，确认其只读边界和 fail-closed 语义。"""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="docsense-analysis-cutover-"
        )
        self.db_path = Path(self._temporary_directory.name) / "knowledge.sqlite3"

    def tearDown(self) -> None:
        # SQLite 连接由被测 Service 自行关闭；显式回收可避免 Windows 临时目录删除时仍有
        # 延迟释放的对象句柄。
        gc.collect()
        self._temporary_directory.cleanup()

    def _initialize_required_schema(self) -> None:
        """只在测试准备阶段初始化真实 Schema，不让被测脚本承担这项工作。"""

        LLMTaskService(str(self.db_path))

    def _run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """用当前 venv 解释器运行脚本，固定 UTF-8 以便稳定断言 JSON。"""

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

    @staticmethod
    def _database_digest(database_path: Path) -> str:
        """读取数据库文件摘要，用于验证脚本未改写主数据库文件。"""

        return hashlib.sha256(database_path.read_bytes()).hexdigest()

    def _checkpoint_before_read_only_assertion(self) -> None:
        """在测试准备写入结束后收敛 WAL，避免将延迟 checkpoint 误判为脚本写入。"""

        with sqlite3.connect(self.db_path) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _insert_task(
        self,
        *,
        execution_id: str,
        business_key: str,
        status: str,
        updated_at: str,
        callback_status: str = "pending",
    ) -> None:
        """写入一条公开投影任务；business_key 故意带敏感样例以验证不被导出。"""

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO llm_tasks (
                    business_type, business_key, execution_id, request_payload,
                    status, callback_status, created_at, updated_at
                ) VALUES ('file', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    business_key,
                    execution_id,
                    '{"fileName":"secret-design.pdf","token":"never-export"}',
                    status,
                    callback_status,
                    "2026-07-27T08:00:00+00:00",
                    updated_at,
                ),
            )

    def _insert_current_batch_execution(
        self,
        *,
        execution_id: str,
        business_key: str,
        execution_state: str = "accepted",
    ) -> None:
        """写入 1F-4 新批次事实，确保它不会被旧任务门禁误判。"""

        with sqlite3.connect(self.db_path) as connection:
            dispatch_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(dispatch_sequence), 0) + 1 "
                    "FROM llm_task_executions"
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO llm_task_executions (
                    execution_id, business_type, business_key, input_schema_version,
                    input_payload, batch_id, batch_sequence, dispatch_sequence,
                    execution_state, public_status, trace_id, created_at, updated_at
                ) VALUES (?, 'file', ?, 1, '{}', ?, 1, ?, ?, '0', ?, ?, ?)
                """,
                (
                    execution_id,
                    business_key,
                    hashlib.md5(
                        execution_id.encode("utf-8"),
                        usedforsecurity=False,
                    ).hexdigest(),
                    dispatch_sequence,
                    execution_state,
                    "trace-current-batch",
                    "2026-07-27T08:00:00+00:00",
                    "2026-07-27T08:00:00+00:00",
                ),
            )

    def _insert_callback_guard(
        self,
        *,
        business_key: str,
        owner_execution_id: str,
        state: str,
        lease_version: int,
        updated_at: str,
    ) -> None:
        """写入 Guard 事实；业务键包含敏感样例但不得出现在脚本输出。"""

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO callback_delivery_guards (
                    business_type, business_key, owner_execution_id, state,
                    lease_version, updated_at
                ) VALUES ('file', ?, ?, ?, ?, ?)
                """,
                (
                    business_key,
                    owner_execution_id,
                    state,
                    lease_version,
                    updated_at,
                ),
            )

    def _insert_rag_lease(
        self,
        *,
        execution_id: str,
        business_key: str,
        status: str,
        updated_at: str,
    ) -> None:
        """写入资源租约事实；真实资源引用不应进入只读预检结果。"""

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO rag_resource_leases (
                    execution_id, business_type, business_key, context_ref,
                    conversation_ref, document_ref, external_location, status,
                    created_at, updated_at
                ) VALUES (?, 'file', ?, 'context-secret', 'conversation-secret',
                    'document-secret', '/secret/path', ?, ?, ?)
                """,
                (
                    execution_id,
                    business_key,
                    status,
                    "2026-07-27T08:00:00+00:00",
                    updated_at,
                ),
            )

    def test_ready_database_returns_zero_and_does_not_change_file(self) -> None:
        """三类历史积压均为零时才能通过，脚本本身不得产生写入。"""

        self._initialize_required_schema()
        self._checkpoint_before_read_only_assertion()
        before_digest = self._database_digest(self.db_path)

        completed = self._run_script("--database", str(self.db_path))

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual("dry_run", payload["mode"])
        self.assertTrue(payload["ready"])
        self.assertEqual(
            {
                "legacyActiveTasks": 0,
                "legacyRecoverableCallbacks": 0,
                "newRunningExecutions": 0,
                "callbackGuardBlockers": 0,
                "openLegacyRagLeases": 0,
            },
            payload["counts"],
        )
        self.assertEqual(
            {"newAcceptedExecutions": 0},
            payload["observations"],
        )
        self.assertEqual(before_digest, self._database_digest(self.db_path))

    def test_library_call_closes_read_only_connection_immediately(self) -> None:
        """库函数返回后必须释放句柄，避免 Windows 部署窗口无法替换数据库。"""

        self._initialize_required_schema()
        # 先释放测试准备阶段的临时 Service 对象，随后不再执行 GC；这样重命名断言只
        # 衡量被测预检函数是否自行关闭了只读连接。
        gc.collect()

        payload = inspect_analysis_cutover(database_path=self.db_path)

        self.assertTrue(payload["ready"])
        moved_path = self.db_path.with_suffix(".moved")
        self.db_path.rename(moved_path)
        moved_path.rename(self.db_path)

    def test_blockers_are_stably_bounded_and_never_export_sensitive_payloads(self) -> None:
        """历史任务、Guard、租约任一非零都应阻断，且输出仅含定位所需最小字段。"""

        self._initialize_required_schema()
        self._insert_task(
            execution_id="legacy-task-b",
            business_key="secret-file-b.pdf",
            status="1",
            updated_at="2026-07-27T08:02:00+00:00",
        )
        self._insert_task(
            execution_id="legacy-task-a",
            business_key="secret-file-a.pdf",
            status="0",
            updated_at="2026-07-27T08:01:00+00:00",
        )
        # 新批次的活跃公开投影不是历史残留，不能误阻断本次切换门禁。
        self._insert_task(
            execution_id="current-batch-task",
            business_key="current-file.pdf",
            status="1",
            updated_at="2026-07-27T08:00:00+00:00",
        )
        self._insert_current_batch_execution(
            execution_id="current-batch-task",
            business_key="current-file.pdf",
        )
        self._insert_task(
            execution_id="legacy-callback-task",
            business_key="legacy-callback-secret.pdf",
            status="2",
            callback_status="failed",
            updated_at="2026-07-27T08:03:00+00:00",
        )
        self._insert_task(
            execution_id="current-running-task",
            business_key="current-running-secret.pdf",
            status="1",
            updated_at="2026-07-27T08:04:00+00:00",
        )
        self._insert_current_batch_execution(
            execution_id="current-running-task",
            business_key="current-running-secret.pdf",
            execution_state="running",
        )
        self._insert_callback_guard(
            business_key="callback-secret-b.pdf",
            owner_execution_id="callback-owner-b",
            state="outcome_unknown",
            lease_version=2,
            updated_at="2026-07-27T08:02:00+00:00",
        )
        self._insert_callback_guard(
            business_key="callback-secret-a.pdf",
            owner_execution_id="callback-owner-a",
            state="sending",
            lease_version=1,
            updated_at="2026-07-27T08:01:00+00:00",
        )
        self._insert_callback_guard(
            business_key="idle-guard.pdf",
            owner_execution_id="idle-owner",
            state="idle",
            lease_version=1,
            updated_at="2026-07-27T08:00:00+00:00",
        )
        self._insert_rag_lease(
            execution_id="lease-b",
            business_key="lease-secret-b.pdf",
            status="audited",
            updated_at="2026-07-27T08:02:00+00:00",
        )
        self._insert_rag_lease(
            execution_id="lease-a",
            business_key="lease-secret-a.pdf",
            status="active",
            updated_at="2026-07-27T08:01:00+00:00",
        )
        self._insert_rag_lease(
            execution_id="lease-closed",
            business_key="closed-lease.pdf",
            status="closed",
            updated_at="2026-07-27T08:00:00+00:00",
        )
        self._checkpoint_before_read_only_assertion()
        before_digest = self._database_digest(self.db_path)

        completed = self._run_script("--database", str(self.db_path), "--limit", "1")

        self.assertEqual(3, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ready"])
        self.assertEqual(
            {
                "legacyActiveTasks": 2,
                "legacyRecoverableCallbacks": 1,
                "newRunningExecutions": 1,
                "callbackGuardBlockers": 2,
                "openLegacyRagLeases": 2,
            },
            payload["counts"],
        )
        self.assertEqual(
            {"newAcceptedExecutions": 1},
            payload["observations"],
        )
        self.assertEqual("legacy-task-a", payload["legacyActiveTasks"][0]["executionId"])
        self.assertEqual(
            "legacy-callback-task",
            payload["legacyRecoverableCallbacks"][0]["executionId"],
        )
        self.assertEqual(
            "current-running-task",
            payload["newRunningExecutions"][0]["executionId"],
        )
        self.assertEqual("callback-owner-a", payload["callbackGuards"][0]["ownerExecutionId"])
        self.assertEqual("lease-a", payload["openLegacyRagLeases"][0]["executionId"])
        self.assertEqual(
            {
                "legacyActiveTasks": True,
                "legacyRecoverableCallbacks": False,
                "newAcceptedExecutions": False,
                "newRunningExecutions": False,
                "callbackGuards": True,
                "openLegacyRagLeases": True,
            },
            payload["truncated"],
        )
        serialized_payload = completed.stdout + completed.stderr
        for forbidden_value in (
            "secret-design.pdf",
            "never-export",
            "callback-secret-a.pdf",
            "/secret/path",
            "context-secret",
        ):
            self.assertNotIn(forbidden_value, serialized_payload)
        self.assertEqual(before_digest, self._database_digest(self.db_path))

    def test_missing_schema_fails_closed_without_initializing_tables(self) -> None:
        """误传未迁移库时只报错，不得因一次读取创建任何预检所需表。"""

        with sqlite3.connect(self.db_path) as connection:
            connection.execute("CREATE TABLE unrelated_records (id INTEGER PRIMARY KEY)")
        before_digest = self._database_digest(self.db_path)

        completed = self._run_script("--database", str(self.db_path))

        self.assertEqual(1, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertIn("Analysis 切换只读预检失败", completed.stderr)
        self.assertEqual(before_digest, self._database_digest(self.db_path))
        with sqlite3.connect(self.db_path) as connection:
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual({"unrelated_records"}, table_names)


if __name__ == "__main__":  # pragma: no cover - 允许离线单文件执行。
    unittest.main()
