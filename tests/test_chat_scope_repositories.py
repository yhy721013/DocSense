"""文件对话 Scope Schema v4 与 SQLite Repository 离线测试。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

from app.services.chat import (
    CHAT_SCOPE_SELECTION_ARCHITECTURE_INITIAL,
    CHAT_SCOPE_SOURCE_ARCHITECTURE_INITIAL,
    CHAT_SCOPE_SELECTION_EXPLICIT,
    CHAT_SCOPE_SOURCE_AUTOMATIC_INITIAL,
    CHAT_SCOPE_SOURCE_EXPLICIT,
    ChatDocumentCandidate,
    ChatRunLockService,
    ChatScopeRevision,
    ChatSessionScopeBinding,
    ChatStore,
    chat_scope_revision_id_for_run,
)
from app.services.chat.persistence import repositories as chat_repositories


_NOW = "2026-07-28T00:00:00+00:00"


def _member(file_name: str) -> ChatDocumentCandidate:
    return ChatDocumentCandidate(
        file_name=file_name,
        original_name=f"{file_name}.original",
        document_ref=f"document:{file_name}",
        external_location=f"custom-documents/{file_name}.json",
    )


class ChatScopeRepositoryTests(unittest.TestCase):
    """验证 Scope 事实、Head CAS 与 run input 引用保持原子可读。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "chat.sqlite3")
        self.store = ChatStore(self.db_path)
        self.locks = ChatRunLockService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_run(self, *, chat_id: str, run_id: str):
        return self.locks.try_acquire_chat_run(
            chat_id=chat_id,
            run_id=run_id,
        )

    def _revision(
        self,
        *,
        chat_id: str,
        run_id: str,
        file_names: tuple[str, ...],
        source_mode: str = CHAT_SCOPE_SOURCE_EXPLICIT,
    ) -> ChatScopeRevision:
        return ChatScopeRevision(
            scope_revision_id=chat_scope_revision_id_for_run(run_id),
            chat_id=chat_id,
            source_mode=source_mode,
            source_run_id=run_id,
            members=tuple(_member(name) for name in file_names),
            created_at=_NOW,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def test_schema_v5_tables_columns_and_migration_are_present(self) -> None:
        with closing(self._connect()) as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            versions = {
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM chat_schema_migrations"
                )
            }
            input_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(chat_run_inputs)"
                )
            }
            revision_foreign_keys = {
                row["from"]
                for row in connection.execute(
                    "PRAGMA foreign_key_list(chat_scope_revisions)"
                )
            }

        self.assertTrue(
            {
                "chat_scope_revisions",
                "chat_scope_members",
                "chat_scope_heads",
            }.issubset(tables)
        )
        self.assertEqual({1, 2, 3, 4, 5}, versions)
        self.assertTrue(
            {
                "requested_files_json",
                "effective_scope_revision_id",
                "selection_mode",
                "requested_architecture_id",
            }.issubset(input_columns)
        )
        self.assertIn("chat_session_scope_bindings", tables)
        self.assertIn("chat_id", revision_foreign_keys)
        self.assertNotIn("source_run_id", revision_foreign_keys)

    def test_v4_fixture_migrates_to_v5_without_scope_or_message_drift(self) -> None:
        """真实形状 v4 数据迁移后行数、字段、Head 和消息必须逐项等价。"""
        db_path = str(Path(self.temp_dir.name) / "v4-fixture.sqlite3")
        with patch.object(
            chat_repositories,
            "_CHAT_SCHEMA_MIGRATIONS",
            chat_repositories._CHAT_SCHEMA_MIGRATIONS[:4],
        ):
            chat_repositories.ensure_chat_schema(db_path)

        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT INTO chat_sessions (
                    chat_id, status, created_at, updated_at
                ) VALUES ('chat-v4', 'active', ?, ?)
                """,
                (_NOW, _NOW),
            )
            connection.execute(
                """
                INSERT INTO chat_runs (
                    run_id, chat_id, status, created_at, updated_at
                ) VALUES ('run-v4', 'chat-v4', 'accepted', ?, ?)
                """,
                (_NOW, _NOW),
            )
            connection.execute(
                """
                INSERT INTO chat_scope_revisions (
                    scope_revision_id, chat_id, source_mode,
                    source_run_id, created_at
                ) VALUES ('scope-v4', 'chat-v4', 'explicit', 'run-v4', ?)
                """,
                (_NOW,),
            )
            connection.execute(
                """
                INSERT INTO chat_scope_members (
                    scope_revision_id, ordinal, file_name, original_name,
                    document_ref, external_location
                ) VALUES (
                    'scope-v4', 0, 'a.pdf', 'A.pdf',
                    'document:a', 'custom-documents/a.json'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO chat_scope_heads (
                    chat_id, scope_revision_id, updated_at
                ) VALUES ('chat-v4', 'scope-v4', ?)
                """,
                (_NOW,),
            )
            requested_json = json.dumps(
                [{"file_name": "a.pdf", "original_name": "A.pdf"}],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO chat_run_inputs (
                    run_id, message, files_json, created_at,
                    requested_files_json, effective_scope_revision_id,
                    selection_mode
                ) VALUES (
                    'run-v4', '问题', '[]', ?, ?, 'scope-v4', 'explicit'
                )
                """,
                (_NOW, requested_json),
            )
            connection.execute(
                """
                INSERT INTO chat_messages (
                    message_id, chat_id, run_id, role, content,
                    status, sequence_no, created_at
                ) VALUES (
                    'message-v4', 'chat-v4', 'run-v4', 'user', '问题',
                    'pending', 1, ?
                )
                """,
                (_NOW,),
            )
            connection.execute(
                """
                INSERT INTO chat_message_files (
                    message_id, file_name, original_name
                ) VALUES ('message-v4', 'a.pdf', 'A.pdf')
                """
            )
            connection.commit()

        chat_repositories.ensure_chat_schema(db_path)
        store = ChatStore(db_path)

        binding = store.session_scope_bindings.get("chat-v4")
        revision = store.scopes.get_current_revision("chat-v4")
        run_input = store.run_inputs.get("run-v4")
        messages = store.messages.list_by_chat("chat-v4")
        self.assertEqual("files", binding.scope_mode)
        self.assertIsNone(binding.architecture_id)
        self.assertEqual("explicit", revision.source_mode)
        self.assertIsNone(revision.source_architecture_id)
        self.assertEqual(("a.pdf",), tuple(item.file_name for item in revision.members))
        self.assertIsNone(run_input.requested_architecture_id)
        self.assertEqual(("a.pdf",), tuple(item.file_name for item in run_input.files))
        self.assertEqual(("a.pdf",), tuple(item.file_name for item in messages[0].files))
        self.assertIsNone(messages[0].architecture_id)

        # 重复 ensure 不得重复迁移或改变事实。
        chat_repositories.ensure_chat_schema(db_path)
        with closing(sqlite3.connect(db_path)) as connection:
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual(
                [1, 2, 3, 4, 5],
                [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM chat_schema_migrations ORDER BY version"
                    )
                ],
            )
            self.assertEqual(
                (1, 1, 1),
                (
                    connection.execute("SELECT COUNT(*) FROM chat_scope_revisions").fetchone()[0],
                    connection.execute("SELECT COUNT(*) FROM chat_scope_members").fetchone()[0],
                    connection.execute("SELECT COUNT(*) FROM chat_scope_heads").fetchone()[0],
                ),
            )

    def test_v5_migration_failure_rolls_back_all_schema_changes(self) -> None:
        """故障发生在 v5 DDL 完成后也不能留下半张表或版本记录。"""
        db_path = str(Path(self.temp_dir.name) / "v5-failure.sqlite3")
        with patch.object(
            chat_repositories,
            "_CHAT_SCHEMA_MIGRATIONS",
            chat_repositories._CHAT_SCHEMA_MIGRATIONS[:4],
        ):
            chat_repositories.ensure_chat_schema(db_path)

        def fail_after_v5(connection: sqlite3.Connection) -> None:
            chat_repositories._add_architecture_chat_scope(connection)
            raise RuntimeError("forced v5 migration failure")

        failing_migrations = (
            *chat_repositories._CHAT_SCHEMA_MIGRATIONS[:4],
            (5, fail_after_v5),
        )
        with patch.object(
            chat_repositories,
            "_CHAT_SCHEMA_MIGRATIONS",
            failing_migrations,
        ):
            with self.assertRaisesRegex(RuntimeError, "forced v5"):
                chat_repositories.ensure_chat_schema(db_path)

        with closing(sqlite3.connect(db_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            run_input_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(chat_run_inputs)"
                )
            }
            self.assertNotIn("chat_session_scope_bindings", tables)
            self.assertNotIn("requested_architecture_id", run_input_columns)
            self.assertEqual(
                [1, 2, 3, 4],
                [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM chat_schema_migrations ORDER BY version"
                    )
                ],
            )

        # 回滚后的 v4 库仍可由正常迁移一次升级成功。
        chat_repositories.ensure_chat_schema(db_path)
        self.assertIsNotNone(ChatStore(db_path).session_scope_bindings)

    def test_architecture_binding_scope_run_input_and_message_are_consistent(self) -> None:
        # 本用例刻意逐层写入 Schema v5 事实，因此不能调用已经会自动创建 files
        # Binding 的生产 Coordinator；先直接构造合法 Session/Run 作为外键父记录。
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO chat_sessions (
                    chat_id, workspace_ref, thread_ref, status,
                    created_at, updated_at, metadata_json
                ) VALUES ('chat-architecture', '', '', 'active', ?, ?, '{}')
                """,
                (_NOW, _NOW),
            )
            connection.execute(
                """
                INSERT INTO chat_runs (
                    run_id, chat_id, status, abort_requested,
                    owner_instance_id, heartbeat_at, error_message,
                    created_at, started_at, completed_at, updated_at
                ) VALUES (
                    'run-architecture', 'chat-architecture', 'accepted', 0,
                    '', NULL, '', ?, NULL, NULL, ?
                )
                """,
                (_NOW, _NOW),
            )
            connection.commit()
        run = self.store.runs.get("run-architecture")
        self.assertIsNotNone(run)
        assert run is not None
        binding = ChatSessionScopeBinding(
            chat_id="chat-architecture",
            scope_mode="architecture",
            architecture_id=7,
            created_at=_NOW,
        )
        self.store.session_scope_bindings.create(binding)
        revision = ChatScopeRevision(
            scope_revision_id=chat_scope_revision_id_for_run(run.run_id),
            chat_id="chat-architecture",
            source_mode=CHAT_SCOPE_SOURCE_ARCHITECTURE_INITIAL,
            source_run_id=run.run_id,
            source_architecture_id=7,
            members=(_member("a.pdf"),),
            created_at=_NOW,
        )
        self.store.scopes.append_and_set_head(
            revision=revision,
            expected_current_revision_id=None,
        )
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO chat_run_inputs (
                    run_id, message, files_json, created_at,
                    requested_files_json, effective_scope_revision_id,
                    selection_mode, requested_architecture_id
                ) VALUES (?, '问题', '[]', ?, '[]', ?, ?, 7)
                """,
                (
                    run.run_id,
                    _NOW,
                    revision.scope_revision_id,
                    CHAT_SCOPE_SELECTION_ARCHITECTURE_INITIAL,
                ),
            )
            connection.commit()
        user_message = self.store.messages.append(
            message_id="message-architecture-user",
            chat_id="chat-architecture",
            run_id=run.run_id,
            role="user",
            content="问题",
            status="pending",
            architecture_id=7,
        )
        assistant_message = self.store.messages.append(
            message_id="message-architecture-assistant",
            chat_id="chat-architecture",
            run_id=run.run_id,
            role="assistant",
            content="回答",
            status="committed",
        )

        loaded_input = self.store.run_inputs.get(run.run_id)
        self.assertEqual(7, loaded_input.requested_architecture_id)
        self.assertEqual(7, user_message.architecture_id)
        self.assertEqual((), user_message.files)
        self.assertIsNone(assistant_message.architecture_id)
        self.assertEqual((), assistant_message.files)

        with closing(self._connect()) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE chat_session_scope_bindings SET architecture_id = 8 "
                    "WHERE chat_id = 'chat-architecture'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO chat_message_files (
                        message_id, file_name, original_name
                    ) VALUES ('message-architecture-user', 'a.pdf', 'A.pdf')
                    """
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE chat_run_inputs SET requested_architecture_id = 8 "
                    "WHERE run_id = 'run-architecture'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO chat_scope_revisions (
                        scope_revision_id, chat_id, source_mode,
                        source_run_id, source_architecture_id, created_at
                    ) VALUES (
                        'wrong-architecture-scope', 'chat-architecture',
                        'architecture_initial', 'run-architecture', 8, ?
                    )
                    """,
                    (_NOW,),
                )

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.messages.append(
                message_id="message-architecture-missing-id",
                chat_id="chat-architecture",
                run_id=run.run_id,
                role="user",
                content="缺少 ID",
                status="pending",
            )
        with self.assertRaises(ValueError):
            self.store.messages.append(
                message_id="message-assistant-with-id",
                chat_id="chat-architecture",
                run_id=run.run_id,
                role="assistant",
                content="错误",
                status="committed",
                architecture_id=7,
            )

    def test_fifty_architecture_bindings_are_isolated(self) -> None:
        count = 50
        barrier = Barrier(count)

        def create_binding(index: int) -> tuple[str, int]:
            chat_id = f"binding-chat-{index}"
            self.store.sessions.create_or_get(chat_id=chat_id)
            barrier.wait(timeout=10)
            binding = self.store.session_scope_bindings.create(
                ChatSessionScopeBinding(
                    chat_id=chat_id,
                    scope_mode="architecture",
                    architecture_id=index + 1,
                    created_at=_NOW,
                )
            )
            return binding.chat_id, int(binding.architecture_id)

        with ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(create_binding, range(count)))

        self.assertEqual(count, len(set(results)))
        for chat_id, architecture_id in results:
            binding = self.store.session_scope_bindings.get(chat_id)
            self.assertEqual(architecture_id, binding.architecture_id)

    def test_append_revision_reads_ordered_members_and_head(self) -> None:
        self._create_run(chat_id="chat-a", run_id="run-a")
        revision = self._revision(
            chat_id="chat-a",
            run_id="run-a",
            file_names=("b.pdf", "a.pdf"),
            source_mode=CHAT_SCOPE_SOURCE_AUTOMATIC_INITIAL,
        )

        head = self.store.scopes.append_and_set_head(
            revision=revision,
            expected_current_revision_id=None,
        )

        self.assertEqual(revision.scope_revision_id, head.scope_revision_id)
        self.assertEqual(head, self.store.scopes.get_head("chat-a"))
        self.assertEqual(
            ("b.pdf", "a.pdf"),
            tuple(
                item.file_name
                for item in self.store.scopes.get_current_revision(
                    "chat-a"
                ).members
            ),
        )

    def test_scope_head_compare_and_set_keeps_append_only_history(self) -> None:
        first_run = self._create_run(chat_id="chat-a", run_id="run-a")
        first = self._revision(
            chat_id="chat-a",
            run_id=first_run.run_id,
            file_names=("a.pdf",),
        )
        self.store.scopes.append_and_set_head(
            revision=first,
            expected_current_revision_id=None,
        )
        self.locks.issue_execution_lease(run_id=first_run.run_id)
        self.locks.complete_run(first_run.run_id)
        second_run = self._create_run(chat_id="chat-a", run_id="run-b")
        second = self._revision(
            chat_id="chat-a",
            run_id=second_run.run_id,
            file_names=("b.pdf", "c.pdf"),
        )

        self.store.scopes.append_and_set_head(
            revision=second,
            expected_current_revision_id=first.scope_revision_id,
        )

        self.assertEqual(
            second.scope_revision_id,
            self.store.scopes.get_head("chat-a").scope_revision_id,
        )
        self.assertEqual(
            (first.scope_revision_id, second.scope_revision_id),
            tuple(
                item.scope_revision_id
                for item in self.store.scopes.list_revisions_by_chat("chat-a")
            ),
        )

    def test_scope_head_cas_conflict_rolls_back_new_revision(self) -> None:
        first_run = self._create_run(chat_id="chat-a", run_id="run-a")
        first = self._revision(
            chat_id="chat-a",
            run_id=first_run.run_id,
            file_names=("a.pdf",),
        )
        self.store.scopes.append_and_set_head(
            revision=first,
            expected_current_revision_id=None,
        )
        self.locks.issue_execution_lease(run_id=first_run.run_id)
        self.locks.complete_run(first_run.run_id)
        second_run = self._create_run(chat_id="chat-a", run_id="run-b")
        second = self._revision(
            chat_id="chat-a",
            run_id=second_run.run_id,
            file_names=("b.pdf",),
        )

        with self.assertRaisesRegex(ValueError, "compare-and-set conflict"):
            self.store.scopes.append_and_set_head(
                revision=second,
                expected_current_revision_id="stale-scope",
            )

        self.assertIsNone(
            self.store.scopes.get_revision(second.scope_revision_id)
        )
        self.assertEqual(
            first.scope_revision_id,
            self.store.scopes.get_head("chat-a").scope_revision_id,
        )

    def test_empty_scope_revision_is_persisted_as_real_head(self) -> None:
        run = self._create_run(chat_id="chat-empty", run_id="run-empty")
        revision = self._revision(
            chat_id="chat-empty",
            run_id=run.run_id,
            file_names=(),
            source_mode=CHAT_SCOPE_SOURCE_AUTOMATIC_INITIAL,
        )

        self.store.scopes.append_and_set_head(
            revision=revision,
            expected_current_revision_id=None,
        )

        current = self.store.scopes.get_current_revision("chat-empty")
        self.assertIsNotNone(current)
        self.assertEqual((), current.members)

    def test_revision_source_run_must_belong_to_same_chat(self) -> None:
        self._create_run(chat_id="chat-a", run_id="run-a")
        self._create_run(chat_id="chat-b", run_id="run-b")
        revision = ChatScopeRevision(
            scope_revision_id="bad-scope",
            chat_id="chat-a",
            source_mode=CHAT_SCOPE_SOURCE_EXPLICIT,
            source_run_id="run-b",
            members=(_member("a.pdf"),),
            created_at=_NOW,
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.scopes.append_and_set_head(
                revision=revision,
                expected_current_revision_id=None,
            )

        self.assertIsNone(self.store.scopes.get_revision("bad-scope"))

    def test_revision_and_members_are_update_immutable(self) -> None:
        run = self._create_run(chat_id="chat-a", run_id="run-a")
        revision = self._revision(
            chat_id="chat-a",
            run_id=run.run_id,
            file_names=("a.pdf",),
        )
        self.store.scopes.append_and_set_head(
            revision=revision,
            expected_current_revision_id=None,
        )

        with closing(self._connect()) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE chat_scope_revisions
                    SET source_mode = 'automatic_initial'
                    WHERE scope_revision_id = ?
                    """,
                    (revision.scope_revision_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE chat_scope_members
                    SET original_name = 'changed.pdf'
                    WHERE scope_revision_id = ?
                    """,
                    (revision.scope_revision_id,),
                )

    def test_run_input_loads_effective_scope_and_requested_files_separately(
        self,
    ) -> None:
        run = self._create_run(chat_id="chat-a", run_id="run-a")
        revision = self._revision(
            chat_id="chat-a",
            run_id=run.run_id,
            file_names=("a.pdf", "b.pdf"),
        )
        self.store.scopes.append_and_set_head(
            revision=revision,
            expected_current_revision_id=None,
        )
        requested_json = json.dumps(
            [
                {
                    "file_name": "a.pdf",
                    "original_name": "a.pdf.original",
                }
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO chat_run_inputs (
                    run_id, message, files_json, created_at,
                    requested_files_json, effective_scope_revision_id,
                    selection_mode
                ) VALUES (?, ?, '[]', ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    "问题",
                    _NOW,
                    requested_json,
                    revision.scope_revision_id,
                    CHAT_SCOPE_SELECTION_EXPLICIT,
                ),
            )
            connection.commit()

        run_input = self.store.run_inputs.get(run.run_id)

        self.assertIsNotNone(run_input)
        self.assertEqual(
            ("a.pdf", "b.pdf"),
            tuple(item.file_name for item in run_input.files),
        )
        self.assertEqual(
            ("a.pdf",),
            tuple(item.file_name for item in run_input.requested_files),
        )
        self.assertEqual(
            revision.scope_revision_id,
            run_input.effective_scope_revision_id,
        )
        self.assertEqual(
            CHAT_SCOPE_SELECTION_EXPLICIT,
            run_input.selection_mode,
        )
        with closing(self._connect()) as connection:
            raw_files_json = connection.execute(
                """
                SELECT files_json FROM chat_run_inputs WHERE run_id = ?
                """,
                (run.run_id,),
            ).fetchone()["files_json"]
        self.assertEqual("[]", raw_files_json)

    def test_scope_revision_survives_source_run_pruning(self) -> None:
        run = self._create_run(chat_id="chat-a", run_id="run-a")
        revision = self._revision(
            chat_id="chat-a",
            run_id=run.run_id,
            file_names=("a.pdf",),
        )
        self.store.scopes.append_and_set_head(
            revision=revision,
            expected_current_revision_id=None,
        )

        with closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM chat_runs WHERE run_id = ?",
                (run.run_id,),
            )
            connection.commit()

        self.assertIsNotNone(
            self.store.scopes.get_revision(revision.scope_revision_id)
        )
        self.assertEqual(
            revision.scope_revision_id,
            self.store.scopes.get_head("chat-a").scope_revision_id,
        )

    def test_fifty_distinct_chats_keep_scope_facts_isolated(self) -> None:
        count = 50
        barrier = Barrier(count)

        def create_scope(index: int) -> tuple[str, str]:
            chat_id = f"chat-{index}"
            run_id = f"run-{index}"
            barrier.wait(timeout=10)
            self.locks.try_acquire_chat_run(
                chat_id=chat_id,
                run_id=run_id,
            )
            revision = self._revision(
                chat_id=chat_id,
                run_id=run_id,
                file_names=(f"{index}.pdf",),
            )
            self.store.scopes.append_and_set_head(
                revision=revision,
                expected_current_revision_id=None,
            )
            return chat_id, revision.scope_revision_id

        with ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(create_scope, range(count)))

        self.assertEqual(count, len(set(results)))
        for index, (chat_id, revision_id) in enumerate(results):
            head = self.store.scopes.get_head(chat_id)
            current = self.store.scopes.get_current_revision(chat_id)
            self.assertEqual(revision_id, head.scope_revision_id)
            self.assertEqual(
                (f"{index}.pdf",),
                tuple(item.file_name for item in current.members),
            )


if __name__ == "__main__":
    unittest.main()
