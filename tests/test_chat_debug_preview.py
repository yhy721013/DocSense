import sqlite3
import unittest
from unittest.mock import patch

from app.services.core.database import ChatDatabaseService, DatabaseService
from tests import workspace_tempdir


class ChatDebugDatabaseQueryTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = workspace_tempdir()
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
        self.chat_db.create_chat("chat-older", ["a.pdf"], "ws-a", "th-a")
        self.chat_db.create_chat("chat-newer", ["b.pdf"], "ws-b", "th-b")
        self.chat_db.update_file_names("chat-older", ["a.pdf", "c.pdf"])

        rows = self.chat_db.list_chats()

        self.assertEqual(rows[0]["chat_id"], "chat-older")
        self.assertEqual(rows[0]["file_names"], ["a.pdf", "c.pdf"])
        self.assertEqual(rows[1]["chat_id"], "chat-newer")


class ChatDebugPreviewTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = workspace_tempdir()
        self.tmp = self._tempdir.__enter__()
        self.kb_service = DatabaseService(db_path=f"{self.tmp}/knowledge.sqlite3")
        self.chat_db = ChatDatabaseService(db_path=f"{self.tmp}/chat.sqlite3")

    def tearDown(self):
        self._tempdir.__exit__(None, None, None)

    def test_load_chat_debug_bootstrap_returns_sessions_and_available_files(self):
        from app.services.utils.chat_debug_preview import load_chat_debug_bootstrap

        self.chat_db.create_chat("conv-001", ["alpha.pdf"], "ws-1", "th-1")
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
        self.assertEqual(result["data"]["sessions"][0]["fileNames"], ["alpha.pdf"])
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
