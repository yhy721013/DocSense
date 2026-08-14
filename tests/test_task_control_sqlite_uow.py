"""阶段 2-2 第 2 步：Connection Factory、Transaction Manager 与窄 UoW。"""

from __future__ import annotations

import ast
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from app.modules.tasks.adapters.sqlite.bootstrap import bootstrap_task_control_database
from app.modules.tasks.adapters.sqlite.connection import (
    SQLiteConnectionFactory,
    SQLiteConnectionFactoryError,
)
from app.modules.tasks.adapters.sqlite.transaction import (
    SQLiteBusyError,
    SQLiteTransactionError,
    SQLiteTransactionManager,
)
from app.modules.tasks.adapters.sqlite.unit_of_work import (
    SQLiteTaskAdmissionUnitOfWorkFactory,
    SQLiteTaskExecutionUnitOfWorkFactory,
    SQLiteTaskRecoveryUnitOfWorkFactory,
)
from app.modules.tasks.ports.unit_of_work import (
    TaskAdmissionUnitOfWorkFactory,
    TaskExecutionUnitOfWorkFactory,
    TaskRecoveryUnitOfWorkFactory,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ORIGINAL_TIME = "2026-08-12T01:02:03.123456Z"
_CHANGED_TIME = "2026-08-12T02:03:04.654321Z"


class _ConnectionProbeStore:
    """仅用于证明 Store 共享连接和事务边界，不模拟任何业务语义。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def set_metadata_time(self, value: str) -> None:
        self.connection.execute(
            "UPDATE task_control_schema_metadata SET created_at = ? WHERE metadata_id = 1",
            (value,),
        )


class TaskControlSQLiteInfrastructureTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.old_path = self.root / "old.sqlite3"
        self.new_path = self.root / "task-control.sqlite3"
        sqlite3.connect(self.old_path).close()
        result = bootstrap_task_control_database(self.old_path, self.new_path)
        # 固定一个可观测值，后续测试只验证事务是否提交/回滚，不依赖 Bootstrap 当前时间。
        connection = sqlite3.connect(self.new_path)
        connection.execute(
            "UPDATE task_control_schema_metadata SET created_at = ? WHERE metadata_id = 1",
            (_ORIGINAL_TIME,),
        )
        connection.commit()
        connection.close()
        # metadata 内容改变不影响 schema_version，但 Bootstrap 身份摘要包含 created_at，需重新
        # 严格打开一次获得新的预期身份，模拟真实进程只使用 Bootstrap 交付的完整身份。
        result = bootstrap_task_control_database(self.old_path, self.new_path)
        self.connection_factory = SQLiteConnectionFactory(
            result,
            busy_timeout_ms=30,
        )
        self.transaction_manager = SQLiteTransactionManager(self.connection_factory)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _read_metadata_time_raw(self) -> str:
        connection = sqlite3.connect(self.new_path)
        try:
            return str(
                connection.execute(
                    "SELECT created_at FROM task_control_schema_metadata WHERE metadata_id = 1"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def _execution_factory(self) -> SQLiteTaskExecutionUnitOfWorkFactory:
        return SQLiteTaskExecutionUnitOfWorkFactory(
            self.transaction_manager,
            execution_builder=_ConnectionProbeStore,
            callback_delivery_builder=_ConnectionProbeStore,
        )


class SQLiteConnectionFactoryTests(TaskControlSQLiteInfrastructureTestCase):
    def test_connections_are_independent_thread_bound_and_configured(self) -> None:
        first = self.connection_factory.open()
        second = self.connection_factory.open(read_only=True)
        try:
            self.assertIsNot(first, second)
            self.assertEqual(1, first.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual(30, first.execute("PRAGMA busy_timeout").fetchone()[0])
            self.assertEqual(1, second.execute("PRAGMA query_only").fetchone()[0])
            with self.assertRaises(sqlite3.OperationalError):
                second.execute(
                    "UPDATE task_control_schema_metadata SET created_at = ?",
                    (_CHANGED_TIME,),
                )

            errors: list[BaseException] = []

            def use_from_another_thread() -> None:
                try:
                    first.execute("SELECT 1")
                except BaseException as exc:  # 测试需要跨线程回传真实 sqlite 异常。
                    errors.append(exc)

            thread = threading.Thread(target=use_from_another_thread)
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(1, len(errors))
            self.assertIsInstance(errors[0], sqlite3.ProgrammingError)
        finally:
            second.close()
            first.close()

    def test_runtime_schema_or_metadata_drift_is_rejected_without_repair(self) -> None:
        connection = sqlite3.connect(self.new_path)
        connection.execute("CREATE TABLE out_of_band_schema_change (id INTEGER)")
        connection.commit()
        connection.close()
        with self.assertRaises(SQLiteConnectionFactoryError) as raised:
            self.connection_factory.open()
        self.assertEqual("database_schema_version_drift", raised.exception.code)
        verification = sqlite3.connect(self.new_path)
        try:
            self.assertTrue(
                verification.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='out_of_band_schema_change'"
                ).fetchone()
            )
        finally:
            verification.close()


class SQLiteTransactionManagerTests(TaskControlSQLiteInfrastructureTestCase):
    def test_normal_exit_without_commit_rolls_back(self) -> None:
        with self.transaction_manager.begin() as transaction:
            transaction.connection.execute(
                "UPDATE task_control_schema_metadata SET created_at = ?",
                (_CHANGED_TIME,),
            )
        self.assertEqual(_ORIGINAL_TIME, self._read_metadata_time_raw())

    def test_explicit_commit_persists_and_exception_rolls_back(self) -> None:
        with self.transaction_manager.begin() as transaction:
            transaction.connection.execute(
                "UPDATE task_control_schema_metadata SET created_at = ?",
                (_CHANGED_TIME,),
            )
            transaction.commit()
        self.assertEqual(_CHANGED_TIME, self._read_metadata_time_raw())

        # 本测试不再使用旧 Factory 打开新事务，因为显式修改 metadata 正应触发身份漂移。
        connection = sqlite3.connect(self.new_path, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE task_control_schema_metadata SET created_at = ?",
            (_ORIGINAL_TIME,),
        )
        connection.rollback()
        connection.close()
        self.assertEqual(_CHANGED_TIME, self._read_metadata_time_raw())

    def test_exception_and_base_exception_both_roll_back(self) -> None:
        for exception in (RuntimeError("fault"), KeyboardInterrupt()):
            with self.subTest(exception=type(exception).__name__):
                try:
                    with self.transaction_manager.begin() as transaction:
                        transaction.connection.execute(
                            "UPDATE task_control_schema_metadata SET created_at = ?",
                            (_CHANGED_TIME,),
                        )
                        raise exception
                except type(exception):
                    pass
                self.assertEqual(_ORIGINAL_TIME, self._read_metadata_time_raw())

    def test_nested_and_cross_thread_transaction_use_are_rejected(self) -> None:
        with self.transaction_manager.begin() as outer:
            with self.assertRaises(SQLiteTransactionError) as nested:
                with self.transaction_manager.begin():
                    pass
            self.assertEqual("nested_transaction_forbidden", nested.exception.code)

            errors: list[BaseException] = []

            def commit_from_other_thread() -> None:
                try:
                    outer.commit()
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=commit_from_other_thread)
            thread.start()
            thread.join(timeout=2)
            self.assertEqual(1, len(errors))
            self.assertIsInstance(errors[0], SQLiteTransactionError)
            self.assertEqual("transaction_thread_mismatch", errors[0].code)
        self.assertEqual(_ORIGINAL_TIME, self._read_metadata_time_raw())

    def test_busy_is_classified_once_and_context_is_released(self) -> None:
        for begin_mode in ("BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"):
            with self.subTest(begin_mode=begin_mode):
                locker = sqlite3.connect(self.new_path, isolation_level=None)
                locker.execute(begin_mode)
                started = time.monotonic()
                try:
                    with self.assertRaises(SQLiteBusyError) as raised:
                        with self.transaction_manager.begin():
                            pass
                    self.assertEqual("sqlite_busy", raised.exception.code)
                    self.assertLess(time.monotonic() - started, 1.0)
                finally:
                    locker.rollback()
                    locker.close()
        # BEGIN 失败必须释放嵌套门禁，锁释放后可以立即开启下一笔事务。
        with self.transaction_manager.begin() as transaction:
            transaction.rollback()


class SQLiteNarrowUnitOfWorkTests(TaskControlSQLiteInfrastructureTestCase):
    def test_three_factories_match_frozen_factory_ports(self) -> None:
        admission_factory = SQLiteTaskAdmissionUnitOfWorkFactory(
            self.transaction_manager,
            admission_builder=_ConnectionProbeStore,
            callback_conflict_builder=_ConnectionProbeStore,
        )
        execution_factory = self._execution_factory()
        recovery_factory = SQLiteTaskRecoveryUnitOfWorkFactory(
            self.transaction_manager,
            recovery_builder=_ConnectionProbeStore,
        )
        self.assertIsInstance(admission_factory, TaskAdmissionUnitOfWorkFactory)
        self.assertIsInstance(execution_factory, TaskExecutionUnitOfWorkFactory)
        self.assertIsInstance(recovery_factory, TaskRecoveryUnitOfWorkFactory)

    def test_admission_stores_share_exactly_one_connection(self) -> None:
        factory = SQLiteTaskAdmissionUnitOfWorkFactory(
            self.transaction_manager,
            admission_builder=_ConnectionProbeStore,
            callback_conflict_builder=_ConnectionProbeStore,
        )
        with factory() as unit_of_work:
            admission = unit_of_work.admission
            conflicts = unit_of_work.callback_conflicts
            self.assertIs(admission.connection, conflicts.connection)  # type: ignore[attr-defined]
            unit_of_work.rollback()

    def test_uow_requires_explicit_commit_and_hides_store_after_close(self) -> None:
        factory = self._execution_factory()
        unit_of_work = factory()
        with unit_of_work:
            unit_of_work.execution.set_metadata_time(_CHANGED_TIME)  # type: ignore[attr-defined]
        self.assertEqual(_ORIGINAL_TIME, self._read_metadata_time_raw())
        with self.assertRaises(SQLiteTransactionError):
            _ = unit_of_work.execution

        committed = factory()
        with committed:
            committed.execution.set_metadata_time(_CHANGED_TIME)  # type: ignore[attr-defined]
            committed.commit()
        self.assertEqual(_CHANGED_TIME, self._read_metadata_time_raw())

    def test_store_builder_failure_rolls_back_and_releases_nested_guard(self) -> None:
        def broken_builder(_connection: sqlite3.Connection) -> object:
            raise RuntimeError("forced-store-builder-failure")

        broken = SQLiteTaskRecoveryUnitOfWorkFactory(
            self.transaction_manager,
            recovery_builder=broken_builder,
        )
        with self.assertRaisesRegex(RuntimeError, "forced-store-builder-failure"):
            with broken():
                pass
        with self._execution_factory()() as recovered:
            recovered.rollback()

        none_builder = SQLiteTaskExecutionUnitOfWorkFactory(
            self.transaction_manager,
            execution_builder=lambda _connection: None,
            callback_delivery_builder=_ConnectionProbeStore,
        )
        with self.assertRaisesRegex(TypeError, "不得返回 None"):
            with none_builder():
                pass
        with self._execution_factory()() as recovered_again:
            recovered_again.rollback()

    def test_infrastructure_modules_have_no_network_sleep_or_savepoint_capability(self) -> None:
        module_paths = (
            PROJECT_ROOT / "app/modules/tasks/adapters/sqlite/connection.py",
            PROJECT_ROOT / "app/modules/tasks/adapters/sqlite/transaction.py",
            PROJECT_ROOT / "app/modules/tasks/adapters/sqlite/unit_of_work.py",
        )
        forbidden_import_roots = {"requests", "httpx", "urllib", "aiohttp", "socket"}
        for path in module_paths:
            with self.subTest(path=path.name):
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
                imported_roots: set[str] = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported_roots.update(
                            alias.name.split(".", 1)[0] for alias in node.names
                        )
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported_roots.add(node.module.split(".", 1)[0])
                self.assertTrue(forbidden_import_roots.isdisjoint(imported_roots))
                self.assertNotIn("time.sleep", source)
                self.assertNotIn("SAVEPOINT", source.upper())


if __name__ == "__main__":
    unittest.main()
