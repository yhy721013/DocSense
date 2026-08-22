"""阶段 2 Task Control 显式 fresh 初始化的失败关闭验收。"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.modules.analysis.adapters.sqlite import (
    ANALYSIS_CONTROL_COMPONENT_NAME,
    ANALYSIS_CONTROL_COMPONENT_VERSION,
    load_analysis_control_manifest,
)
from app.modules.report.adapters.sqlite import (
    REPORT_CONTROL_COMPONENT_NAME,
    REPORT_CONTROL_COMPONENT_VERSION,
    load_report_control_manifest,
)
from app.modules.tasks.adapters.process_guard import FileProcessSingletonGuard
from app.modules.tasks.adapters.sqlite import (
    TaskControlBootstrapError,
    bootstrap_fresh_task_control_database,
    require_explicit_fresh_bootstrap_when_uninitialized,
)
from app.modules.weaponry.adapters.sqlite import (
    WEAPONRY_CONTROL_COMPONENT_NAME,
    WEAPONRY_CONTROL_COMPONENT_VERSION,
    load_weaponry_control_manifest,
)


def _components() -> tuple[dict[str, dict[str, object]], dict[str, int]]:
    """返回当前发布必须一次就绪的三业务 Manifest，不从生产 Container 反向导入。"""

    known = {
        REPORT_CONTROL_COMPONENT_NAME: load_report_control_manifest(),
        WEAPONRY_CONTROL_COMPONENT_NAME: load_weaponry_control_manifest(),
        ANALYSIS_CONTROL_COMPONENT_NAME: load_analysis_control_manifest(),
    }
    required = {
        REPORT_CONTROL_COMPONENT_NAME: REPORT_CONTROL_COMPONENT_VERSION,
        WEAPONRY_CONTROL_COMPONENT_NAME: WEAPONRY_CONTROL_COMPONENT_VERSION,
        ANALYSIS_CONTROL_COMPONENT_NAME: ANALYSIS_CONTROL_COMPONENT_VERSION,
    }
    return known, required


class Stage2FreshTaskControlBootstrapTests(unittest.TestCase):
    """证明 fresh 不猜测、不覆盖、不代删，并完整发布当前组件。"""

    def _bootstrap(self, old_path: Path, new_path: Path):
        known, required = _components()
        return bootstrap_fresh_task_control_database(
            old_path,
            new_path,
            fresh_install_confirmed=True,
            known_components=known,
            required_components=required,
        )

    def test_fresh_publishes_all_required_components_without_creating_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "legacy.sqlite3"
            new_path = root / "db" / "task-control-v2.sqlite3"

            result = self._bootstrap(old_path, new_path)

            self.assertTrue(result.created)
            self.assertFalse(old_path.exists())
            self.assertEqual(new_path.resolve(), result.database_path)
            self.assertEqual(
                (
                    ANALYSIS_CONTROL_COMPONENT_NAME,
                    REPORT_CONTROL_COMPONENT_NAME,
                    WEAPONRY_CONTROL_COMPONENT_NAME,
                ),
                result.identity.registered_components,
            )
            connection = sqlite3.connect(new_path)
            try:
                registered = {
                    row[0]
                    for row in connection.execute(
                        "SELECT component_name FROM task_control_schema_components"
                    ).fetchall()
                }
            finally:
                connection.close()
            self.assertEqual(set(result.identity.registered_components), registered)
            self.assertFalse(Path(f"{new_path}-wal").exists())
            self.assertFalse(Path(f"{new_path}-shm").exists())
            self.assertFalse(Path(f"{new_path}-journal").exists())

    def test_confirmation_and_complete_component_set_are_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "legacy.sqlite3"
            new_path = root / "task-control-v2.sqlite3"
            known, required = _components()

            with self.assertRaises(TaskControlBootstrapError) as raised:
                bootstrap_fresh_task_control_database(
                    old_path,
                    new_path,
                    fresh_install_confirmed=False,
                    known_components=known,
                    required_components=required,
                )
            self.assertEqual(
                "fresh_install_confirmation_required",
                raised.exception.code,
            )
            with self.assertRaises(TaskControlBootstrapError) as raised:
                bootstrap_fresh_task_control_database(
                    old_path,
                    new_path,
                    fresh_install_confirmed=True,
                    known_components=known,
                    required_components={},
                )
            self.assertEqual("fresh_required_components_empty", raised.exception.code)
            self.assertFalse(new_path.exists())

    def test_ordinary_application_gate_requires_preprovisioned_fresh_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "legacy.sqlite3"
            new_path = root / "task-control-v2.sqlite3"

            with self.assertRaises(TaskControlBootstrapError) as raised:
                require_explicit_fresh_bootstrap_when_uninitialized(
                    old_path,
                    new_path,
                )
            self.assertEqual("fresh_bootstrap_required", raised.exception.code)
            self.assertFalse(old_path.exists())
            self.assertFalse(new_path.exists())

            self._bootstrap(old_path, new_path)
            # v2 已由一次性命令发布后，普通应用才可以进入严格打开分支。
            require_explicit_fresh_bootstrap_when_uninitialized(old_path, new_path)

    def test_any_legacy_or_target_file_member_blocks_without_deletion(self) -> None:
        suffixes = ("", "-wal", "-shm", "-journal")
        for owner in ("legacy", "target"):
            for suffix in suffixes:
                with self.subTest(owner=owner, suffix=suffix or "main"):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        old_path = root / "legacy.sqlite3"
                        new_path = root / "task-control-v2.sqlite3"
                        selected = old_path if owner == "legacy" else new_path
                        residual = Path(f"{selected}{suffix}")
                        residual.write_bytes(b"unknown-owner")

                        with self.assertRaises(TaskControlBootstrapError) as raised:
                            self._bootstrap(old_path, new_path)

                        expected = (
                            "fresh_legacy_file_set_present"
                            if owner == "legacy"
                            else "fresh_target_file_set_present"
                        )
                        self.assertEqual(expected, raised.exception.code)
                        self.assertEqual(b"unknown-owner", residual.read_bytes())

    def test_repeat_invocation_and_schema_lock_contention_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "legacy.sqlite3"
            new_path = root / "task-control-v2.sqlite3"
            first = self._bootstrap(old_path, new_path)
            with self.assertRaises(TaskControlBootstrapError) as raised:
                self._bootstrap(old_path, new_path)
            self.assertEqual("fresh_target_file_set_present", raised.exception.code)
            self.assertEqual(first.database_path, new_path.resolve())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "legacy.sqlite3"
            new_path = root / "task-control-v2.sqlite3"
            guard = FileProcessSingletonGuard(
                new_path.with_name(f"{new_path.name}.schema.lock"),
                component_name="fresh 测试占用者",
            )
            self.assertTrue(guard.acquire())
            try:
                with self.assertRaises(TaskControlBootstrapError) as raised:
                    self._bootstrap(old_path, new_path)
                self.assertEqual("bootstrap_schema_lock_busy", raised.exception.code)
                self.assertFalse(new_path.exists())
            finally:
                guard.release()


if __name__ == "__main__":
    unittest.main()
