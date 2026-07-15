"""阶段 0 SQLite 资产盘点器的只读行为测试。"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from scripts.stage0_sqlite_inventory import inspect_database
from tests import workspace_tempdir


class Stage0SqliteInventoryTests(unittest.TestCase):
    def test_inventory_reports_schema_counts_and_statuses_without_writing(self) -> None:
        with workspace_tempdir() as directory:
            database = Path(directory) / "inventory.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    CREATE TABLE tasks (
                        id INTEGER PRIMARY KEY,
                        status TEXT NOT NULL,
                        callback_status TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE INDEX ix_tasks_status ON tasks(status);
                    INSERT INTO tasks(status, callback_status, payload)
                    VALUES ('running', 'pending', 'sensitive-body'),
                           ('done', 'success', 'another-sensitive-body');
                    """
                )
                connection.commit()
            finally:
                connection.close()

            before = database.stat()
            inventory = inspect_database(
                database,
                include_row_counts=True,
                include_hash=True,
                integrity_check=True,
            )
            after = database.stat()

        self.assertTrue(inventory["queryOnly"])
        self.assertTrue(inventory["fileUnchangedDuringInspection"])
        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(["ok"], inventory["integrityCheck"])
        self.assertEqual(64, len(inventory["sha256"]))

        tasks = next(table for table in inventory["tables"] if table["name"] == "tasks")
        self.assertEqual(2, tasks["rowCount"])
        self.assertEqual(
            {"done": 1, "running": 1},
            {
                str(item["value"]): item["count"]
                for item in tasks["statusDistributions"]["status"]
            },
        )
        # 聚合资产只能出现列名，不得把业务正文字段的具体值带入输出。
        self.assertNotIn("sensitive-body", str(inventory))

    def test_missing_database_fails_before_connecting(self) -> None:
        with workspace_tempdir() as directory:
            missing = Path(directory) / "missing.sqlite3"
            with self.assertRaises(FileNotFoundError):
                inspect_database(missing)


if __name__ == "__main__":
    unittest.main()
