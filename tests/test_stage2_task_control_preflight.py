"""阶段 2 旧 Task SQLite 只读预检的 fail-closed 合同测试。"""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "preflight_stage2_task_control.py"
SPEC = importlib.util.spec_from_file_location("stage2_task_control_preflight", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - 仅防御损坏的 Python 环境
    raise RuntimeError("无法装载阶段 2 预检脚本")
PREFLIGHT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFLIGHT)


def _create_old_database(path: Path) -> None:
    """建立只包含预检必要列的旧库夹具，避免测试依赖生产 Store 初始化副作用。"""

    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE llm_task_executions (
                execution_state TEXT NOT NULL,
                callback_status TEXT NOT NULL
            );
            CREATE TABLE callback_delivery_guards (state TEXT NOT NULL);
            CREATE TABLE report_resource_records (state TEXT NOT NULL);
            CREATE TABLE weaponry_creation_intents (state TEXT NOT NULL);
            """
        )
        connection.commit()
    finally:
        connection.close()


class Stage2TaskControlPreflightTests(unittest.TestCase):
    """验证安全现场、阻塞现场、未知状态和路径门禁。"""

    def test_settled_old_database_is_safe_and_unchanged(self) -> None:
        """全终态现场通过，且预检前后主库元数据与侧车集合保持不变。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.sqlite3"
            new_path = root / "db" / "task-control-v2.sqlite3"
            _create_old_database(old_path)
            connection = sqlite3.connect(old_path)
            try:
                connection.execute(
                    "INSERT INTO llm_task_executions VALUES ('succeeded', 'success')"
                )
                connection.execute("INSERT INTO callback_delivery_guards VALUES ('idle')")
                connection.execute("INSERT INTO report_resource_records VALUES ('cleaned')")
                connection.execute("INSERT INTO weaponry_creation_intents VALUES ('resolved')")
                connection.commit()
            finally:
                connection.close()

            before = PREFLIGHT._file_set_snapshot(old_path)
            result = PREFLIGHT.inspect_old_database(old_path, new_path)
            after = PREFLIGHT._file_set_snapshot(old_path)

            self.assertEqual("safe_for_empty_v2_initialization", result["status"])
            self.assertEqual(0, result["blockerCount"])
            self.assertEqual(before, after)
            self.assertFalse(new_path.exists())
            self.assertFalse(result["rowIdentityIncluded"])

    def test_active_unknown_and_uncleaned_facts_block_cutover(self) -> None:
        """活动执行、unknown Guard 和未清理资源任一存在都必须停止切换。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.sqlite3"
            new_path = root / "new.sqlite3"
            _create_old_database(old_path)
            connection = sqlite3.connect(old_path)
            try:
                connection.execute(
                    "INSERT INTO llm_task_executions VALUES ('running', 'pending')"
                )
                connection.execute(
                    "INSERT INTO callback_delivery_guards VALUES ('outcome_unknown')"
                )
                connection.execute(
                    "INSERT INTO report_resource_records VALUES ('cleanup_pending')"
                )
                connection.execute(
                    "INSERT INTO weaponry_creation_intents VALUES ('quarantined')"
                )
                connection.commit()
            finally:
                connection.close()

            result = PREFLIGHT.inspect_old_database(old_path, new_path)

            self.assertEqual("blocked_facts_require_reconciliation", result["status"])
            self.assertGreaterEqual(result["blockerCount"], 5)

    def test_unknown_state_fails_closed(self) -> None:
        """旧库出现契约外枚举时不得把未知事实当成空闲现场。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.sqlite3"
            _create_old_database(old_path)
            connection = sqlite3.connect(old_path)
            try:
                connection.execute(
                    "INSERT INTO llm_task_executions VALUES ('mystery', 'success')"
                )
                connection.commit()
            finally:
                connection.close()

            result = PREFLIGHT.inspect_old_database(old_path, root / "new.sqlite3")

            self.assertEqual("invalid_fail_closed", result["status"])
            self.assertIn(
                "unknown_state_value",
                {item["code"] for item in result["schemaErrors"]},
            )

    def test_same_old_and_new_path_is_rejected(self) -> None:
        """同一路径即使使用不同文本表示也不允许原地初始化 v2。"""

        with tempfile.TemporaryDirectory() as directory:
            old_path = Path(directory) / "old.sqlite3"
            _create_old_database(old_path)

            with self.assertRaises(PREFLIGHT.PreflightInputError):
                PREFLIGHT.inspect_old_database(old_path, old_path.parent / "." / old_path.name)

    def test_immutable_mode_requires_explicit_stopped_writer_confirmation(self) -> None:
        """immutable 不能成为绕过 WAL/并发保护的静默开关。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.sqlite3"
            _create_old_database(old_path)

            with self.assertRaises(PREFLIGHT.PreflightInputError):
                PREFLIGHT.inspect_old_database(
                    old_path,
                    root / "new.sqlite3",
                    immutable_offline_snapshot=True,
                )


if __name__ == "__main__":
    unittest.main()
