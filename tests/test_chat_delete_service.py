"""Unit tests for the stage-9 file-chat delete state machine."""

from __future__ import annotations

import tempfile
import unittest

from app.services.chat import (
    ChatDeleteCleanupError,
    ChatDeleteNotFoundError,
    ChatDeleteService,
    ChatHistoryService,
    ChatStore,
    LEASE_CLEANUP_FAILED,
    LEASE_CLOSED,
    MESSAGE_COMMITTED,
    MESSAGE_ROLE_USER,
    RESOURCE_DOCUMENT_BINDING,
    RESOURCE_THREAD,
    RESOURCE_WORKSPACE,
    chat_document_binding_lease_id,
    chat_thread_lease_id,
    chat_workspace_lease_id,
)
from app.services.core.database import ChatDatabaseService
from tests.fakes import FakeChatConversationFactory


class ChatDeleteServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.db_path = f"{self.tmp}/chat.sqlite3"
        self.store = ChatStore(self.db_path)
        self.chat_db = ChatDatabaseService(self.db_path)
        self.factory = FakeChatConversationFactory()
        self.service = ChatDeleteService(
            store=self.store,
            chat_db=self.chat_db,
            conversation_factory=self.factory,
        )

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def _create_remote_chat(self, chat_id: str = "chat-delete"):
        with self.factory.create() as port:
            refs = port.open_conversation(
                context_name=f"context-{chat_id}",
                conversation_name=f"thread-{chat_id}",
            )
        self.chat_db.create_chat(
            chat_id,
            ["delete.pdf"],
            refs.context_ref,
            refs.conversation_ref,
        )
        self.store.sessions.create_or_get(
            chat_id=chat_id,
            workspace_ref=refs.context_ref,
            thread_ref=refs.conversation_ref,
        )
        self.store.resource_leases.ensure_active(
            lease_id=chat_workspace_lease_id(chat_id),
            chat_id=chat_id,
            resource_type=RESOURCE_WORKSPACE,
            external_ref=refs.context_ref,
        )
        self.store.resource_leases.ensure_active(
            lease_id=chat_thread_lease_id(chat_id),
            chat_id=chat_id,
            resource_type=RESOURCE_THREAD,
            external_ref=f"{refs.context_ref}::{refs.conversation_ref}",
        )
        self.store.resource_leases.ensure_active(
            lease_id=chat_document_binding_lease_id(
                chat_id=chat_id,
                file_name="delete.pdf",
            ),
            chat_id=chat_id,
            resource_type=RESOURCE_DOCUMENT_BINDING,
            external_ref=f"{refs.context_ref}::custom-documents/delete.pdf.json",
        )
        return refs

    def test_delete_success_closes_leases_and_keeps_audit_session(self) -> None:
        self._create_remote_chat()

        result = self.service.delete_chat(chat_id="chat-delete")
        second_port_count = len(self.factory.ports)
        repeated = self.service.delete_chat(chat_id="chat-delete")

        session = self.store.sessions.get("chat-delete")
        leases = self.store.resource_leases.list_by_chat("chat-delete")

        self.assertTrue(result.deleted)
        self.assertTrue(repeated.deleted)
        self.assertEqual(second_port_count, len(self.factory.ports))
        self.assertIsNone(self.chat_db.get_chat("chat-delete"))
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual("deleted", session.status)
        self.assertEqual({LEASE_CLOSED}, {lease.status for lease in leases})

    def test_thread_delete_failure_is_compensated_by_context_delete(self) -> None:
        self.factory = FakeChatConversationFactory(
            delete_conversation_error_message="thread delete failed",
        )
        self.service = ChatDeleteService(
            store=self.store,
            chat_db=self.chat_db,
            conversation_factory=self.factory,
        )
        self._create_remote_chat("chat-thread-fail")

        result = self.service.delete_chat(chat_id="chat-thread-fail")
        leases = self.store.resource_leases.list_by_chat("chat-thread-fail")

        self.assertTrue(result.deleted)
        self.assertEqual({LEASE_CLOSED}, {lease.status for lease in leases})
        self.assertIsNone(self.chat_db.get_chat("chat-thread-fail"))

    def test_context_delete_failure_keeps_cleanup_failed_leases(self) -> None:
        self.factory = FakeChatConversationFactory(
            delete_context_error_message="workspace delete failed",
        )
        self.service = ChatDeleteService(
            store=self.store,
            chat_db=self.chat_db,
            conversation_factory=self.factory,
        )
        self._create_remote_chat("chat-workspace-fail")

        with self.assertRaises(ChatDeleteCleanupError):
            self.service.delete_chat(chat_id="chat-workspace-fail")

        session = self.store.sessions.get("chat-workspace-fail")
        leases = {
            lease.resource_type: lease
            for lease in self.store.resource_leases.list_by_chat(
                "chat-workspace-fail"
            )
        }

        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual("error", session.status)
        self.assertIsNotNone(self.chat_db.get_chat("chat-workspace-fail"))
        self.assertEqual(LEASE_CLOSED, leases[RESOURCE_THREAD].status)
        self.assertEqual(LEASE_CLEANUP_FAILED, leases[RESOURCE_WORKSPACE].status)
        self.assertEqual(
            LEASE_CLEANUP_FAILED,
            leases[RESOURCE_DOCUMENT_BINDING].status,
        )
        self.assertEqual(
            "workspace delete failed",
            leases[RESOURCE_WORKSPACE].error_message,
        )

    def test_deleted_session_history_returns_empty_list(self) -> None:
        refs = self._create_remote_chat("chat-history-delete")
        self.store.runs.create(run_id="run-history-delete", chat_id="chat-history-delete")
        self.store.messages.append(
            message_id="message-history-delete",
            chat_id="chat-history-delete",
            run_id="run-history-delete",
            role=MESSAGE_ROLE_USER,
            content="delete me",
            status=MESSAGE_COMMITTED,
        )

        before = ChatHistoryService(self.store).list_history("chat-history-delete")
        self.service.delete_chat(chat_id="chat-history-delete")
        after = ChatHistoryService(self.store).list_history("chat-history-delete")

        self.assertEqual(1, len(before))
        self.assertEqual([], after)
        self.assertEqual(refs.context_ref, self.store.sessions.get("chat-history-delete").workspace_ref)

    def test_missing_chat_raises_not_found(self) -> None:
        with self.assertRaises(ChatDeleteNotFoundError):
            self.service.delete_chat(chat_id="missing-chat")


if __name__ == "__main__":
    unittest.main()
