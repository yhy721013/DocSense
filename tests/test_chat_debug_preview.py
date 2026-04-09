import unittest

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
