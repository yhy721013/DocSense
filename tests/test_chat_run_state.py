"""阶段 4 文件对话运行锁的离线测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
import sqlite3
import zlib
from threading import Barrier
from unittest.mock import patch

from app.modules.chat.domain import (
    CHAT_ARCHITECTURE_CANDIDATE_INVALID,
    CHAT_ARCHITECTURE_CANDIDATE_NOT_FOUND,
    CHAT_ARCHITECTURE_CANDIDATE_RESOLVED,
    CHAT_ARCHITECTURE_ERROR_INVALID,
    CHAT_ARCHITECTURE_ERROR_NOT_FOUND,
)
from app.modules.chat import (
    RUN_FAILED,
    RUN_ACCEPTED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    ChatRunBusyError,
    ChatAdmissionBusyError,
    ChatDocumentCandidate,
    ChatArchitectureCandidates,
    ChatArchitectureIdConflictError,
    ChatArchitectureScopeInvalidError,
    ChatArchitectureScopeNotFoundError,
    ChatDocumentSelectionCandidates,
    ChatRunInactiveError,
    ChatRunLockService,
    ChatStore,
    ChatHistoryService,
    ChatScopeModeConflictError,
    ChatScopeSelector,
    MESSAGE_COMMITTED,
    MESSAGE_DISCARDED,
)
from app.modules.chat.domain.identity import FileChatIdentity, WeaponryChatIdentity


def _identity(value: str | int) -> FileChatIdentity:
    """把测试标签稳定映射为合法文件对话身份。"""
    if isinstance(value, int) or str(value).isdigit():
        return FileChatIdentity(chat_id=int(value))
    return FileChatIdentity(chat_id=zlib.crc32(str(value).encode("utf-8")) + 1)


def _weaponry_identity(value: str | int, architecture_id: int) -> WeaponryChatIdentity:
    """用独立 DocSense userId 构造知识谱系复合身份。"""
    return WeaponryChatIdentity(
        user_id=_identity(value).chat_id,
        architecture_id=architecture_id,
    )


def _document_candidate(file_name: str) -> ChatDocumentCandidate:
    """构造不依赖知识库或供应商网络的受理候选。"""
    return ChatDocumentCandidate(
        file_name=file_name,
        original_name=f"{file_name}.original",
        document_ref=f"document:{file_name}",
        external_location=f"custom-documents/{file_name}.json",
    )


def _architecture_candidates(
    architecture_id: int,
    *file_names: str,
) -> ChatDocumentSelectionCandidates:
    """构造无需访问知识目录或远端服务的 architecture 候选。"""
    return ChatDocumentSelectionCandidates(
        architecture_candidates=ChatArchitectureCandidates(
            architecture_id=architecture_id,
            resolution_outcome=CHAT_ARCHITECTURE_CANDIDATE_RESOLVED,
            documents=tuple(_document_candidate(name) for name in file_names),
        )
    )


def _failed_architecture_candidates(
    architecture_id: int,
    *,
    invalid: bool = False,
) -> ChatDocumentSelectionCandidates:
    """构造交由受理事务延迟裁决的空目录或损坏目录结果。"""
    return ChatDocumentSelectionCandidates(
        architecture_candidates=ChatArchitectureCandidates(
            architecture_id=architecture_id,
            resolution_outcome=(
                CHAT_ARCHITECTURE_CANDIDATE_INVALID
                if invalid
                else CHAT_ARCHITECTURE_CANDIDATE_NOT_FOUND
            ),
            error_code=(
                CHAT_ARCHITECTURE_ERROR_INVALID
                if invalid
                else CHAT_ARCHITECTURE_ERROR_NOT_FOUND
            ),
        )
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

    def _conversation_id(
        self,
        identity: FileChatIdentity | WeaponryChatIdentity,
    ) -> str:
        """解析已受理身份的内部 UUID，避免测试继续把公开 ID 当主键。"""
        resolution = self.store.identities.resolve_any(identity)
        self.assertIsNotNone(resolution)
        assert resolution is not None
        return resolution.conversation_id

    def test_same_chat_is_exclusive_until_run_completes(self) -> None:
        first = self.locks.try_acquire_chat_run(
            identity=_identity("chat-a"),
            run_id="run-a",
        )

        with self.assertRaises(ChatRunBusyError) as error:
            self.locks.try_acquire_chat_run(
                identity=_identity("chat-a"),
                run_id="run-b",
            )

        self.locks.issue_execution_lease(run_id="run-a")
        completed = self.locks.complete_run("run-a")
        second = self.locks.try_acquire_chat_run(
            identity=_identity("chat-a"),
            run_id="run-b",
        )

        self.assertEqual(RUN_ACCEPTED, first.status)
        self.assertEqual("run-a", error.exception.active_run_id)
        self.assertEqual(RUN_SUCCEEDED, completed.status)
        self.assertEqual(RUN_ACCEPTED, second.status)

    def test_admission_guard_is_exclusive_without_creating_business_facts(
        self,
    ) -> None:
        """Guard 竞争只影响临时协调表，不得提前创建 Session、Scope 或 run。"""
        selector = ChatScopeSelector.for_files(())
        lease = self.locks.reserve_chat_admission(
            identity=_identity("chat-admission"),
            scope_selector=selector,
        )

        with self.assertRaises(ChatAdmissionBusyError):
            self.locks.reserve_chat_admission(
                identity=_identity("chat-admission"),
                scope_selector=selector,
            )

        self.assertIsNone(
            self.store.identities.resolve_any(_identity("chat-admission"))
        )
        self.locks.release_chat_admission(lease=lease)

        retry = self.locks.reserve_chat_admission(
            identity=_identity("chat-admission"),
            scope_selector=selector,
        )
        self.locks.release_chat_admission(lease=retry)

    def test_admission_guard_is_consumed_by_atomic_run_acceptance(self) -> None:
        """正式受理必须在同一事务中消费 Guard，且之后由活动 run 继续互斥。"""
        selector = ChatScopeSelector.for_architecture(7)
        identity = _weaponry_identity("chat-admission-accept", 7)
        lease = self.locks.reserve_chat_admission(
            identity=identity,
            scope_selector=selector,
        )
        run = self.locks.try_acquire_chat_run(
            identity=identity,
            run_id="run-admission-accept",
            user_message="问题",
            document_candidates=_architecture_candidates(7, "alpha.pdf"),
            scope_selector=selector,
            admission_lease=lease,
        )

        self.assertEqual(RUN_ACCEPTED, run.status)
        with self.assertRaises(ChatRunBusyError):
            self.locks.reserve_chat_admission(
                identity=identity,
                scope_selector=selector,
            )
        # 成功受理后的幂等释放不能影响已经提交的运行事实。
        self.locks.release_chat_admission(lease=lease)
        self.assertIsNotNone(self.store.runs.get("run-admission-accept"))

    def test_expired_admission_guard_is_recovered_without_business_drift(
        self,
    ) -> None:
        """请求崩溃遗留的过期 Guard 可回收，且不会伪造 Session 或运行事实。"""
        selector = ChatScopeSelector.for_files(())
        self.locks.reserve_chat_admission(
            identity=_identity("chat-admission-expired"),
            scope_selector=selector,
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE conversation_admissions
                SET expires_at = '2000-01-01T00:00:00+00:00'
                WHERE identity_key = ?
                """
                ,
                (_identity("chat-admission-expired").identity_key,),
            )

        recovered = self.locks.reserve_chat_admission(
            identity=_identity("chat-admission-expired"),
            scope_selector=selector,
        )

        self.assertIsNone(
            self.store.identities.resolve_any(
                _identity("chat-admission-expired")
            )
        )
        self.locks.release_chat_admission(lease=recovered)

    def test_new_session_uses_default_candidates_atomically(self) -> None:
        candidates = ChatDocumentSelectionCandidates(
            new_session_default_documents=(
                _document_candidate("alpha.pdf"),
                _document_candidate("beta.pdf"),
            )
        )

        with self.assertLogs(
            "app.modules.chat.adapters.sqlite.locking.lock_service",
            level="INFO",
        ) as captured:
            run = self.locks.try_acquire_chat_run(
                identity=_identity("chat-default"),
                run_id="run-default",
                user_message="请总结",
                document_candidates=candidates,
                max_files_per_request=2,
            )

        run_input = self.store.run_inputs.get(run.run_id)
        messages = self.store.messages.list_by_chat(run.conversation_id)
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
        current_scope = self.store.scopes.get_current_revision(
            run.conversation_id
        )
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

    def test_architecture_initial_admission_persists_one_frozen_scope(self) -> None:
        """首轮必须原子提交 Binding、Revision、run input 与消息选择器。"""
        run = self.locks.try_acquire_chat_run(
            identity=_weaponry_identity("chat-architecture-initial", 7),
            run_id="run-architecture-initial",
            user_message="请总结类别",
            document_candidates=_architecture_candidates(
                7,
                "alpha.pdf",
                "beta.pdf",
            ),
            scope_selector=ChatScopeSelector.for_architecture(7),
            max_files_per_request=2,
        )

        binding = self.store.session_scope_bindings.get(run.conversation_id)
        run_input = self.store.run_inputs.get(run.run_id)
        revision = self.store.scopes.get_current_revision(run.conversation_id)
        messages = self.store.messages.list_by_chat(run.conversation_id)
        self.assertIsNotNone(binding)
        self.assertIsNotNone(run_input)
        self.assertIsNotNone(revision)
        assert binding is not None
        assert run_input is not None
        assert revision is not None
        self.assertEqual("architecture", binding.scope_mode)
        self.assertEqual(7, binding.architecture_id)
        self.assertEqual("architecture_initial", run_input.selection_mode)
        self.assertEqual(7, run_input.requested_architecture_id)
        self.assertEqual(
            ("alpha.pdf", "beta.pdf"),
            tuple(item.file_name for item in run_input.files),
        )
        self.assertEqual((), run_input.requested_files)
        self.assertEqual("architecture_initial", revision.source_mode)
        self.assertEqual(7, revision.source_architecture_id)
        self.assertEqual(7, messages[0].architecture_id)
        self.assertEqual((), messages[0].files)

    def test_architecture_reuse_ignores_current_not_found_candidates(self) -> None:
        """已有同 ID 会话只复用旧快照，不受当前目录空结果影响。"""
        identity = _weaponry_identity("chat-architecture-reuse", 11)
        initial = self.locks.try_acquire_chat_run(
            identity=identity,
            run_id="run-architecture-first",
            user_message="首轮",
            document_candidates=_architecture_candidates(11, "frozen.pdf"),
            scope_selector=ChatScopeSelector.for_architecture(11),
            max_files_per_request=1,
        )
        self.locks.issue_execution_lease(run_id=initial.run_id)
        self.locks.complete_run(initial.run_id)

        reused = self.locks.try_acquire_chat_run(
            identity=identity,
            run_id="run-architecture-reuse",
            user_message="后续",
            document_candidates=_failed_architecture_candidates(11),
            scope_selector=ChatScopeSelector.for_architecture(11),
            max_files_per_request=1,
        )

        run_input = self.store.run_inputs.get(reused.run_id)
        self.assertIsNotNone(run_input)
        assert run_input is not None
        self.assertEqual("architecture_reuse", run_input.selection_mode)
        self.assertEqual(11, run_input.requested_architecture_id)
        self.assertEqual(
            ("frozen.pdf",),
            tuple(item.file_name for item in run_input.files),
        )
        self.assertEqual(
            1,
            len(self.store.scopes.list_revisions_by_chat(initial.conversation_id)),
        )

    def test_architecture_conflicts_are_reported_before_active_run(self) -> None:
        """模式和 ID 冲突优先于同 chat 的 active run 冲突。"""
        identity = _weaponry_identity("chat-architecture-conflict", 21)
        self.locks.try_acquire_chat_run(
            identity=identity,
            run_id="run-architecture-active",
            user_message="首轮",
            document_candidates=_architecture_candidates(21, "frozen.pdf"),
            scope_selector=ChatScopeSelector.for_architecture(21),
            max_files_per_request=1,
        )

        with self.assertRaises(ChatArchitectureIdConflictError):
            self.locks.try_acquire_chat_run(
                identity=identity,
                run_id="run-wrong-id",
                user_message="错误 ID",
                document_candidates=_architecture_candidates(22, "other.pdf"),
                scope_selector=ChatScopeSelector.for_architecture(22),
                max_files_per_request=1,
            )
        with self.assertRaises(ChatScopeModeConflictError):
            self.locks.try_acquire_chat_run(
                identity=identity,
                run_id="run-wrong-mode",
                user_message="错误模式",
                document_candidates=ChatDocumentSelectionCandidates(
                    explicit_documents=(_document_candidate("other.pdf"),)
                ),
                scope_selector=ChatScopeSelector.for_files(("other.pdf",)),
                max_files_per_request=1,
            )

        self.assertIsNone(self.store.runs.get("run-wrong-id"))
        self.assertIsNone(self.store.runs.get("run-wrong-mode"))

    def test_architecture_empty_invalid_and_limit_failures_roll_back_all(self) -> None:
        """新会话目录错误或超限不得留下 Session、Binding 或其他孤儿事实。"""
        cases = (
            (
                "not-found",
                _failed_architecture_candidates(31),
                ChatArchitectureScopeNotFoundError,
                5,
            ),
            (
                "invalid",
                _failed_architecture_candidates(32, invalid=True),
                ChatArchitectureScopeInvalidError,
                5,
            ),
            (
                "over-limit",
                _architecture_candidates(33, "a.pdf", "b.pdf"),
                ValueError,
                1,
            ),
        )
        for suffix, candidates, expected_error, limit in cases:
            with self.subTest(suffix=suffix):
                architecture_id = candidates.architecture_candidates.architecture_id
                identity = _weaponry_identity(suffix, architecture_id)
                with self.assertRaises(expected_error):
                    self.locks.try_acquire_chat_run(
                        identity=identity,
                        run_id=f"run-{suffix}",
                        user_message="问题",
                        document_candidates=candidates,
                        scope_selector=ChatScopeSelector.for_architecture(
                            architecture_id
                        ),
                        max_files_per_request=limit,
                    )
                self.assertIsNone(self.store.identities.resolve_any(identity))
                self.assertIsNone(self.store.runs.get(f"run-{suffix}"))

    def test_fifty_concurrent_architecture_admissions_freeze_once(self) -> None:
        """50 个同 ID 首轮竞争只能产生一个 run、Binding 和 Revision。"""
        worker_count = 50
        barrier = Barrier(worker_count)
        candidates = _architecture_candidates(41, "frozen.pdf")
        selector = ChatScopeSelector.for_architecture(41)
        identity = _weaponry_identity("chat-architecture-concurrent", 41)

        def attempt(index: int) -> str:
            barrier.wait()
            try:
                self.locks.try_acquire_chat_run(
                    identity=identity,
                    run_id=f"run-architecture-{index}",
                    user_message=f"question-{index}",
                    document_candidates=candidates,
                    scope_selector=selector,
                    max_files_per_request=1,
                )
                return "accepted"
            except ChatRunBusyError:
                return "busy"

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            outcomes = list(executor.map(attempt, range(worker_count)))

        self.assertEqual(1, outcomes.count("accepted"))
        self.assertEqual(49, outcomes.count("busy"))
        conversation_id = self._conversation_id(identity)
        binding = self.store.session_scope_bindings.get(conversation_id)
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(41, binding.architecture_id)
        self.assertEqual(
            1,
            len(self.store.scopes.list_revisions_by_chat(
                conversation_id
            )),
        )

    def test_fifty_mixed_architecture_ids_bind_exactly_one_id(self) -> None:
        """同一 chatId 的混合 ID 竞争只能冻结胜方 ID，败方不得留下脏数据。"""

        worker_count = 50
        barrier = Barrier(worker_count)

        def attempt(index: int) -> str:
            architecture_id = 51 if index % 2 == 0 else 52
            barrier.wait()
            try:
                self.locks.try_acquire_chat_run(
                    identity=_identity("chat-architecture-mixed-ids"),
                    run_id=f"run-architecture-mixed-id-{index}",
                    user_message=f"question-{index}",
                    document_candidates=_architecture_candidates(
                        architecture_id,
                        f"frozen-{architecture_id}.pdf",
                    ),
                    scope_selector=ChatScopeSelector.for_architecture(
                        architecture_id
                    ),
                    max_files_per_request=1,
                )
                return "accepted"
            except ChatRunBusyError:
                return "busy"
            except ChatArchitectureIdConflictError:
                return "architecture_id_conflict"

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            outcomes = list(executor.map(attempt, range(worker_count)))

        self.assertEqual(1, outcomes.count("accepted"))
        self.assertEqual(24, outcomes.count("busy"))
        self.assertEqual(25, outcomes.count("architecture_id_conflict"))
        identity = _identity("chat-architecture-mixed-ids")
        conversation_id = self._conversation_id(identity)
        binding = self.store.session_scope_bindings.get(conversation_id)
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertIn(binding.architecture_id, (51, 52))
        self.assertEqual(
            1,
            len(self.store.scopes.list_revisions_by_chat(
                conversation_id
            )),
        )
        self.assertEqual(
            1,
            len(self.store.messages.list_by_chat(
                conversation_id
            )),
        )

    def test_fifty_mixed_scope_modes_bind_exactly_one_mode(self) -> None:
        """同一 chatId 的文件/类别模式竞争只能固化胜方模式。"""

        worker_count = 50
        barrier = Barrier(worker_count)

        def attempt(index: int) -> str:
            is_architecture = index % 2 == 0
            barrier.wait()
            try:
                if is_architecture:
                    candidates = _architecture_candidates(
                        53,
                        "architecture.pdf",
                    )
                    selector = ChatScopeSelector.for_architecture(53)
                else:
                    candidates = ChatDocumentSelectionCandidates(
                        explicit_documents=(
                            _document_candidate("explicit.pdf"),
                        )
                    )
                    selector = ChatScopeSelector.for_files(("explicit.pdf",))
                self.locks.try_acquire_chat_run(
                    identity=_identity("chat-mixed-scope-modes"),
                    run_id=f"run-mixed-scope-mode-{index}",
                    user_message=f"question-{index}",
                    document_candidates=candidates,
                    scope_selector=selector,
                    max_files_per_request=1,
                )
                return "accepted"
            except ChatRunBusyError:
                return "busy"
            except ChatScopeModeConflictError:
                return "scope_mode_conflict"

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            outcomes = list(executor.map(attempt, range(worker_count)))

        self.assertEqual(1, outcomes.count("accepted"))
        self.assertEqual(24, outcomes.count("busy"))
        self.assertEqual(25, outcomes.count("scope_mode_conflict"))
        identity = _identity("chat-mixed-scope-modes")
        conversation_id = self._conversation_id(identity)
        binding = self.store.session_scope_bindings.get(conversation_id)
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertIn(binding.scope_mode, ("files", "architecture"))
        self.assertEqual(
            1,
            len(self.store.scopes.list_revisions_by_chat(
                conversation_id
            )),
        )
        self.assertEqual(
            1,
            len(self.store.messages.list_by_chat(
                conversation_id
            )),
        )

    def test_fifty_different_identities_can_freeze_same_architecture(self) -> None:
        """不同复合身份共享 architectureId 时相互隔离，并可独立冻结快照。"""

        worker_count = 50
        barrier = Barrier(worker_count)
        candidates = _architecture_candidates(61, "shared.pdf")
        selector = ChatScopeSelector.for_architecture(61)

        def attempt(index: int) -> str:
            barrier.wait()
            identity = _weaponry_identity(index + 1, 61)
            run = self.locks.try_acquire_chat_run(
                identity=identity,
                run_id=f"run-shared-architecture-{index}",
                user_message=f"question-{index}",
                document_candidates=candidates,
                scope_selector=selector,
                max_files_per_request=1,
            )
            return run.conversation_id

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            conversation_ids = tuple(executor.map(attempt, range(worker_count)))

        self.assertEqual(worker_count, len(conversation_ids))
        for conversation_id in conversation_ids:
            binding = self.store.session_scope_bindings.get(conversation_id)
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual("architecture", binding.scope_mode)
            self.assertEqual(61, binding.architecture_id)
            self.assertEqual(
                1,
                len(self.store.scopes.list_revisions_by_chat(conversation_id)),
            )
            self.assertEqual(
                1,
                len(self.store.messages.list_by_chat(conversation_id)),
            )

    def test_existing_session_without_binding_fails_closed(self) -> None:
        """人工构造的既有会话缺失不可变 Binding 时必须优先失败关闭。"""

        identity = _identity("chat-existing")
        conversation_id = self.store.identities.create_conversation(
            identity
        ).conversation_id
        candidates = ChatDocumentSelectionCandidates(
            new_session_default_documents=(
                _document_candidate("default.pdf"),
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            "^existing chat session is missing immutable scope binding$",
        ):
            self.locks.try_acquire_chat_run(
                identity=identity,
                run_id="run-existing",
                user_message="继续",
                document_candidates=candidates,
                max_files_per_request=1,
            )

        self.assertIsNone(self.store.runs.get("run-existing"))
        self.assertIsNone(self.store.run_inputs.get("run-existing"))
        self.assertEqual(
            (),
            self.store.messages.list_by_chat(conversation_id),
        )

    def test_new_session_explicit_selection_has_distinct_safe_log(self) -> None:
        """显式选择应使用独立模式，并且日志只记录计数而不泄漏文件身份。"""

        candidates = ChatDocumentSelectionCandidates(
            explicit_documents=(
                _document_candidate("explicit.pdf"),
            )
        )

        with self.assertLogs(
            "app.modules.chat.adapters.sqlite.locking.lock_service",
            level="INFO",
        ) as captured:
            self.locks.try_acquire_chat_run(
                identity=_identity("chat-explicit"),
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
                identity=_identity("chat-over-limit"),
                run_id="run-over-limit",
                user_message="请总结",
                document_candidates=candidates,
                max_files_per_request=1,
            )

        self.assertIsNone(
            self.store.identities.resolve_any(_identity("chat-over-limit"))
        )
        self.assertIsNone(self.store.runs.get("run-over-limit"))
        self.assertIsNone(self.store.run_inputs.get("run-over-limit"))

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
                    identity=_identity("chat-write-failure"),
                    run_id="run-write-failure",
                    user_message="请总结",
                    document_candidates=candidates,
                    max_files_per_request=5,
                )

        self.assertIsNone(
            self.store.identities.resolve_any(_identity("chat-write-failure"))
        )
        self.assertIsNone(self.store.runs.get("run-write-failure"))
        self.assertIsNone(self.store.run_inputs.get("run-write-failure"))

    def test_initial_admission_rolls_back_at_every_business_insert(self) -> None:
        """首次受理任一业务写点失败时，都不得留下半条会话、范围、运行或消息。"""

        # 这些表覆盖首次知识谱系对话同一事务中的完整写入顺序。使用 SQLite
        # BEFORE INSERT 故障触发器可以在不修改生产代码的前提下逐点证明回滚，
        # 同时避免 Mock 掉 Repository 后遗漏真实外键和触发器行为。
        admission_tables = (
            "conversations",
            "conversation_identities",
            "chat_session_scope_bindings",
            "chat_scope_revisions",
            "chat_scope_members",
            "chat_scope_heads",
            "chat_runs",
            "chat_run_inputs",
            "chat_messages",
            "chat_message_files",
        )
        for table_name in admission_tables:
            with self.subTest(table_name=table_name):
                with tempfile.TemporaryDirectory(
                    ignore_cleanup_errors=True
                ) as temporary_dir:
                    db_path = f"{temporary_dir}/chat.sqlite3"
                    ChatStore(db_path)
                    with sqlite3.connect(db_path) as connection:
                        connection.execute(
                            f"""
                            CREATE TRIGGER phase8_fail_insert
                            BEFORE INSERT ON {table_name}
                            BEGIN
                                SELECT RAISE(ABORT, 'phase8 injected write failure');
                            END
                            """
                        )
                        connection.commit()

                    locks = ChatRunLockService(db_path)
                    if table_name == "chat_message_files":
                        identity = FileChatIdentity(chat_id=801)
                        document_candidates = ChatDocumentSelectionCandidates(
                            explicit_documents=(
                                _document_candidate("phase8.pdf"),
                            )
                        )
                        scope_selector = ChatScopeSelector.for_files(
                            ("phase8.pdf",)
                        )
                    else:
                        identity = WeaponryChatIdentity(
                            user_id=801,
                            architecture_id=802,
                        )
                        document_candidates = _architecture_candidates(
                            802,
                            "phase8.pdf",
                        )
                        scope_selector = ChatScopeSelector.for_architecture(802)
                    with self.assertRaises(sqlite3.DatabaseError):
                        locks.try_acquire_chat_run(
                            identity=identity,
                            run_id="phase8-failed-admission",
                            user_message="受理故障注入",
                            document_candidates=document_candidates,
                            scope_selector=scope_selector,
                            max_files_per_request=1,
                        )

                    with sqlite3.connect(db_path) as connection:
                        for business_table in admission_tables:
                            row_count = connection.execute(
                                f"SELECT COUNT(*) FROM {business_table}"
                            ).fetchone()[0]
                            self.assertEqual(
                                0,
                                row_count,
                                msg=(
                                    f"{table_name} 写入失败后仍残留 "
                                    f"{business_table} 事实"
                                ),
                            )

    def test_fifty_concurrent_first_admissions_accept_only_one_run(self) -> None:
        worker_count = 50
        barrier = Barrier(worker_count)
        candidates = ChatDocumentSelectionCandidates(
            new_session_default_documents=(
                _document_candidate("all.pdf"),
            )
        )
        identity = _identity("chat-concurrent-default")

        def attempt(index: int) -> str:
            barrier.wait()
            try:
                self.locks.try_acquire_chat_run(
                    identity=identity,
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
        conversation_id = self._conversation_id(identity)
        active_runs = self.store.runs.list_active(conversation_id)
        self.assertEqual(1, len(active_runs))
        run_input = self.store.run_inputs.get(active_runs[0].run_id)
        self.assertIsNotNone(run_input)
        assert run_input is not None
        self.assertEqual(
            ("all.pdf",),
            tuple(item.file_name for item in run_input.files),
        )
        revisions = self.store.scopes.list_revisions_by_chat(
            conversation_id
        )
        head = self.store.scopes.get_head(conversation_id)
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
            identity=_identity("chat-concurrent-explicit"),
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
                    identity=_identity("chat-concurrent-explicit"),
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
            initial.conversation_id
        )
        head = self.store.scopes.get_head(initial.conversation_id)
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
            identity=_identity("chat-explicit-over-limit"),
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
            initial.conversation_id
        )
        self.assertIsNotNone(previous_head)

        with self.assertRaisesRegex(
            ValueError,
            "^fileNames超过文件对话数量上限$",
        ):
            self.locks.try_acquire_chat_run(
                identity=_identity("chat-explicit-over-limit"),
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
            self.store.scopes.get_head(initial.conversation_id),
        )
        self.assertEqual(
            1,
            len(self.store.scopes.list_revisions_by_chat(
                initial.conversation_id
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
            identity=_identity("40001"),
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

        history = ChatHistoryService(self.store).list_history(_identity(40001))

        self.assertEqual(1, len(history))
        self.assertEqual([], history[0]["files"])

    def test_failed_run_releases_chat_for_next_attempt(self) -> None:
        self.locks.try_acquire_chat_run(
            identity=_identity("chat-fail"),
            run_id="run-fail",
        )
        self.locks.issue_execution_lease(run_id="run-fail")
        failed = self.locks.fail_run("run-fail", error_message="stream failed")
        retry = self.locks.try_acquire_chat_run(
            identity=_identity("chat-fail"),
            run_id="run-retry",
        )

        self.assertEqual("failed", failed.status)
        self.assertEqual(RUN_ACCEPTED, retry.status)

    def test_different_chats_do_not_block_each_other(self) -> None:
        first = self.locks.try_acquire_chat_run(
            identity=_identity("chat-one"),
            run_id="run-one",
        )
        second = self.locks.try_acquire_chat_run(
            identity=_identity("chat-two"),
            run_id="run-two",
        )

        self.assertEqual(RUN_ACCEPTED, first.status)
        self.assertEqual(RUN_ACCEPTED, second.status)

    def test_request_abort_sets_flag_on_active_run(self) -> None:
        self.locks.try_acquire_chat_run(
            identity=_identity("chat-abort"),
            run_id="run-abort",
        )

        aborted = self.locks.request_abort("run-abort")

        self.assertTrue(aborted.abort_requested)

    def test_discard_unstarted_run_hides_the_accepted_user_message(self) -> None:
        """已受理状态从未领取执行权时，断开连接不应产生历史轮次。"""
        run = self.locks.try_acquire_chat_run(
            identity=_identity("chat-discard"),
            run_id="run-discard",
            user_message="尚未执行",
        )

        discarded = self.locks.discard_unstarted_run(
            run_id=run.run_id,
            error_message="response closed before execution",
        )

        message = self.store.messages.list_by_chat(run.conversation_id)[0]
        self.assertEqual(RUN_FAILED, discarded.status)
        self.assertEqual(MESSAGE_DISCARDED, message.status)
        self.assertEqual((), self.store.runs.list_active(run.conversation_id))

    def test_heartbeat_updates_active_run(self) -> None:
        started = self.locks.try_acquire_chat_run(
            identity=_identity("chat-heartbeat"),
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
            identity=_identity("chat-stale"),
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
            identity=_identity("chat-stale"),
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
            identity=_identity("chat-stale-explicit"),
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

        conversation_id = self._conversation_id(
            _identity("chat-stale-explicit")
        )
        expired = locks.expire_stale_runs_for_chat(
            conversation_id=conversation_id
        )
        active = self.store.runs.list_active(conversation_id)

        self.assertEqual(["run-stale-explicit"], [run.run_id for run in expired])
        self.assertEqual(RUN_FAILED, expired[0].status)
        self.assertEqual("chat run heartbeat expired", expired[0].error_message)
        self.assertEqual((), active)

    def test_terminal_run_rejects_illegal_follow_up_state_changes(self) -> None:
        self.locks.try_acquire_chat_run(
            identity=_identity("chat-terminal"),
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
