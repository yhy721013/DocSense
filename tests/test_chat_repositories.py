"""Offline tests for stage-3 file-chat repositories."""

from __future__ import annotations

import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.chat import (
    LEASE_CLEANUP_FAILED,
    LEASE_CLEANUP_PENDING,
    LEASE_CLOSED,
    MESSAGE_COMMITTED,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    RUN_ACCEPTED,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    SESSION_ACTIVE,
    ChatStore,
    ensure_chat_schema,
)
from app.services.core.database import ChatDatabaseService


class ChatRepositorySchemaTests(unittest.TestCase):
    def test_schema_initialization_is_repeatable_and_keeps_legacy_chats(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = f"{tmp}/chat.sqlite3"
            legacy = ChatDatabaseService(db_path)
            legacy.create_chat(
                "chat-legacy",
                ["原始.pdf"],
                "workspace-slug",
                "thread-slug",
            )

            ensure_chat_schema(db_path)
            ChatStore(db_path)
            ChatStore(db_path)

            with sqlite3.connect(db_path) as connection:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'table'
                        """
                    ).fetchall()
                }

            self.assertIn("chats", table_names)
            self.assertIn("chat_sessions", table_names)
            self.assertIn("chat_documents", table_names)
            self.assertIn("chat_runs", table_names)
            self.assertIn("chat_messages", table_names)
            self.assertIn("chat_message_files", table_names)
            self.assertIn("chat_resource_leases", table_names)
            self.assertEqual("workspace-slug", legacy.get_chat("chat-legacy")["workspace_slug"])
            self.assertEqual(["原始.pdf"], legacy.get_chat("chat-legacy")["file_original_names"][0])
            self.assertEqual(["chat-legacy"], [item["chat_id"] for item in legacy.list_chats()])

    def test_chat_database_service_migrates_legacy_chats_schema(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = f"{tmp}/legacy-chat.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute(
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
                connection.execute(
                    """
                    INSERT INTO chats (
                        chat_id, file_original_names, workspace_slug,
                        thread_slug, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-chat",
                        '[["legacy.pdf"]]',
                        "legacy-ws",
                        "legacy-thread",
                        "2026-07-08T00:00:00+00:00",
                        "2026-07-08T00:00:00+00:00",
                    ),
                )

            legacy = ChatDatabaseService(db_path)

            with sqlite3.connect(db_path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(chats)")
                }

            self.assertIn("turn_timestamps", columns)
            self.assertEqual([], legacy.get_chat("legacy-chat")["turn_timestamps"])
            self.assertEqual(
                ["legacy-chat"],
                [item["chat_id"] for item in legacy.list_chats()],
            )

    def test_repository_source_does_not_depend_on_anythingllm(self) -> None:
        chat_dir = Path(__file__).resolve().parents[1] / "app" / "services" / "chat"
        for source_path in chat_dir.glob("*.py"):
            with self.subTest(source_path=source_path.name):
                source = source_path.read_text(encoding="utf-8")
                self.assertNotIn("anythingllm", source.casefold())


class ChatRepositoryBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.db_path = f"{self.tmp}/chat.sqlite3"
        self.store = ChatStore(self.db_path)

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def test_sessions_and_documents_are_idempotent(self) -> None:
        session = self.store.sessions.create_or_get(
            chat_id="chat-a",
            workspace_ref="workspace-a",
            thread_ref="thread-a",
            metadata={"标题": "测试"},
        )
        same_session = self.store.sessions.create_or_get(
            chat_id="chat-a",
            workspace_ref="workspace-a",
        )

        self.assertEqual(SESSION_ACTIVE, session.status)
        self.assertEqual(session, same_session)
        self.assertEqual("测试", session.metadata["标题"])

        with self.assertRaises(ValueError):
            self.store.sessions.create_or_get(
                chat_id="chat-b",
                metadata={"bad": math.nan},
            )

        self.store.runs.create(run_id="run-first", chat_id="chat-a")
        self.store.runs.create(run_id="run-second", chat_id="chat-a")
        first = self.store.documents.add(
            chat_id="chat-a",
            file_name="hash.pdf",
            original_name="原名.pdf",
            document_ref="document:first",
            external_location="custom-documents/first.json",
            added_by_run_id="run-first",
        )
        second = self.store.documents.add(
            chat_id="chat-a",
            file_name="hash.pdf",
            original_name="更新原名.pdf",
            document_ref="document:second",
            external_location="custom-documents/second.json",
            added_by_run_id="run-second",
        )
        documents = self.store.documents.list_by_chat("chat-a")

        self.assertEqual(first.created_at, second.created_at)
        self.assertEqual(1, len(documents))
        self.assertEqual("更新原名.pdf", documents[0].original_name)
        self.assertEqual("document:second", documents[0].document_ref)
        self.assertEqual("run-first", documents[0].added_by_run_id)

    def test_session_create_or_get_enriches_empty_placeholder(self) -> None:
        placeholder = self.store.sessions.create_or_get(chat_id="chat-placeholder")

        enriched = self.store.sessions.create_or_get(
            chat_id="chat-placeholder",
            workspace_ref="workspace-ref",
            thread_ref="thread-ref",
            metadata={"source": "chat"},
        )
        same = self.store.sessions.create_or_get(
            chat_id="chat-placeholder",
            workspace_ref="workspace-ref",
            metadata={"source": "chat"},
        )

        self.assertEqual("", placeholder.workspace_ref)
        self.assertEqual("workspace-ref", enriched.workspace_ref)
        self.assertEqual("thread-ref", enriched.thread_ref)
        self.assertEqual("chat", enriched.metadata["source"])
        self.assertEqual(enriched, same)

        with self.assertRaisesRegex(ValueError, "metadata 冲突"):
            self.store.sessions.create_or_get(
                chat_id="chat-placeholder",
                metadata={"source": "other"},
            )

    def test_run_status_transitions_and_active_lookup(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-run")
        accepted = self.store.runs.create(
            run_id="run-a",
            chat_id="chat-run",
            request_id="request-a",
            owner_instance_id="instance-a",
        )
        self.store.runs.create(run_id="run-b", chat_id="chat-run")

        running = self.store.runs.mark_running("run-a")
        abort_requested = self.store.runs.request_abort("run-a")
        failed = self.store.runs.mark_failed(
            "run-a",
            error_message="模型响应失败",
        )
        active_run_ids = [run.run_id for run in self.store.runs.list_active("chat-run")]

        self.assertEqual(RUN_ACCEPTED, accepted.status)
        self.assertEqual(RUN_RUNNING, running.status)
        self.assertIsNotNone(running.started_at)
        self.assertTrue(abort_requested.abort_requested)
        self.assertEqual(RUN_FAILED, failed.status)
        self.assertEqual("模型响应失败", failed.error_message)
        self.assertIsNotNone(failed.completed_at)
        self.assertEqual(["run-b"], active_run_ids)

        with self.assertRaises(ValueError):
            self.store.runs.mark_running("run-a")
        with self.assertRaises(ValueError):
            self.store.runs.request_abort("run-a")

    def test_run_create_rejects_identity_conflicts(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-identity")
        created = self.store.runs.create(
            run_id="run-identity",
            chat_id="chat-identity",
            request_id="request-a",
            owner_instance_id="owner-a",
        )
        self.store.runs.mark_running("run-identity")

        same = self.store.runs.create(
            run_id="run-identity",
            chat_id="chat-identity",
            request_id="request-a",
            owner_instance_id="owner-a",
        )

        self.assertEqual(RUN_ACCEPTED, created.status)
        self.assertEqual(RUN_RUNNING, same.status)
        with self.assertRaises(ValueError):
            self.store.runs.create(
                run_id="run-identity",
                chat_id="chat-identity",
                request_id="request-b",
                owner_instance_id="owner-a",
            )
        with self.assertRaises(ValueError):
            self.store.runs.create(
                run_id="run-identity",
                chat_id="chat-identity",
                request_id="request-a",
                owner_instance_id="owner-b",
            )

    def test_terminal_run_status_is_not_reopened(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-terminal")
        self.store.runs.create(run_id="run-terminal", chat_id="chat-terminal")
        self.store.runs.mark_running("run-terminal")
        succeeded = self.store.runs.mark_succeeded("run-terminal")

        with self.assertRaises(ValueError):
            self.store.runs.mark_failed(
                "run-terminal",
                error_message="late failure",
            )

        self.assertEqual(RUN_SUCCEEDED, succeeded.status)
        self.assertEqual(
            succeeded.completed_at,
            self.store.runs.mark_succeeded("run-terminal").completed_at,
        )

    def test_run_must_be_created_as_accepted(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-run-create")

        with self.assertRaisesRegex(ValueError, "accepted"):
            self.store.runs.create(
                run_id="run-created-running",
                chat_id="chat-run-create",
                status=RUN_RUNNING,
            )
        with self.assertRaisesRegex(ValueError, "accepted"):
            self.store.runs.create(
                run_id="run-created-succeeded",
                chat_id="chat-run-create",
                status=RUN_SUCCEEDED,
            )

    def test_messages_keep_sequence_and_linked_files(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-message")
        self.store.runs.create(run_id="run-message", chat_id="chat-message")

        user_message = self.store.messages.append(
            message_id="message-user",
            chat_id="chat-message",
            run_id="run-message",
            role=MESSAGE_ROLE_USER,
            content="你好",
            status=MESSAGE_COMMITTED,
            files=(("hash.pdf", "原名.pdf"),),
        )
        assistant_message = self.store.messages.append(
            message_id="message-assistant",
            chat_id="chat-message",
            run_id="run-message",
            role=MESSAGE_ROLE_ASSISTANT,
            content="  回答\n",
            status=MESSAGE_COMMITTED,
        )
        messages = self.store.messages.list_by_chat("chat-message")

        self.assertEqual(1, user_message.sequence_no)
        self.assertEqual(2, assistant_message.sequence_no)
        self.assertEqual(("message-user", "message-assistant"), tuple(message.message_id for message in messages))
        self.assertEqual("  回答\n", messages[1].content)
        self.assertEqual("hash.pdf", messages[0].files[0].file_name)
        self.assertEqual("原名.pdf", messages[0].files[0].original_name)

    def test_message_append_is_idempotent_and_rejects_conflicts(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-message-retry")
        self.store.runs.create(
            run_id="run-message-retry",
            chat_id="chat-message-retry",
        )
        arguments = {
            "message_id": "message-retry",
            "chat_id": "chat-message-retry",
            "run_id": "run-message-retry",
            "role": MESSAGE_ROLE_USER,
            "content": "请总结",
            "status": MESSAGE_COMMITTED,
            "files": (("b.pdf", "乙.pdf"), ("a.pdf", "甲.pdf")),
        }

        first = self.store.messages.append(**arguments)
        second = self.store.messages.append(**arguments)

        self.assertEqual(first, second)
        self.assertEqual(1, second.sequence_no)
        self.assertEqual(("a.pdf", "b.pdf"), tuple(item.file_name for item in second.files))
        with self.assertRaisesRegex(ValueError, "身份或内容冲突"):
            self.store.messages.append(**{**arguments, "content": "不同内容"})

    def test_run_associations_must_belong_to_same_chat(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-left")
        self.store.sessions.create_or_get(chat_id="chat-right")
        self.store.runs.create(run_id="run-right", chat_id="chat-right")

        with self.assertRaisesRegex(ValueError, "不属于当前 chat_id"):
            self.store.messages.append(
                message_id="message-cross-chat",
                chat_id="chat-left",
                run_id="run-right",
                role=MESSAGE_ROLE_USER,
                content="cross",
                status=MESSAGE_COMMITTED,
            )
        with self.assertRaisesRegex(ValueError, "不属于当前 chat_id"):
            self.store.documents.add(
                chat_id="chat-left",
                file_name="cross.pdf",
                added_by_run_id="run-right",
            )
        with self.assertRaisesRegex(ValueError, "不属于当前 chat_id"):
            self.store.resource_leases.begin(
                lease_id="lease-cross-chat",
                chat_id="chat-left",
                run_id="run-right",
                resource_type="workspace",
            )

        with sqlite3.connect(self.db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "does not belong"):
                connection.execute(
                    """
                    INSERT INTO chat_messages (
                        message_id, chat_id, run_id, role, content,
                        status, sequence_no, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "message-direct-cross-chat",
                        "chat-left",
                        "run-right",
                        MESSAGE_ROLE_USER,
                        "cross",
                        MESSAGE_COMMITTED,
                        1,
                        "2026-07-08T00:00:00+00:00",
                    ),
                )

    def test_message_history_loads_files_without_n_plus_one_queries(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-query-count")
        self.store.runs.create(run_id="run-query-count", chat_id="chat-query-count")
        for index in range(3):
            self.store.messages.append(
                message_id=f"message-query-{index}",
                chat_id="chat-query-count",
                run_id="run-query-count",
                role=MESSAGE_ROLE_USER,
                content=f"message-{index}",
                status=MESSAGE_COMMITTED,
                files=((f"{index}.pdf", f"{index}.pdf"),),
            )

        statements: list[str] = []
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.set_trace_callback(statements.append)
        with patch(
            "app.services.chat.persistence.repositories._connect",
            return_value=connection,
        ):
            messages = self.store.messages.list_by_chat("chat-query-count")

        select_statements = [
            statement for statement in statements if statement.lstrip().upper().startswith("SELECT")
        ]
        self.assertEqual(3, len(messages))
        self.assertEqual(2, len(select_statements))

    def test_resource_lease_lifecycle(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-lease")
        self.store.runs.create(run_id="run-lease", chat_id="chat-lease")

        planned = self.store.resource_leases.begin(
            lease_id="lease-workspace",
            chat_id="chat-lease",
            run_id="run-lease",
            resource_type="workspace",
        )
        active = self.store.resource_leases.activate(
            lease_id="lease-workspace",
            external_ref="workspace-ref",
        )
        failed = self.store.resource_leases.record_cleanup_failure(
            lease_id="lease-workspace",
            error_message="delete failed",
        )
        closed = self.store.resource_leases.mark_closed("lease-workspace")

        self.assertEqual("planned", planned.status)
        self.assertEqual("active", active.status)
        self.assertEqual("workspace-ref", active.external_ref)
        self.assertEqual(LEASE_CLEANUP_FAILED, failed.status)
        self.assertEqual("delete failed", failed.error_message)
        self.assertEqual(LEASE_CLOSED, closed.status)
        self.assertEqual((), self.store.resource_leases.list_open())

        with self.assertRaises(ValueError):
            self.store.resource_leases.activate(
                lease_id="lease-workspace",
                external_ref="workspace-ref-2",
            )
        with self.assertRaises(ValueError):
            self.store.resource_leases.mark_cleanup_pending("lease-workspace")

    def test_resource_lease_allows_cleanup_retry_after_failure(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-lease-retry")
        self.store.runs.create(run_id="run-lease-retry", chat_id="chat-lease-retry")
        self.store.resource_leases.begin(
            lease_id="lease-retry",
            chat_id="chat-lease-retry",
            run_id="run-lease-retry",
            resource_type="workspace",
        )
        self.store.resource_leases.activate(
            lease_id="lease-retry",
            external_ref="workspace-retry",
        )
        failed = self.store.resource_leases.record_cleanup_failure(
            lease_id="lease-retry",
            error_message="delete failed",
        )
        pending = self.store.resource_leases.mark_cleanup_pending("lease-retry")

        self.assertEqual(LEASE_CLEANUP_FAILED, failed.status)
        self.assertEqual(LEASE_CLEANUP_PENDING, pending.status)

    def test_active_resource_lease_external_ref_is_immutable(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-lease-immutable")
        self.store.runs.create(
            run_id="run-lease-immutable",
            chat_id="chat-lease-immutable",
        )
        self.store.resource_leases.begin(
            lease_id="lease-immutable",
            chat_id="chat-lease-immutable",
            run_id="run-lease-immutable",
            resource_type="workspace",
        )
        active = self.store.resource_leases.activate(
            lease_id="lease-immutable",
            external_ref="workspace:first",
        )
        same = self.store.resource_leases.activate(
            lease_id="lease-immutable",
            external_ref="workspace:first",
        )

        self.assertEqual(active, same)
        with self.assertRaisesRegex(ValueError, "external_ref 冲突"):
            self.store.resource_leases.activate(
                lease_id="lease-immutable",
                external_ref="workspace:second",
            )
        with sqlite3.connect(self.db_path) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    """
                    UPDATE chat_resource_leases
                    SET external_ref = 'workspace:second'
                    WHERE lease_id = 'lease-immutable'
                    """
                )

        self.store.resource_leases.begin(
            lease_id="lease-closed-without-ref",
            chat_id="chat-lease-immutable",
            run_id="run-lease-immutable",
            resource_type="thread",
        )
        self.store.resource_leases.mark_closed("lease-closed-without-ref")
        with self.assertRaisesRegex(ValueError, "非 planned"):
            self.store.resource_leases.begin(
                lease_id="lease-closed-without-ref",
                chat_id="chat-lease-immutable",
                run_id="run-lease-immutable",
                resource_type="thread",
                external_ref="thread:late",
            )

    def test_delete_chat_clears_legacy_and_stage3_tables(self) -> None:
        legacy = ChatDatabaseService(self.db_path)
        legacy.create_chat("chat-delete", ["delete.pdf"], "ws-delete", "th-delete")
        self.store.sessions.create_or_get(chat_id="chat-delete")
        self.store.runs.create(run_id="run-delete", chat_id="chat-delete")
        self.store.documents.add(
            chat_id="chat-delete",
            file_name="delete.pdf",
            original_name="delete.pdf",
        )
        self.store.messages.append(
            message_id="message-delete",
            chat_id="chat-delete",
            run_id="run-delete",
            role=MESSAGE_ROLE_USER,
            content="delete",
            status=MESSAGE_COMMITTED,
            files=(("delete.pdf", "delete.pdf"),),
        )
        self.store.resource_leases.begin(
            lease_id="lease-delete",
            chat_id="chat-delete",
            run_id="run-delete",
            resource_type="workspace",
        )

        legacy.delete_chat("chat-delete")

        self.assertIsNone(legacy.get_chat("chat-delete"))
        self.assertIsNone(self.store.sessions.get("chat-delete"))
        self.assertEqual((), self.store.documents.list_by_chat("chat-delete"))
        self.assertEqual((), self.store.messages.list_by_chat("chat-delete"))
        self.assertIsNone(self.store.runs.get("run-delete"))
        self.assertIsNone(self.store.resource_leases.get("lease-delete"))


if __name__ == "__main__":
    unittest.main()
