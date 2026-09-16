"""XLSX Folder 只读库存脚本的离线治理测试。"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.integrations.anythingllm.documents import XlsxFolderInventoryItem
from scripts.inspect_xlsx_folder_inventory import (
    build_inventory_report,
    build_parser,
    load_committed_xlsx_locations,
)


class XlsxFolderInventoryScriptTests(unittest.TestCase):
    """证明库存只输出脱敏统计，并保护全部永久知识 Folder。"""

    def test_report_protects_committed_and_never_emits_remote_names(self) -> None:
        exact_folder = "prepared-exact.xlsx-6f2a"
        drifted_folder = "prepared-drifted.xlsx-7e3b"
        unreferenced_folder = "prepared-unreferenced.xlsx-8a4c"
        missing_folder = "prepared-missing.xlsx-9b5d"
        inventory = (
            XlsxFolderInventoryItem(
                exact_folder,
                (f"{exact_folder}/sheet-summary.json",),
            ),
            XlsxFolderInventoryItem(
                drifted_folder,
                (
                    f"{drifted_folder}/sheet-details.json",
                    f"{drifted_folder}/sheet-summary.json",
                ),
            ),
            XlsxFolderInventoryItem(
                unreferenced_folder,
                (f"{unreferenced_folder}/sheet-summary.json",),
            ),
        )
        committed = (
            f"{exact_folder}/sheet-summary.json",
            f"{drifted_folder}/sheet-summary.json",
            f"{missing_folder}/sheet-summary.json",
        )

        report = build_inventory_report(inventory, committed)

        self.assertFalse(report["remoteMutation"])
        self.assertEqual(3, report["folderCount"])
        self.assertEqual(4, report["memberCount"])
        self.assertEqual(1, report["committedProtectedFolderCount"])
        self.assertEqual(1, report["committedDriftedFolderCount"])
        self.assertEqual(1, report["unreferencedFolderCount"])
        self.assertEqual(1, report["missingRemoteCommittedFolderCount"])
        self.assertTrue(report["attentionRequired"])
        rendered = json.dumps(report, ensure_ascii=False)
        for sensitive_name in (
            exact_folder,
            drifted_folder,
            unreferenced_folder,
            missing_folder,
            "sheet-summary.json",
            "sheet-details.json",
        ):
            self.assertNotIn(sensitive_name, rendered)
        protected = [
            item
            for item in report["folders"]
            if item["state"].startswith("committed")
        ]
        self.assertTrue(protected)
        self.assertTrue(
            all(
                item["recommendation"]
                == "report_only_no_automatic_delete"
                for item in protected
            )
        )

    def test_sqlite_catalog_is_read_only_and_filters_non_xlsx_paths(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            database_path = Path(tmp) / "knowledge.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE documents (doc_path TEXT)")
            connection.executemany(
                "INSERT INTO documents (doc_path) VALUES (?)",
                (
                    ("custom-documents/example.pdf-id.json",),
                    ("prepared-a.xlsx-6f2a/sheet-summary.json",),
                    ("prepared-a.xlsx-6f2a/sheet-summary.json",),
                ),
            )
            connection.commit()
            connection.close()

            locations = load_committed_xlsx_locations(database_path)

            self.assertEqual(
                ("prepared-a.xlsx-6f2a/sheet-summary.json",),
                locations,
            )
            check = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    3,
                    check.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                )
            finally:
                check.close()

    def test_cli_exposes_no_apply_cleanup_or_delete_option(self) -> None:
        option_strings = {
            option
            for action in build_parser()._actions
            for option in action.option_strings
        }
        self.assertFalse(
            option_strings.intersection(
                {"--apply", "--cleanup", "--delete", "--token"}
            )
        )


if __name__ == "__main__":
    unittest.main()
