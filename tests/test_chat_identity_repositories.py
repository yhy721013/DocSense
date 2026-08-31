"""Conversation 复合身份、准入 Guard 与世代释放的 SQLite 门禁。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.modules.chat.adapters.sqlite.identity_repository import (
    SQLiteConversationIdentityRepository,
)
from app.modules.chat.adapters.sqlite.repositories import _connect
from app.modules.chat.domain.identity import FileChatIdentity, WeaponryChatIdentity
from app.modules.chat.ports.identities import (
    ConversationAdmissionBusyError,
    ConversationIdentityConflictError,
    FileConversationTombstonedError,
)


class ConversationIdentityRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = f"{self._tempdir.name}/chat.sqlite3"
        self.repository = SQLiteConversationIdentityRepository(
            self.db_path,
            owner_instance_id="identity-test-instance",
        )

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def test_schema_contains_identity_guard_chunk_and_audit_constraints(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            versions = connection.execute(
                "SELECT version FROM chat_schema_migrations ORDER BY version"
            ).fetchall()
        self.assertTrue(
            {
                "conversation_identities",
                "conversation_admissions",
                "message_source_chunks",
                "conversation_deletion_audits",
            }.issubset(tables)
        )
        self.assertEqual(
            [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,)],
            versions,
        )

    def test_same_composite_identity_fifty_threads_create_one_generation(self) -> None:
        identity = WeaponryChatIdentity(user_id=9, architecture_id=17)

        def create_once(_: int) -> str:
            try:
                return self.repository.create_conversation(identity).conversation_id
            except ConversationIdentityConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=50) as executor:
            results = list(executor.map(create_once, range(50)))

        winners = [item for item in results if item != "conflict"]
        self.assertEqual(1, len(winners))
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(
                1,
                connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM conversation_identities"
                ).fetchone()[0],
            )

    def test_different_public_identities_do_not_conflict(self) -> None:
        identities = (
            FileChatIdentity(chat_id=1),
            FileChatIdentity(chat_id=2),
            WeaponryChatIdentity(user_id=1, architecture_id=1),
            WeaponryChatIdentity(user_id=2, architecture_id=1),
            WeaponryChatIdentity(user_id=1, architecture_id=2),
        )
        conversation_ids = {
            self.repository.create_conversation(identity).conversation_id
            for identity in identities
        }
        self.assertEqual(len(identities), len(conversation_ids))

    def test_guard_conflict_creates_no_business_rows(self) -> None:
        identity = WeaponryChatIdentity(user_id=3, architecture_id=4)
        first = self.repository.reserve_admission(identity)
        with self.assertRaises(ConversationAdmissionBusyError):
            self.repository.reserve_admission(identity)
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM conversation_identities"
                ).fetchone()[0],
            )
        self.assertTrue(self.repository.release_admission(first))

    def test_expired_token_cannot_delete_new_guard(self) -> None:
        identity = WeaponryChatIdentity(user_id=5, architecture_id=6)
        old = self.repository.reserve_admission(identity)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE conversation_admissions SET expires_at = '2000-01-01T00:00:00+00:00'"
            )
        current = self.repository.reserve_admission(identity)
        self.assertFalse(self.repository.release_admission(old))
        with sqlite3.connect(self.db_path) as connection:
            token = connection.execute(
                "SELECT admission_token FROM conversation_admissions"
            ).fetchone()[0]
        self.assertEqual(current.admission_token, token)

    def test_file_tombstone_is_never_reused(self) -> None:
        identity = FileChatIdentity(chat_id=88)
        created = self.repository.create_conversation(identity)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE conversations SET status = 'deleted' WHERE conversation_id = ?",
                (created.conversation_id,),
            )
        self.assertIsNone(self.repository.resolve_active(identity))
        self.assertIsNotNone(self.repository.resolve_any(identity))
        with self.assertRaises(FileConversationTombstonedError):
            self.repository.create_conversation(identity)

    def test_finalize_weaponry_delete_purges_body_and_releases_identity_atomically(self) -> None:
        """删除成功只留下不含正文的最小审计事实，并允许创建新世代。"""

        identity = WeaponryChatIdentity(user_id=31, architecture_id=41)
        created = self.repository.create_conversation(identity)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO chat_runs(
                    run_id, conversation_id, status, abort_requested,
                    owner_instance_id, heartbeat_at, error_message,
                    created_at, started_at, completed_at, updated_at
                ) VALUES ('run-delete', ?, 'succeeded', 0, 'owner', NULL, '',
                          'now', 'now', 'now', 'now')
                """,
                (created.conversation_id,),
            )
            connection.execute(
                """
                INSERT INTO chat_messages(
                    message_id, conversation_id, run_id, role, content,
                    status, sequence_no, architecture_id, created_at
                ) VALUES ('assistant-delete', ?, 'run-delete', 'assistant',
                          '敏感正文', 'committed', 1, NULL, 'now')
                """,
                (created.conversation_id,),
            )
            connection.execute(
                """
                INSERT INTO message_source_chunks(
                    message_id, position, content, file_name,
                    original_file_name, created_at
                ) VALUES ('assistant-delete', 0, 'Chunk 正文', 'stored.pdf',
                          '原文件.pdf', 'now')
                """
            )
            # 仓储终态不能只相信调用顺序：测试显式建立与生产 DeleteService 相同的
            # deleting + cleanup succeeded 权威前置事实，再验证最终物理清除。
            connection.execute(
                """
                UPDATE conversations
                SET status = 'deleting', updated_at = 'now'
                WHERE conversation_id = ?
                """,
                (created.conversation_id,),
            )
            connection.execute(
                """
                INSERT INTO chat_cleanup_jobs(
                    job_id, conversation_id, reason, lease_id, status,
                    attempt_count, next_attempt_at, error_message,
                    created_at, updated_at
                ) VALUES ('cleanup-delete', ?, 'delete_chat', '', 'succeeded',
                          1, 'now', '', 'now', 'now')
                """,
                (created.conversation_id,),
            )

        self.assertIsNone(
            self.repository.finalize_completed_delete(created.conversation_id)
        )
        with sqlite3.connect(self.db_path) as connection:
            # Weaponry 删除后整个在线聚合均应消失，不能残留供应商引用、身份、运行、
            # 消息、Chunk、范围、租约或清理任务。独立审计表是唯一允许保留的业务事实。
            aggregate_tables = (
                "conversations",
                "conversation_identities",
                "chat_runs",
                "chat_messages",
                "message_source_chunks",
                "chat_resource_leases",
                "chat_cleanup_jobs",
            )
            for table_name in aggregate_tables:
                self.assertEqual(
                    0,
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table_name}"
                    ).fetchone()[0],
                    table_name,
                )
            audit_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(conversation_deletion_audits)"
                )
            }
            self.assertEqual(
                {"conversation_id", "identity_kind", "deletion_status",
                 "cleanup_result", "deleted_at"},
                audit_columns,
            )
            self.assertEqual(
                [],
                connection.execute(
                    "PRAGMA foreign_key_list(conversation_deletion_audits)"
                ).fetchall(),
            )
            self.assertEqual(
                (
                    created.conversation_id,
                    "weaponry",
                    "deleted",
                    "succeeded",
                ),
                connection.execute(
                    """
                    SELECT conversation_id, identity_kind, deletion_status,
                           cleanup_result
                    FROM conversation_deletion_audits
                    """
                ).fetchone(),
            )
        replacement = self.repository.create_conversation(identity)
        self.assertNotEqual(created.conversation_id, replacement.conversation_id)

    def test_finalize_delete_rejects_missing_cleanup_success_fact(self) -> None:
        """绕过 DeleteService 不得跳过删除状态、清理成功事实或租约关闭条件。"""

        created = self.repository.create_conversation(
            WeaponryChatIdentity(user_id=51, architecture_id=61)
        )

        with self.assertRaisesRegex(
            ValueError,
            "conversation must be deleting before finalization",
        ):
            self.repository.finalize_completed_delete(created.conversation_id)

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE conversations SET status = 'deleting' WHERE conversation_id = ?",
                (created.conversation_id,),
            )

        with self.assertRaisesRegex(
            ValueError,
            "conversation delete cleanup has not succeeded",
        ):
            self.repository.finalize_completed_delete(created.conversation_id)

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO chat_cleanup_jobs(
                    job_id, conversation_id, reason, lease_id, status,
                    attempt_count, next_attempt_at, error_message,
                    created_at, updated_at
                ) VALUES ('cleanup-delete', ?, 'delete_chat', '', 'succeeded',
                          1, 'now', '', 'now', 'now')
                """,
                (created.conversation_id,),
            )
            connection.execute(
                """
                INSERT INTO chat_resource_leases(
                    lease_id, conversation_id, run_id, resource_type,
                    external_ref, status, error_message, created_at, updated_at
                ) VALUES ('lease-open', ?, '', 'workspace', '', 'planned',
                          '', 'now', 'now')
                """,
                (created.conversation_id,),
            )

        with self.assertRaisesRegex(
            ValueError,
            "conversation still has unresolved resource leases",
        ):
            self.repository.finalize_completed_delete(created.conversation_id)

        self.assertIsNotNone(
            self.repository.get_by_conversation_id(created.conversation_id)
        )

    def test_foreign_keys_and_identity_check_constraints_are_enforced(self) -> None:
        connection = _connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO conversation_identities(
                        conversation_id, identity_kind, chat_id, user_id,
                        architecture_id, active, created_at, released_at
                    ) VALUES (?, 'weaponry', NULL, 0, 1, 1, 'now', '')
                    """,
                    ("abcdefab-1234-5678-9234-567812345678",),
                )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
