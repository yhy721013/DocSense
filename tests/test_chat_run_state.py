"""阶段 4 文件对话运行锁的离线测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
import sqlite3
from threading import Barrier
from unittest.mock import patch

from app.services.chat import (
    RUN_FAILED,
    RUN_ACCEPTED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    ChatRunBusyError,
    ChatDocumentCandidate,
    ChatDocumentSelectionCandidates,
    ChatRunInactiveError,
    ChatRunLockService,
    ChatStore,
    ChatHistoryService,
    MESSAGE_COMMITTED,
    MESSAGE_DISCARDED,
)


def _document_candidate(file_name: str) -> ChatDocumentCandidate:
    """构造不依赖知识库或供应商网络的受理候选。"""
    return ChatDocumentCandidate(
        file_name=file_name,
        original_name=f"{file_name}.original",
        document_ref=f"document:{file_name}",
        external_location=f"custom-documents/{file_name}.json",
    )


class ChatRunLockServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.db_path = f"{self.tmp}/chat.sqlite3"
        self.store = ChatStore(self.db_path)
        self.locks = ChatRunLockService(
            self.db_path,
            owner_instance_id="test-instance",
        )

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def test_same_chat_is_exclusive_until_run_completes(self) -> None:
        first = self.locks.try_acquire_chat_run(
            chat_id="chat-a",
            run_id="run-a",
        )

        with self.assertRaises(ChatRunBusyError) as error:
            self.locks.try_acquire_chat_run(
                chat_id="chat-a",
                run_id="run-b",
            )

        self.locks.issue_execution_lease(run_id="run-a")
        completed = self.locks.complete_run("run-a")
        second = self.locks.try_acquire_chat_run(
            chat_id="chat-a",
            run_id="run-b",
        )

        self.assertEqual(RUN_ACCEPTED, first.status)
        self.assertEqual("run-a", error.exception.active_run_id)
        self.assertEqual(RUN_SUCCEEDED, completed.status)
        self.assertEqual(RUN_ACCEPTED, second.status)

    def test_new_session_uses_default_candidates_atomically(self) -> None:
        candidates = ChatDocumentSelectionCandidates(
            new_session_default_documents=(
                _document_candidate("alpha.pdf"),
                _document_candidate("beta.pdf"),
            )
        )

        with self.assertLogs(
            "app.services.chat.locking.lock_service",
            level="INFO",
        ) as captured:
            run = self.locks.try_acquire_chat_run(
                chat_id="chat-default",
                run_id="run-default",
                user_message="请总结",
                document_candidates=candidates,
                max_files_per_request=2,
            )

        run_input = self.store.run_inputs.get(run.run_id)
        messages = self.store.messages.list_by_chat("chat-default")
        self.assertIsNotNone(run_input)
        assert run_input is not None
        self.assertEqual(
            ("alpha.pdf", "beta.pdf"),
            tuple(item.file_name for item in run_input.files),
        )
        self.assertEqual((), run_input.requested_files)
        self.assertEqual("automatic_initial", run_input.selection_mode)
        self.assertTrue(run_input.effective_scope_revision_id)
        self.assertEqual((), messages[0].files)
        current_scope = self.store.scopes.get_current_revision("chat-default")
        self.assertIsNotNone(current_scope)
        assert current_scope is not None
        self.assertEqual(
            run_input.effective_scope_revision_id,
            current_scope.scope_revision_id,
        )
        selection_log = next(
            message
            for message in captured.output
            if "受理事务已选择有效文档" in message
        )
        self.assertIn("run_id=run-default", selection_log)
        self.assertIn("selection_mode=automatic_initial", selection_log)
        self.assertIn("session_created=True", selection_log)
        self.assertIn("explicit_candidate_count=0", selection_log)
        self.assertIn("default_candidate_count=2", selection_log)
        self.assertIn("effective_file_count=2", selection_log)
        self.assertNotIn("alpha.pdf", selection_log)
        self.assertNotIn("document:alpha.pdf", selection_log)

    def test_existing_session_without_scope_fails_closed(self) -> None:
        """迁移后异常缺失 Head 的既有会话不得静默退化为空范围。"""

        self.store.sessions.create_or_get(chat_id="chat-existing")
        candidates = ChatDocumentSelectionCandidates(
            new_session_default_documents=(
                _document_candidate("default.pdf"),
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            "^existing chat session is missing active scope$",
        ):
            self.locks.try_acquire_chat_run(
                chat_id="chat-existing",
                run_id="run-existing",
                user_message="继续",
                document_candidates=candidates,
                max_files_per_request=1,
            )

        self.assertIsNone(self.store.runs.get("run-existing"))
        self.assertIsNone(self.store.run_inputs.get("run-existing"))
        self.assertEqual((), self.store.messages.list_by_chat("chat-existing"))

    def test_new_session_explicit_selection_has_distinct_safe_log(self) -> None:
        """显式选择应使用独立模式，并且日志只记录计数而不泄漏文件身份。"""

        candidates = ChatDocumentSelectionCandidates(
            explicit_documents=(
                _document_candidate("explicit.pdf"),
            )
        )

        with self.assertLogs(
            "app.services.chat.locking.lock_service",
            level="INFO",
        ) as captured:
            self.locks.try_acquire_chat_run(
                chat_id="chat-explicit",
                run_id="run-explicit",
                user_message="请总结",
                document_candidates=candidates,
                max_files_per_request=1,
            )

        selection_log = next(
            message
            for message in captured.output
            if "受理事务已选择有效文档" in message
        )
        self.assertIn("run_id=run-explicit", selection_log)
        self.assertIn("selection_mode=explicit", selection_log)
        self.assertIn("session_created=True", selection_log)
        self.assertIn("explicit_candidate_count=1", selection_log)
        self.assertIn("default_candidate_count=0", selection_log)
        self.assertIn("effective_file_count=1", selection_log)
        self.assertNotIn("explicit.pdf", selection_log)
        self.assertNotIn("document:explicit.pdf", selection_log)

    def test_effective_file_limit_rolls_back_first_session_and_run(self) -> None:
        candidates = ChatDocumentSelectionCandidates(
            new_session_default_documents=(
                _document_candidate("alpha.pdf"),
                _document_candidate("beta.pdf"),
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            "^fileNames超过文件对话数量上限$",
        ):
            self.locks.try_acquire_chat_run(
                chat_id="chat-over-limit",
                run_id="run-over-limit",
                user_message="请总结",
                document_candidates=candidates,
                max_files_per_request=1,
            )

        self.assertIsNone(self.store.sessions.get("chat-over-limit"))
        self.assertIsNone(self.store.runs.get("run-over-limit"))
        self.assertIsNone(self.store.run_inputs.get("run-over-limit"))
        self.assertEqual(
            (),
            self.store.messages.list_by_chat("chat-over-limit"),
        )

    def test_pending_user_failure_rolls_back_session_run_and_input(self) -> None:
        """写入待处理消息失败时，应回滚同一事务内创建的全部会话与运行事实。"""

        candidates = ChatDocumentSelectionCandidates(
            new_session_default_documents=(
                _document_candidate("alpha.pdf"),
            )
        )

        with patch.object(
            self.locks,
            "_append_user_pending",
            side_effect=RuntimeError("injected pending message failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "^injected pending message failure$",
            ):
                self.locks.try_acquire_chat_run(
                    chat_id="chat-write-failure",
                    run_id="run-write-failure",
                    user_message="请总结",
                    document_candidates=candidates,
                    max_files_per_request=5,
                )

        self.assertIsNone(self.store.sessions.get("chat-write-failure"))
        self.assertIsNone(self.store.runs.get("run-write-failure"))
        self.assertIsNone(self.store.run_inputs.get("run-write-failure"))
        self.assertIsNone(
            self.store.scopes.get_current_revision("chat-write-failure")
        )
        self.assertEqual(
            (),
            self.store.messages.list_by_chat("chat-write-failure"),
        )

    def test_fifty_concurrent_first_admissions_accept_only_one_run(self) -> None:
        worker_count = 50
        barrier = Barrier(worker_count)
        candidates = ChatDocumentSelectionCandidates(
            new_session_default_documents=(
                _document_candidate("all.pdf"),
            )
        )

        def attempt(index: int) -> str:
            barrier.wait()
            try:
                self.locks.try_acquire_chat_run(
                    chat_id="chat-concurrent-default",
                    run_id=f"run-concurrent-{index}",
                    user_message=f"question-{index}",
                    document_candidates=candidates,
                    max_files_per_request=5,
                )
                return "accepted"
            except ChatRunBusyError:
                return "busy"

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            outcomes = list(executor.map(attempt, range(worker_count)))

        self.assertEqual(1, outcomes.count("accepted"))
        self.assertEqual(worker_count - 1, outcomes.count("busy"))
        active_runs = self.store.runs.list_active(
            "chat-concurrent-default"
        )
        self.assertEqual(1, len(active_runs))
        run_input = self.store.run_inputs.get(active_runs[0].run_id)
        self.assertIsNotNone(run_input)
        assert run_input is not None
        self.assertEqual(
            ("all.pdf",),
            tuple(item.file_name for item in run_input.files),
        )
        revisions = self.store.scopes.list_revisions_by_chat(
            "chat-concurrent-default"
        )
        head = self.store.scopes.get_head("chat-concurrent-default")
        self.assertEqual(1, len(revisions))
        self.assertIsNotNone(head)
        assert head is not None
        self.assertEqual(revisions[0].scope_revision_id, head.scope_revision_id)
        self.assertEqual(
            active_runs[0].run_id,
            revisions[0].source_run_id,
        )

    def test_fifty_concurrent_explicit_updates_create_one_new_head(self) -> None:
        """同一会话并发替换范围时，只允许 accepted 运行推进一次 Head。"""

        initial = self.locks.try_acquire_chat_run(
            chat_id="chat-concurrent-explicit",
            run_id="run-initial",
            user_message="initial",
            document_candidates=ChatDocumentSelectionCandidates(
                new_session_default_documents=(
                    _document_candidate("base.pdf"),
                )
            ),
            max_files_per_request=5,
        )
        self.locks.issue_execution_lease(run_id=initial.run_id)
        self.locks.complete_run(initial.run_id)

        worker_count = 50
        barrier = Barrier(worker_count)

        def attempt(index: int) -> tuple[str, str]:
            run_id = f"run-explicit-{index}"
            barrier.wait()
            try:
                self.locks.try_acquire_chat_run(
                    chat_id="chat-concurrent-explicit",
                    run_id=run_id,
                    user_message=f"question-{index}",
                    document_candidates=ChatDocumentSelectionCandidates(
                        explicit_documents=(
                            _document_candidate(f"selected-{index}.pdf"),
                        )
                    ),
                    max_files_per_request=5,
                )
                return ("accepted", run_id)
            except ChatRunBusyError:
                return ("busy", run_id)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            outcomes = list(executor.map(attempt, range(worker_count)))

        accepted_run_ids = tuple(
            run_id for status, run_id in outcomes if status == "accepted"
        )
        self.assertEqual(1, len(accepted_run_ids))
        self.assertEqual(worker_count - 1, sum(
            status == "busy" for status, _ in outcomes
        ))
        revisions = self.store.scopes.list_revisions_by_chat(
            "chat-concurrent-explicit"
        )
        head = self.store.scopes.get_head("chat-concurrent-explicit")
        self.assertEqual(2, len(revisions))
        self.assertIsNotNone(head)
        assert head is not None
        self.assertEqual(revisions[-1].scope_revision_id, head.scope_revision_id)
        self.assertEqual(accepted_run_ids[0], revisions[-1].source_run_id)
        accepted_input = self.store.run_inputs.get(accepted_run_ids[0])
        self.assertIsNotNone(accepted_input)
        assert accepted_input is not None
        self.assertEqual("explicit", accepted_input.selection_mode)
        self.assertEqual(
            tuple(item.file_name for item in accepted_input.requested_files),
            tuple(item.file_name for item in accepted_input.files),
        )

    def test_explicit_over_limit_keeps_previous_scope_head(self) -> None:
        """既有会话显式范围超限时，Head、Revision 和运行事实必须全量回滚。"""

        initial = self.locks.try_acquire_chat_run(
            chat_id="chat-explicit-over-limit",
            run_id="run-initial",
            user_message="initial",
            document_candidates=ChatDocumentSelectionCandidates(
                new_session_default_documents=(
                    _document_candidate("base.pdf"),
                )
            ),
            max_files_per_request=2,
        )
        self.locks.issue_execution_lease(run_id=initial.run_id)
        self.locks.complete_run(initial.run_id)
        previous_head = self.store.scopes.get_head(
            "chat-explicit-over-limit"
        )
        self.assertIsNotNone(previous_head)

        with self.assertRaisesRegex(
            ValueError,
            "^fileNames超过文件对话数量上限$",
        ):
            self.locks.try_acquire_chat_run(
                chat_id="chat-explicit-over-limit",
                run_id="run-over-limit",
                user_message="replace",
                document_candidates=ChatDocumentSelectionCandidates(
                    explicit_documents=(
                        _document_candidate("alpha.pdf"),
                        _document_candidate("beta.pdf"),
                    )
                ),
                max_files_per_request=1,
            )

        self.assertEqual(
            previous_head,
            self.store.scopes.get_head("chat-explicit-over-limit"),
        )
        self.assertEqual(
            1,
            len(self.store.scopes.list_revisions_by_chat(
                "chat-explicit-over-limit"
            )),
        )
        self.assertIsNone(self.store.runs.get("run-over-limit"))
        self.assertIsNone(self.store.run_inputs.get("run-over-limit"))

    def test_thousand_effective_files_do_not_expand_empty_requested_history(
        self,
    ) -> None:
        """历史大小只由 Requested Scope 决定，不随 Effective Scope 线性膨胀。"""

        candidates = ChatDocumentSelectionCandidates(
            new_session_default_documents=tuple(
                _document_candidate(f"document-{index:04d}.pdf")
                for index in range(1000)
            )
        )
        run = self.locks.try_acquire_chat_run(
            chat_id="40001",
            run_id="run-thousand-effective",
            user_message="请总结",
            document_candidates=candidates,
            # 该值只用于隔离验证大范围的历史投影；公开路由仍使用配置上限，
            # 本测试不放宽生产 `DOCSENSE_CHAT_MAX_FILES`。
            max_files_per_request=1000,
        )
        run_input = self.store.run_inputs.get(run.run_id)
        self.assertIsNotNone(run_input)
        assert run_input is not None
        self.assertEqual(1000, len(run_input.files))
        self.assertEqual((), run_input.requested_files)
        self.store.messages.set_status(
            message_id=f"{run.run_id}:user",
            status=MESSAGE_COMMITTED,
        )

        history = ChatHistoryService(self.store).list_history("40001")

        self.assertEqual(1, len(history))
        self.assertEqual([], history[0]["files"])

    def test_failed_run_releases_chat_for_next_attempt(self) -> None:
        self.locks.try_acquire_chat_run(
            chat_id="chat-fail",
            run_id="run-fail",
        )
        self.locks.issue_execution_lease(run_id="run-fail")
        failed = self.locks.fail_run("run-fail", error_message="stream failed")
        retry = self.locks.try_acquire_chat_run(
            chat_id="chat-fail",
            run_id="run-retry",
        )

        self.assertEqual("failed", failed.status)
        self.assertEqual(RUN_ACCEPTED, retry.status)

    def test_different_chats_do_not_block_each_other(self) -> None:
        first = self.locks.try_acquire_chat_run(
            chat_id="chat-one",
            run_id="run-one",
        )
        second = self.locks.try_acquire_chat_run(
            chat_id="chat-two",
            run_id="run-two",
        )

        self.assertEqual(RUN_ACCEPTED, first.status)
        self.assertEqual(RUN_ACCEPTED, second.status)

    def test_request_abort_sets_flag_on_active_run(self) -> None:
        self.locks.try_acquire_chat_run(
            chat_id="chat-abort",
            run_id="run-abort",
        )

        aborted = self.locks.request_abort("run-abort")

        self.assertTrue(aborted.abort_requested)

    def test_discard_unstarted_run_hides_the_accepted_user_message(self) -> None:
        """已受理状态从未领取执行权时，断开连接不应产生历史轮次。"""
        run = self.locks.try_acquire_chat_run(
            chat_id="chat-discard",
            run_id="run-discard",
            user_message="尚未执行",
        )

        discarded = self.locks.discard_unstarted_run(
            run_id=run.run_id,
            error_message="response closed before execution",
        )

        message = self.store.messages.list_by_chat("chat-discard")[0]
        self.assertEqual(RUN_FAILED, discarded.status)
        self.assertEqual(MESSAGE_DISCARDED, message.status)
        self.assertEqual((), self.store.runs.list_active("chat-discard"))

    def test_heartbeat_updates_active_run(self) -> None:
        started = self.locks.try_acquire_chat_run(
            chat_id="chat-heartbeat",
            run_id="run-heartbeat",
        )
        self.locks.issue_execution_lease(run_id="run-heartbeat")
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE chat_runs
                SET heartbeat_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    "2000-01-01T00:00:00+00:00",
                    "2000-01-01T00:00:00+00:00",
                    "run-heartbeat",
                ),
            )

        touched = self.locks.heartbeat_run("run-heartbeat")

        self.assertEqual(RUN_ACCEPTED, started.status)
        self.assertEqual(RUN_RUNNING, touched.status)
        self.assertNotEqual("2000-01-01T00:00:00+00:00", touched.heartbeat_at)

    def test_stale_active_run_is_failed_before_retry(self) -> None:
        locks = ChatRunLockService(
            self.db_path,
            owner_instance_id="test-instance",
            stale_after_seconds=1,
        )
        locks.try_acquire_chat_run(
            chat_id="chat-stale",
            run_id="run-stale",
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE chat_runs
                SET heartbeat_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    "2000-01-01T00:00:00+00:00",
                    "2000-01-01T00:00:00+00:00",
                    "run-stale",
                ),
            )

        retry = locks.try_acquire_chat_run(
            chat_id="chat-stale",
            run_id="run-after-stale",
        )
        stale = self.store.runs.get("run-stale")

        self.assertEqual(RUN_ACCEPTED, retry.status)
        self.assertIsNotNone(stale)
        assert stale is not None
        self.assertEqual(RUN_FAILED, stale.status)
        self.assertEqual("chat run heartbeat expired", stale.error_message)

    def test_stale_active_run_can_be_expired_without_retry(self) -> None:
        locks = ChatRunLockService(
            self.db_path,
            owner_instance_id="test-instance",
            stale_after_seconds=1,
        )
        locks.try_acquire_chat_run(
            chat_id="chat-stale-explicit",
            run_id="run-stale-explicit",
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE chat_runs
                SET heartbeat_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    "2000-01-01T00:00:00+00:00",
                    "2000-01-01T00:00:00+00:00",
                    "run-stale-explicit",
                ),
            )

        expired = locks.expire_stale_runs_for_chat(chat_id="chat-stale-explicit")
        active = self.store.runs.list_active("chat-stale-explicit")

        self.assertEqual(["run-stale-explicit"], [run.run_id for run in expired])
        self.assertEqual(RUN_FAILED, expired[0].status)
        self.assertEqual("chat run heartbeat expired", expired[0].error_message)
        self.assertEqual((), active)

    def test_terminal_run_rejects_illegal_follow_up_state_changes(self) -> None:
        self.locks.try_acquire_chat_run(
            chat_id="chat-terminal",
            run_id="run-terminal",
        )
        self.locks.issue_execution_lease(run_id="run-terminal")
        completed = self.locks.complete_run("run-terminal")

        with self.assertRaises(ValueError):
            self.locks.fail_run("run-terminal", error_message="late failure")
        with self.assertRaises(ChatRunInactiveError):
            self.locks.request_abort("run-terminal")

        self.assertEqual(RUN_SUCCEEDED, completed.status)


if __name__ == "__main__":
    unittest.main()
