"""阶段 9 文件对话删除状态机的单元测试。"""

from __future__ import annotations

import tempfile
import unittest
import zlib

from app.modules.chat import (
    ChatDeleteCleanupError,
    ChatDeleteBusyError,
    ChatDeleteNotFoundError,
    ChatDeleteService,
    ChatCommandService,
    ChatRunLockService,
    ChatHistoryService,
    ChatSessionScopeBinding,
    ChatStore,
    LEASE_CLEANUP_FAILED,
    LEASE_CLOSED,
    MESSAGE_COMMITTED,
    MESSAGE_ROLE_USER,
    RESOURCE_DOCUMENT_BINDING,
    RESOURCE_THREAD,
    RESOURCE_WORKSPACE,
    chat_document_binding_lease_id,
    chat_scoped_external_ref,
    chat_thread_lease_id,
    chat_workspace_lease_id,
)
from tests.fakes import FakeChatConversationFactory
from app.modules.chat.domain.identity import FileChatIdentity, WeaponryChatIdentity


def _identity(value: str) -> FileChatIdentity:
    """把测试标签稳定映射为文件对话公开身份。"""
    return FileChatIdentity(chat_id=zlib.crc32(value.encode("utf-8")) + 1)


class ChatDeleteServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.db_path = f"{self.tmp}/chat.sqlite3"
        self.store = ChatStore(self.db_path)
        self.commands = ChatCommandService(ChatRunLockService(self.db_path))
        self.factory = FakeChatConversationFactory()
        self.service = ChatDeleteService(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=self.factory,
        )

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def _create_remote_chat(
        self,
        label: str = "chat-delete",
        *,
        identity: FileChatIdentity | WeaponryChatIdentity | None = None,
    ):
        identity = identity or _identity(label)
        conversation_id = self.store.identities.create_conversation(
            identity
        ).conversation_id
        with self.factory.create() as port:
            refs = port.open_conversation(
                context_name=f"context-{label}",
                conversation_name=f"thread-{label}",
            )
        self.store.sessions.create_or_get(
            conversation_id=conversation_id,
            workspace_ref=refs.context_ref,
            thread_ref=refs.conversation_ref,
        )
        self.store.session_scope_bindings.create(
            ChatSessionScopeBinding(
                conversation_id=conversation_id,
                scope_mode=(
                    "architecture"
                    if isinstance(identity, WeaponryChatIdentity)
                    else "files"
                ),
                architecture_id=(
                    identity.architecture_id
                    if isinstance(identity, WeaponryChatIdentity)
                    else None
                ),
                created_at="2026-07-28T00:00:00+00:00",
            )
        )
        self.store.resource_leases.ensure_active(
            lease_id=chat_workspace_lease_id(conversation_id),
            conversation_id=conversation_id,
            resource_type=RESOURCE_WORKSPACE,
            external_ref=refs.context_ref,
        )
        self.store.resource_leases.ensure_active(
            lease_id=chat_thread_lease_id(conversation_id),
            conversation_id=conversation_id,
            resource_type=RESOURCE_THREAD,
            external_ref=chat_scoped_external_ref(
                context_ref=refs.context_ref,
                resource_ref=refs.conversation_ref,
            ),
        )
        self.store.resource_leases.ensure_active(
            lease_id=chat_document_binding_lease_id(
                conversation_id=conversation_id,
                file_name="delete.pdf",
            ),
            conversation_id=conversation_id,
            resource_type=RESOURCE_DOCUMENT_BINDING,
            external_ref=f"{refs.context_ref}::custom-documents/delete.pdf.json",
        )
        return refs, identity, conversation_id

    def test_delete_success_closes_leases_and_keeps_audit_session(self) -> None:
        _, identity, conversation_id = self._create_remote_chat()

        result = self.service.delete_chat(identity=identity)
        second_port_count = len(self.factory.ports)
        repeated = self.service.delete_chat(identity=identity)

        session = self.store.sessions.get(conversation_id)
        leases = self.store.resource_leases.list_by_chat(conversation_id)

        self.assertTrue(result.deleted)
        self.assertTrue(repeated.deleted)
        self.assertEqual(second_port_count, len(self.factory.ports))
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
            chat_commands=self.commands,
            conversation_factory=self.factory,
        )
        _, identity, conversation_id = self._create_remote_chat("chat-thread-fail")

        result = self.service.delete_chat(identity=identity)
        leases = self.store.resource_leases.list_by_chat(conversation_id)

        self.assertTrue(result.deleted)
        self.assertEqual({LEASE_CLOSED}, {lease.status for lease in leases})

    def test_context_delete_failure_keeps_cleanup_failed_leases(self) -> None:
        self.factory = FakeChatConversationFactory(
            delete_context_error_message="workspace delete failed",
        )
        self.service = ChatDeleteService(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=self.factory,
        )
        _, identity, conversation_id = self._create_remote_chat("chat-workspace-fail")

        with self.assertRaises(ChatDeleteCleanupError):
            self.service.delete_chat(identity=identity)

        session = self.store.sessions.get(conversation_id)
        leases = {
            lease.resource_type: lease
            for lease in self.store.resource_leases.list_by_chat(
                conversation_id
            )
        }

        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual("error", session.status)
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

    def test_weaponry_cleanup_failure_keeps_identity_bound_to_old_generation(
        self,
    ) -> None:
        """远端清理未完成时不得释放复合身份并创建第二个同名 Workspace。"""

        self.factory = FakeChatConversationFactory(
            delete_context_error_message="workspace delete failed",
        )
        self.service = ChatDeleteService(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=self.factory,
        )
        identity = WeaponryChatIdentity(user_id=70001, architecture_id=90001)
        _, _, conversation_id = self._create_remote_chat(
            "weaponry-workspace-fail",
            identity=identity,
        )

        with self.assertRaises(ChatDeleteCleanupError):
            self.service.delete_chat(identity=identity)

        resolution = self.store.identities.resolve_active(identity)
        self.assertIsNotNone(resolution)
        assert resolution is not None
        self.assertEqual(conversation_id, resolution.conversation_id)
        self.assertEqual("error", resolution.session.status)

    def test_deleted_session_history_returns_empty_list(self) -> None:
        refs, identity, conversation_id = self._create_remote_chat("chat-history-delete")
        self.store.runs.create(
            run_id="run-history-delete",
            conversation_id=conversation_id,
        )
        self.store.runs.mark_running("run-history-delete")
        self.store.runs.mark_succeeded("run-history-delete")
        self.store.messages.append(
            message_id="message-history-delete",
            conversation_id=conversation_id,
            run_id="run-history-delete",
            role=MESSAGE_ROLE_USER,
            content="delete me",
            status=MESSAGE_COMMITTED,
        )

        before = ChatHistoryService(self.store).list_history(identity)
        self.service.delete_chat(identity=identity)
        after = ChatHistoryService(self.store).list_history(identity)

        self.assertEqual(1, len(before))
        self.assertEqual([], after)
        # 远端引用属于不含正文的最小清理审计事实；消息正文已由历史空数组证明清除。
        self.assertEqual(
            refs.context_ref,
            self.store.sessions.get(conversation_id).workspace_ref,
        )

    def test_missing_chat_raises_not_found(self) -> None:
        with self.assertRaises(ChatDeleteNotFoundError):
            self.service.delete_chat(identity=_identity("missing-chat"))

    def test_delete_rejects_active_run_without_changing_session_state(self) -> None:
        _, identity, conversation_id = self._create_remote_chat("chat-delete-active")
        self.commands.start_chat_run(
            identity=identity,
            user_message="still running",
        )

        with self.assertRaises(ChatDeleteBusyError):
            self.service.delete_chat(identity=identity)

        session = self.store.sessions.get(conversation_id)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual("active", session.status)

    def test_delete_preserves_unresolved_planned_leases_for_recovery(self) -> None:
        identity = _identity("chat-unresolved-resource")
        conversation_id = self.store.identities.create_conversation(
            identity
        ).conversation_id
        self.store.resource_leases.begin(
            lease_id=chat_workspace_lease_id(conversation_id),
            conversation_id=conversation_id,
            resource_type=RESOURCE_WORKSPACE,
        )

        with self.assertRaises(ChatDeleteCleanupError):
            self.service.delete_chat(identity=identity)

        session = self.store.sessions.get(conversation_id)
        lease = self.store.resource_leases.get(
            chat_workspace_lease_id(conversation_id)
        )
        self.assertIsNotNone(session)
        self.assertIsNotNone(lease)
        assert session is not None and lease is not None
        self.assertEqual("error", session.status)
        self.assertEqual(LEASE_CLEANUP_FAILED, lease.status)


if __name__ == "__main__":
    unittest.main()
