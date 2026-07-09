import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.services.core.database import ChatDatabaseService, DatabaseService


class ChatDebugDatabaseQueryTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.kb_service = DatabaseService(db_path=f"{self.tmp}/knowledge.sqlite3")
        self.chat_db = ChatDatabaseService(db_path=f"{self.tmp}/chat.sqlite3")

    def tearDown(self):
        self._tempdir.__exit__(None, None, None)

    def test_list_document_records_returns_rows_sorted_by_file_name(self):
        self.kb_service.save_document_record(
            "zulu.pdf",
            9,
            "doc-zulu",
            "custom-documents/doc-zulu.json",
        )
        self.kb_service.save_document_record(
            "alpha.pdf",
            3,
            "doc-alpha",
            "custom-documents/doc-alpha.json",
        )

        rows = self.kb_service.list_document_records()

        self.assertEqual(
            [row["file_name"] for row in rows],
            ["alpha.pdf", "zulu.pdf"],
        )
        self.assertEqual(rows[0]["architecture_id"], 3)
        self.assertEqual(rows[0]["anything_doc_id"], "doc-alpha")

    def test_list_chats_returns_latest_updated_first_with_decoded_file_names(self):
        self.chat_db.create_chat("chat-older", ["测试A.pdf"], "ws-a", "th-a")
        self.chat_db.create_chat("chat-newer", ["测试B.pdf"], "ws-b", "th-b")
        self.chat_db.append_file_original_names("chat-older", ["测试C.pdf"])

        rows = self.chat_db.list_chats()

        self.assertEqual(rows[0]["chat_id"], "chat-older")
        self.assertEqual(rows[0]["file_original_names"], [["测试A.pdf"], ["测试C.pdf"]])
        self.assertEqual(rows[1]["chat_id"], "chat-newer")

    def test_create_and_append_chat_persist_turn_timestamps(self):
        self.chat_db.create_chat("chat-ts", ["测试A.pdf"], "ws-a", "th-a")
        created = self.chat_db.get_chat("chat-ts")
        self.assertEqual(len(created["turn_timestamps"]), 1)
        self.assertIsInstance(created["turn_timestamps"][0], int)

        self.chat_db.append_file_original_names("chat-ts", ["测试B.pdf"])
        updated = self.chat_db.get_chat("chat-ts")
        self.assertEqual(len(updated["turn_timestamps"]), 2)
        self.assertGreaterEqual(updated["turn_timestamps"][1], updated["turn_timestamps"][0])


class ChatDebugPreviewTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.kb_service = DatabaseService(db_path=f"{self.tmp}/knowledge.sqlite3")
        self.chat_db = ChatDatabaseService(db_path=f"{self.tmp}/chat.sqlite3")

    def tearDown(self):
        self._tempdir.__exit__(None, None, None)

    def test_load_chat_debug_bootstrap_returns_sessions_and_available_files(self):
        from app.services.utils.chat_debug_preview import load_chat_debug_bootstrap

        self.chat_db.create_chat("conv-001", ["测试文件.pdf"], "ws-1", "th-1")
        self.kb_service.save_document_record(
            "alpha.pdf",
            12,
            "doc-alpha",
            "custom-documents/doc-alpha.json",
        )

        result = load_chat_debug_bootstrap(chat_db=self.chat_db, kb_service=self.kb_service)

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "读取成功")
        self.assertEqual(result["data"]["sessions"][0]["chatId"], "conv-001")
        self.assertEqual(result["data"]["sessions"][0]["fileNames"], [["测试文件.pdf"]])
        self.assertEqual(result["data"]["availableFiles"][0]["fileName"], "alpha.pdf")
        self.assertEqual(result["data"]["availableFiles"][0]["architectureId"], 12)

    def test_load_chat_debug_bootstrap_returns_empty_lists_for_empty_databases(self):
        from app.services.utils.chat_debug_preview import load_chat_debug_bootstrap

        result = load_chat_debug_bootstrap(chat_db=self.chat_db, kb_service=self.kb_service)

        self.assertEqual(
            result,
            {
                "ok": True,
                "message": "读取成功",
                "data": {"sessions": [], "availableFiles": []},
            },
        )

    def test_load_chat_debug_bootstrap_returns_error_state_when_query_fails(self):
        from app.services.utils.chat_debug_preview import load_chat_debug_bootstrap

        with patch.object(self.chat_db, "list_chats", side_effect=sqlite3.Error("boom")):
            result = load_chat_debug_bootstrap(chat_db=self.chat_db, kb_service=self.kb_service)

        self.assertFalse(result["ok"])
        self.assertEqual(result["data"], {"sessions": [], "availableFiles": []})
        self.assertIn("读取失败", result["message"])

    def test_load_chat_debug_bootstrap_migrates_legacy_chats_schema(self):
        """Stage 3 migrates old chats tables that lack turn_timestamps."""
        from app.services.utils.chat_debug_preview import load_chat_debug_bootstrap

        legacy_path = f"{self.tmp}/legacy-chat.sqlite3"
        with sqlite3.connect(legacy_path) as conn:
            conn.execute(
                """
                CREATE TABLE chats (
                    chat_id TEXT PRIMARY KEY,
                    file_original_names TEXT NOT NULL,
                    workspace_slug TEXT NOT NULL,
                    thread_slug TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO chats (
                    chat_id, file_original_names, workspace_slug,
                    thread_slug, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-chat",
                    json.dumps([["旧文件.pdf"]], ensure_ascii=False),
                    "legacy-ws",
                    "legacy-thread",
                    "2026-07-08T00:00:00+00:00",
                    "2026-07-08T00:00:00+00:00",
                ),
            )

        legacy_chat_db = ChatDatabaseService(db_path=legacy_path)
        result = load_chat_debug_bootstrap(
            chat_db=legacy_chat_db,
            kb_service=self.kb_service,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["sessions"][0]["chatId"], "legacy-chat")
        self.assertEqual(1, len(result["data"]["sessions"][0]["fileNames"]))
