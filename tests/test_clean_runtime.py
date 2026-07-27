"""发布清库脚本的离线安全回归。

测试只操作隔离临时目录，不调用 AnythingLLM，也不启动 ``run.py``。
"""

from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest import mock

import clean
from tests import workspace_tempdir


class CleanRuntimeTests(unittest.TestCase):
    """验证发布所依赖的数据库清空与失败退出语义。"""

    @staticmethod
    def _environment(root: Path) -> dict[str, str]:
        runtime = root / "runtime"
        return {
            "DOCSENSE_RUNTIME_DIR": str(runtime.resolve()),
            # 任务库特意放在 runtime 外，覆盖曾经逃逸清理范围的兼容配置。
            "DOCSENSE_LLM_TASK_DB": str((root / "task-db.sqlite3").resolve()),
            "DOCSENSE_KNOWLEDGE_BASE_DB": str(
                (runtime / "knowledge_base.sqlite3").resolve()
            ),
            "KNOWLEDGE_BASE_DB_PATH": "",
            "DOCSENSE_CHAT_DB": str(
                (runtime / "chat_sessions.sqlite3").resolve()
            ),
        }

    def test_removes_runtime_and_external_component_task_database(self) -> None:
        with workspace_tempdir() as temporary_directory:
            root = Path(temporary_directory)
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "knowledge_base.sqlite3").write_bytes(b"knowledge")
            task_database = root / "task-db.sqlite3"
            task_database.write_bytes(b"task")
            Path(f"{task_database}-wal").write_bytes(b"wal")

            with mock.patch.dict(
                os.environ,
                self._environment(root),
                clear=False,
            ):
                clean.clean_runtime()

            self.assertEqual([], list(runtime.iterdir()))
            self.assertFalse(task_database.exists())
            self.assertFalse(Path(f"{task_database}-wal").exists())

    def test_runtime_cleanup_failure_raises_instead_of_reporting_success(self) -> None:
        with workspace_tempdir() as temporary_directory:
            root = Path(temporary_directory)
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "locked.sqlite3").write_bytes(b"locked")

            with (
                mock.patch.dict(
                    os.environ,
                    self._environment(root),
                    clear=False,
                ),
                mock.patch.object(
                    Path,
                    "unlink",
                    side_effect=PermissionError("locked"),
                ),
                mock.patch.object(clean.time, "sleep"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "重试多次后仍无法清理运行时目录",
                ):
                    clean.clean_runtime()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
