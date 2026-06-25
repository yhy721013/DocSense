from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


class InspectLLMTasksScriptTests(unittest.TestCase):
    def test_exports_sqlite_content_to_timestamped_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / ".runtime" / "llm_tasks.sqlite3"
            output_dir = tmp_path / ".runtime" / "sqlite"
            db_path.parent.mkdir(parents=True)

            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE llm_tasks (
                        business_type TEXT NOT NULL,
                        business_key TEXT NOT NULL,
                        request_payload TEXT NOT NULL,
                        status TEXT NOT NULL,
                        progress REAL NOT NULL DEFAULT 0,
                        message TEXT NOT NULL DEFAULT '',
                        result_payload TEXT,
                        callback_status TEXT NOT NULL DEFAULT 'pending',
                        callback_attempts INTEGER NOT NULL DEFAULT 0,
                        last_callback_error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (business_type, business_key)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO llm_tasks (
                        business_type, business_key, request_payload, status, progress,
                        message, result_payload, callback_status, callback_attempts,
                        last_callback_error, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "file",
                        "sample.txt",
                        json.dumps({"businessType": "file", "params": [{"fileName": "sample.txt"}]}, ensure_ascii=False),
                        "2",
                        1.0,
                        "解析成功",
                        json.dumps({"msg": "解析成功", "data": {"status": "2"}}, ensure_ascii=False),
                        "success",
                        1,
                        "",
                        "2026-06-25T00:00:00+00:00",
                        "2026-06-25T00:01:00+00:00",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT_DIR / "scripts/inspect_llm_tasks.py"),
                    "--db-path",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                cwd=ROOT_DIR,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            exported_files = list(output_dir.glob("llm_tasks_*.json"))
            self.assertEqual(len(exported_files), 1)
            self.assertRegex(exported_files[0].name, r"^llm_tasks_\d{8}_\d{6}_\d{6}\.json$")

            exported = json.loads(exported_files[0].read_text(encoding="utf-8"))
            self.assertEqual(exported["metadata"]["databasePath"], str(db_path.resolve()))
            self.assertEqual(exported["metadata"]["objectCount"], 1)
            self.assertEqual(exported["metadata"]["totalRows"], 1)

            table = exported["tables"][0]
            self.assertEqual(table["name"], "llm_tasks")
            self.assertEqual(table["rowCount"], 1)
            self.assertEqual(table["columns"][0]["name"], "business_type")

            row = table["rows"][0]
            self.assertEqual(row["business_type"], "file")
            self.assertEqual(row["business_key"], "sample.txt")
            self.assertEqual(row["request_payload"]["businessType"], "file")
            self.assertEqual(row["request_payload"]["params"][0]["fileName"], "sample.txt")
            self.assertEqual(row["result_payload"]["data"]["status"], "2")
            self.assertEqual(row["callback_status"], "success")


if __name__ == "__main__":
    unittest.main()
