"""结构化来源键从类别快照到绑定回执的 SQLite 闭环门禁。"""

from __future__ import annotations

import tempfile
import sqlite3
import unittest

from app.modules.chat.adapters.sqlite.identity_repository import (
    SQLiteConversationIdentityRepository,
)
from app.modules.chat.adapters.sqlite.store import ChatStore
from app.modules.chat.domain.document_candidates import ChatDocumentCandidate
from app.modules.chat.domain.document_scope import (
    CHAT_SCOPE_MODE_ARCHITECTURE,
    CHAT_SCOPE_SOURCE_ARCHITECTURE_INITIAL,
    ChatScopeRevision,
    ChatSessionScopeBinding,
)
from app.modules.chat.domain.identity import WeaponryChatIdentity


class ChatStructuredSourcePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = f"{self.temporary.name}/chat.sqlite3"
        identity = SQLiteConversationIdentityRepository(
            self.db_path,
            owner_instance_id="source-persistence-test",
        ).create_conversation(WeaponryChatIdentity(user_id=9, architecture_id=7))
        self.conversation_id = identity.conversation_id
        self.store = ChatStore(self.db_path)
        self.store.session_scope_bindings.create(
            ChatSessionScopeBinding(
                conversation_id=self.conversation_id,
                scope_mode=CHAT_SCOPE_MODE_ARCHITECTURE,
                architecture_id=7,
                created_at="2026-08-01T00:00:00+00:00",
            )
        )
        self.store.runs.create(
            run_id="run-1",
            conversation_id=self.conversation_id,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scope_and_binding_receipt_preserve_the_exact_source_key(self) -> None:
        source_key = "docsense_ref:" + "a" * 32
        document = ChatDocumentCandidate(
            file_name="alpha.pdf",
            original_name="原始 Alpha.pdf",
            document_ref="document:alpha",
            external_location="custom-documents/alpha.json",
            structured_source_key=source_key,
        )
        revision = ChatScopeRevision(
            scope_revision_id="scope-1",
            conversation_id=self.conversation_id,
            source_mode=CHAT_SCOPE_SOURCE_ARCHITECTURE_INITIAL,
            source_run_id="run-1",
            source_architecture_id=7,
            members=(document,),
            created_at="2026-08-01T00:00:00+00:00",
        )

        self.store.scopes.append_and_set_head(
            revision=revision,
            expected_current_revision_id=None,
        )
        binding = self.store.document_bindings.add(
            conversation_id=self.conversation_id,
            file_name=document.file_name,
            original_name=document.original_name,
            document_ref=document.document_ref,
            external_location=document.external_location,
            structured_source_key=source_key,
            added_by_run_id="run-1",
        )

        restored = self.store.scopes.get_current_revision(self.conversation_id)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(source_key, restored.members[0].structured_source_key)
        self.assertEqual(source_key, binding.structured_source_key)
        self.assertEqual(
            source_key,
            self.store.document_bindings.list_current_by_chat(
                self.conversation_id
            )[0].structured_source_key,
        )

    def test_duplicate_source_key_in_one_scope_is_rejected(self) -> None:
        source_key = "docsense_ref:" + "b" * 32
        members = tuple(
            ChatDocumentCandidate(
                file_name=f"{name}.pdf",
                original_name=f"原始 {name}.pdf",
                document_ref=f"document:{name}",
                external_location=f"custom-documents/{name}.json",
                structured_source_key=source_key,
            )
            for name in ("one", "two")
        )
        revision = ChatScopeRevision(
            scope_revision_id="scope-duplicate",
            conversation_id=self.conversation_id,
            source_mode=CHAT_SCOPE_SOURCE_ARCHITECTURE_INITIAL,
            source_run_id="run-1",
            source_architecture_id=7,
            members=members,
            created_at="2026-08-01T00:00:00+00:00",
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE constraint failed"):
            self.store.scopes.append_and_set_head(
                revision=revision,
                expected_current_revision_id=None,
            )


if __name__ == "__main__":
    unittest.main()
