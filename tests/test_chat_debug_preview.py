"""本地权威文件对话调试初始化数据的离线测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.services.chat import ChatStore
from app.services.core.database import DatabaseService
from app.services.utils.chat_debug_preview import load_chat_debug_bootstrap


class ChatDebugPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.kb_service = DatabaseService(db_path=f"{self.tmp}/knowledge.sqlite3")
        self.chat_store = ChatStore(db_path=f"{self.tmp}/chat.sqlite3")

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def test_bootstrap_reads_new_sessions_and_business_file_names(self) -> None:
        self.chat_store.sessions.create_or_get(
            chat_id="10001",
            workspace_ref="ws-1",
            thread_ref="thread-1",
        )
        self.chat_store.document_bindings.add(
            chat_id="10001",
            file_name="hash-alpha.pdf",
            original_name="测试文件.pdf",
            document_ref="document:alpha",
            external_location="custom-documents/alpha.json",
        )
        self.kb_service.save_document_record(
            "hash-alpha.pdf",
            12,
            "alpha",
            "custom-documents/alpha.json",
            original_name="测试文件.pdf",
        )

        result = load_chat_debug_bootstrap(
            chat_store=self.chat_store,
            kb_service=self.kb_service,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(10001, result["data"]["sessions"][0]["chatId"])
        self.assertEqual(
            ["hash-alpha.pdf"],
            result["data"]["sessions"][0]["fileNames"],
        )
        self.assertEqual(
            "hash-alpha.pdf",
            result["data"]["availableFiles"][0]["fileName"],
        )

    def test_deleted_session_is_hidden_from_debug_list(self) -> None:
        self.chat_store.sessions.create_or_get(chat_id="10002")
        self.chat_store.sessions.set_status(chat_id="10002", status="deleting")
        self.chat_store.sessions.set_status(chat_id="10002", status="deleted")

        result = load_chat_debug_bootstrap(
            chat_store=self.chat_store,
            kb_service=self.kb_service,
        )

        self.assertEqual([], result["data"]["sessions"])

    def test_legacy_string_chat_id_is_not_returned_to_debug_page(self) -> None:
        """不兼容历史字符串会话，调试接口也不得泄露旧类型。"""

        self.chat_store.sessions.create_or_get(chat_id="legacy-chat")

        result = load_chat_debug_bootstrap(
            chat_store=self.chat_store,
            kb_service=self.kb_service,
        )

        self.assertTrue(result["ok"])
        self.assertEqual([], result["data"]["sessions"])

    def test_query_failure_returns_stable_empty_error_payload(self) -> None:
        with patch.object(
            self.chat_store.sessions,
            "list_all",
            side_effect=sqlite3.Error("boom"),
        ):
            result = load_chat_debug_bootstrap(
                chat_store=self.chat_store,
                kb_service=self.kb_service,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            {"sessions": [], "availableFiles": []},
            result["data"],
        )
        self.assertIn("读取失败", result["message"])


if __name__ == "__main__":
    unittest.main()
