"""Debug 文件与本地 SQLite 只读 Adapter 测试。"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.modules.debug.adapters import (
    FileCallbackHistoryReadAdapter,
    LocalChatDebugSnapshotReadAdapter,
)
from app.services.chat import ChatRunLockService, ChatStore
from app.services.chat.domain import ChatDocumentCandidate, ChatDocumentSelectionCandidates
from app.services.core.database import DatabaseService


class DebugAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tempdir.__enter__())

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def test_callback_adapter_orders_limits_and_rejects_unsafe_names(self) -> None:
        history = self.root / "callback"
        history.mkdir()
        for name, mtime in (("old.json", 1000), ("latest.json", 2000)):
            path = history / name
            path.write_text("{}", encoding="utf-8")
            os.utime(path, (mtime, mtime))
        (history / "ignored.txt").write_text("{}", encoding="utf-8")

        adapter = FileCallbackHistoryReadAdapter(history)
        self.assertEqual(
            ["latest.json", "old.json"],
            [item.record_id for item in adapter.list_records(limit=50)],
        )
        for unsafe in ("../latest.json", "nested/latest.json", "latest.txt"):
            with self.subTest(unsafe=unsafe):
                self.assertIsNone(adapter.find_record(unsafe))

    def test_callback_adapter_reports_encoding_and_race_delete_failures(self) -> None:
        history = self.root / "callback"
        history.mkdir()
        invalid = history / "invalid.json"
        invalid.write_bytes(b"\xff\xfe")
        adapter = FileCallbackHistoryReadAdapter(history)

        self.assertEqual("encoding", adapter.read_record("invalid.json").error_kind)
        invalid.unlink()
        self.assertEqual("io", adapter.read_record("invalid.json").error_kind)

    def test_callback_adapter_revalidates_read_name_and_rejects_symlink(self) -> None:
        """读取入口本身必须拒绝穿越；不得依赖调用者先执行 find_record。"""

        history = self.root / "callback"
        history.mkdir()
        outside = self.root / "outside.json"
        outside.write_text('{"secret": true}', encoding="utf-8")
        adapter = FileCallbackHistoryReadAdapter(history)

        self.assertEqual("io", adapter.read_record("../outside.json").error_kind)

        link = history / "linked.json"
        try:
            link.symlink_to(outside)
        except OSError:
            # Windows 未启用开发者模式时可能无权创建符号链接；路径穿越断言仍始终执行，
            # 符号链接分支则由下面的确定性 mock 测试继续覆盖。
            link.write_text("{}", encoding="utf-8")
            original_is_symlink = Path.is_symlink

            def controlled_is_symlink(path: Path) -> bool:
                return path == link or original_is_symlink(path)

            with patch.object(
                Path,
                "is_symlink",
                autospec=True,
                side_effect=controlled_is_symlink,
            ):
                self.assertNotIn(
                    "linked.json",
                    {item.record_id for item in adapter.list_records(limit=50)},
                )
                self.assertIsNone(adapter.find_record("linked.json"))
                self.assertEqual("io", adapter.read_record("linked.json").error_kind)
        else:
            self.assertNotIn(
                "linked.json",
                {item.record_id for item in adapter.list_records(limit=50)},
            )
            self.assertIsNone(adapter.find_record("linked.json"))
            self.assertEqual("io", adapter.read_record("linked.json").error_kind)

    def test_callback_adapter_skips_stat_failure_without_aborting_list(self) -> None:
        history = self.root / "callback"
        history.mkdir()
        good = history / "good.json"
        broken = history / "broken.json"
        good.write_text("{}", encoding="utf-8")
        broken.write_text("{}", encoding="utf-8")
        original_stat = Path.stat

        def controlled_stat(path: Path, *args, **kwargs):
            if path.name == "broken.json":
                raise OSError("simulated stat failure")
            return original_stat(path, *args, **kwargs)

        with patch("pathlib.Path.stat", autospec=True, side_effect=controlled_stat):
            records = FileCallbackHistoryReadAdapter(history).list_records(limit=50)

        self.assertEqual(["good.json"], [item.record_id for item in records])

    def test_chat_adapter_projects_active_scope_and_binding_count(self) -> None:
        chat_path = str(self.root / "chat.sqlite3")
        chat_store = ChatStore(db_path=chat_path)
        kb_service = DatabaseService(db_path=str(self.root / "knowledge.sqlite3"))
        ChatRunLockService(chat_path).try_acquire_chat_run(
            chat_id="10001",
            run_id="debug-adapter-run",
            user_message="question",
            document_candidates=ChatDocumentSelectionCandidates(
                explicit_documents=(
                    ChatDocumentCandidate(
                        file_name="alpha.pdf",
                        original_name="Alpha.pdf",
                        document_ref="document:alpha",
                        external_location="custom-documents/alpha.json",
                    ),
                )
            ),
            max_files_per_request=5,
        )
        chat_store.document_bindings.add(
            chat_id="10001",
            file_name="alpha.pdf",
            original_name="Alpha.pdf",
            document_ref="document:alpha",
            external_location="custom-documents/alpha.json",
        )
        kb_service.save_document_record(
            "alpha.pdf",
            7,
            "alpha",
            "custom-documents/alpha.json",
            ingested_file_name="alpha.pdf",
        )

        snapshot = LocalChatDebugSnapshotReadAdapter(
            chat_store=chat_store,
            kb_service=kb_service,
        ).read_snapshot()

        self.assertEqual(10001, snapshot.sessions[0].chat_id)
        self.assertEqual(("alpha.pdf",), snapshot.sessions[0].file_names)
        self.assertEqual(1, snapshot.active_scope_member_count)
        self.assertEqual(1, snapshot.workspace_binding_count)
        self.assertEqual("alpha.pdf", snapshot.available_files[0].file_name)

    def test_chat_adapter_hides_deleted_sessions(self) -> None:
        """删除终态属于持久化事实，Debug Adapter 不得把该会话重新投影。"""

        chat_path = str(self.root / "chat-deleted.sqlite3")
        chat_store = ChatStore(db_path=chat_path)
        chat_store.sessions.create_or_get(chat_id="10002")
        chat_store.sessions.set_status(chat_id="10002", status="deleting")
        chat_store.sessions.set_status(chat_id="10002", status="deleted")

        snapshot = LocalChatDebugSnapshotReadAdapter(
            chat_store=chat_store,
            kb_service=DatabaseService(
                db_path=str(self.root / "knowledge-deleted.sqlite3")
            ),
        ).read_snapshot()

        self.assertEqual((), snapshot.sessions)

    def test_chat_adapter_hides_noncanonical_legacy_chat_id(self) -> None:
        """非公开整数 chatId 只能留在本地存量数据中，不能泄露给 Debug 页面。"""

        chat_path = str(self.root / "chat-legacy-id.sqlite3")
        chat_store = ChatStore(db_path=chat_path)
        chat_store.sessions.create_or_get(chat_id="legacy-chat")

        snapshot = LocalChatDebugSnapshotReadAdapter(
            chat_store=chat_store,
            kb_service=DatabaseService(
                db_path=str(self.root / "knowledge-legacy-id.sqlite3")
            ),
        ).read_snapshot()

        self.assertEqual((), snapshot.sessions)


if __name__ == "__main__":
    unittest.main()
