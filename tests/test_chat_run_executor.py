"""文件对话运行执行边界的测试。"""

from __future__ import annotations

import tempfile
import unittest
import zlib
from unittest.mock import patch

from app.modules.chat.ports import (
    ChatChunk,
    ChatDocumentRef,
    ChatSessionRefs,
    ChatSourceEvidence,
    ChatSourceFinalization,
)
from app.modules.chat.domain import CHAT_ARCHITECTURE_CANDIDATE_RESOLVED
from app.modules.chat import (
    ChatArchitectureCandidates,
    ChatCommandService,
    ChatDocumentCandidate,
    ChatRunEventRepository,
    ChatRunLockService,
    ChatRunExecutor,
    ChatRunEventRecorder,
    ChatRunStreamRequest,
    ChatStreamEvent,
    ChatStore,
    ChatSessionScopeBinding,
    ChatScopeSelector,
    ResolvedChatDocument,
    MESSAGE_COMMITTED,
    MESSAGE_DISCARDED,
    MESSAGE_PENDING,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    RUN_ABORTED,
    RUN_ACCEPTED,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    SynchronousChatRunExecutor,
    record_chat_run_events,
)
from tests.fakes import FakeChatConversationFactory
from tests.fakes.chat import FakeChatConversationPort
from app.modules.chat.domain.identity import FileChatIdentity, WeaponryChatIdentity


def _identity(value: str | int) -> FileChatIdentity:
    """将旧测试标签稳定映射为文件对话公开身份。"""
    if isinstance(value, int) or str(value).isdigit():
        return FileChatIdentity(chat_id=int(value))
    return FileChatIdentity(chat_id=zlib.crc32(str(value).encode("utf-8")) + 1)


def _weaponry_identity(value: str | int, architecture_id: int) -> WeaponryChatIdentity:
    """构造互不冲突的知识谱系复合身份。"""
    file_identity = _identity(value)
    return WeaponryChatIdentity(
        user_id=file_identity.chat_id,
        architecture_id=architecture_id,
    )


class _StaticDocumentResolver:
    def __init__(self, *, all_file_names=()):
        self.all_file_names = tuple(all_file_names)
        self.resolve_many_calls = []
        self.resolve_all_available_calls = 0

    def resolve_many(self, file_names):
        normalized_file_names = tuple(file_names)
        self.resolve_many_calls.append(normalized_file_names)
        return tuple(
            ResolvedChatDocument(
                file_name=file_name,
                original_name=f"{file_name}.original",
                structured_source_key=f"custom-documents/{file_name}.json",
                document=ChatDocumentRef(
                    document_ref=f"document:{file_name}",
                    external_location=f"custom-documents/{file_name}.json",
                ),
            )
            for file_name in normalized_file_names
        )

    def resolve_all_available(self):
        """返回独立全量候选，并记录调用次数以验证阶段 2 选择路径。"""
        self.resolve_all_available_calls += 1
        return tuple(
            ResolvedChatDocument(
                file_name=file_name,
                original_name=f"{file_name}.original",
                structured_source_key=f"custom-documents/{file_name}.json",
                document=ChatDocumentRef(
                    document_ref=f"document:{file_name}",
                    external_location=f"custom-documents/{file_name}.json",
                ),
            )
            for file_name in self.all_file_names
        )


class _ArchitectureDocumentResolver(_StaticDocumentResolver):
    """同时提供 architecture 精确解析能力的离线测试替身。"""

    def __init__(
        self,
        *,
        architecture_file_names=(),
        fail_if_architecture_called: bool = False,
    ) -> None:
        super().__init__()
        self.architecture_file_names = tuple(architecture_file_names)
        self.fail_if_architecture_called = fail_if_architecture_called
        self.resolve_architecture_calls: list[int] = []

    def resolve_by_architecture_id(
        self,
        architecture_id: int,
    ) -> ChatArchitectureCandidates:
        """返回固定类别快照；可配置为一旦被重查就立即暴露测试失败。"""

        self.resolve_architecture_calls.append(architecture_id)
        if self.fail_if_architecture_called:
            raise AssertionError("existing architecture scope must not be re-resolved")
        return ChatArchitectureCandidates(
            architecture_id=architecture_id,
            resolution_outcome=CHAT_ARCHITECTURE_CANDIDATE_RESOLVED,
            documents=tuple(
                ChatDocumentCandidate(
                    file_name=file_name,
                    original_name=f"{file_name}.original",
                    document_ref=f"document:{file_name}",
                    external_location=f"custom-documents/{file_name}.json",
                    structured_source_key=f"custom-documents/{file_name}.json",
                )
                for file_name in self.architecture_file_names
            ),
        )


class ChatRunStreamRequestTests(unittest.TestCase):
    """校验面向未来运行执行器的队列安全输入。"""

    def test_request_normalizes_text_and_file_snapshots(self) -> None:
        request = ChatRunStreamRequest(
            run_id=" run-1 ",
            conversation_id=" chat-1 ",
            message=" 你好 ",
            file_names=(" hash-a.pdf ",),
            file_original_names=(" 原名.pdf ",),
        )

        self.assertEqual("run-1", request.run_id)
        self.assertEqual("chat-1", request.conversation_id)
        self.assertEqual("你好", request.message)
        self.assertEqual(("hash-a.pdf",), request.file_names)
        self.assertEqual(("原名.pdf",), request.file_original_names)

    def test_request_rejects_ambiguous_file_sequences(self) -> None:
        with self.assertRaises(TypeError):
            ChatRunStreamRequest(
                run_id="run-1",
                conversation_id="chat-1",
                message="hi",
                file_names="hash-a.pdf",  # type: ignore[arg-type]
                file_original_names=("原名.pdf",),
            )

    def test_request_keeps_empty_requested_scope_separate_from_effective_scope(
        self,
    ) -> None:
        request = ChatRunStreamRequest(
            run_id="run-1",
            conversation_id="chat-1",
            message="hi",
            file_names=("hash-a.pdf",),
            file_original_names=("原名.pdf",),
            requested_file_names=(),
            requested_file_original_names=(),
        )

        self.assertEqual(("hash-a.pdf",), request.file_names)
        self.assertEqual((), request.requested_file_names)

    def test_request_rejects_non_empty_requested_effective_mismatch(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "non-empty requested_file_names must match effective file_names",
        ):
            ChatRunStreamRequest(
                run_id="run-1",
                conversation_id="chat-1",
                message="hi",
                file_names=("effective.pdf",),
                file_original_names=("有效原名.pdf",),
                requested_file_names=("requested.pdf",),
                requested_file_original_names=("请求原名.pdf",),
            )

    def test_request_requires_matching_file_snapshot_lengths(self) -> None:
        with self.assertRaises(ValueError):
            ChatRunStreamRequest(
                run_id="run-1",
                conversation_id="chat-1",
                message="hi",
                file_names=("hash-a.pdf",),
                file_original_names=(),
            )

    def test_protocol_accepts_event_stream_executor(self) -> None:
        class FakeExecutor:
            def execute_chat_run(self, run_id: str):
                yield ChatStreamEvent("done", {"chatId": run_id})

        self.assertIsInstance(FakeExecutor(), ChatRunExecutor)


class ChatRunEventRecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.db_path = f"{self.tmp}/chat.sqlite3"
        self.store = ChatStore(self.db_path)
        self.commands = ChatCommandService(ChatRunLockService(self.db_path))
        self.identity = FileChatIdentity(chat_id=10001)
        self.conversation_id = self.store.identities.create_conversation(
            self.identity
        ).conversation_id
        self.store.session_scope_bindings.create(
            ChatSessionScopeBinding(
                conversation_id=self.conversation_id,
                scope_mode="files",
                architecture_id=None,
                created_at="2026-07-28T00:00:00+00:00",
            )
        )
        self.store.runs.create(
            run_id="run-1",
            conversation_id=self.conversation_id,
        )
        self.store.runs.mark_running("run-1")
        self.request = ChatRunStreamRequest(
            run_id="run-1",
            conversation_id=self.conversation_id,
            message="请总结",
            file_names=("hash-a.pdf",),
            file_original_names=("原名.pdf",),
        )

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def test_done_commits_user_and_complete_assistant_message(self) -> None:
        events = [
            ChatStreamEvent("chatInfo", {"chatId": 10001}),
            ChatStreamEvent("textChunk", {"content": "你好"}),
            ChatStreamEvent("textChunk", {"content": "世界"}),
            ChatStreamEvent("done", {"chatId": 10001}),
        ]

        result = list(
            record_chat_run_events(
                request=self.request,
                events=events,
                store=self.store,
                chat_commands=self.commands,
            )
        )

        messages = self.store.messages.list_by_chat(self.conversation_id)
        run = self.store.runs.get("run-1")
        self.assertEqual(events, result)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_SUCCEEDED, run.status)
        self.assertEqual(2, len(messages))
        self.assertEqual(MESSAGE_ROLE_USER, messages[0].role)
        self.assertEqual(MESSAGE_COMMITTED, messages[0].status)
        self.assertEqual("请总结", messages[0].content)
        self.assertEqual("hash-a.pdf", messages[0].files[0].file_name)
        self.assertEqual("原名.pdf", messages[0].files[0].original_name)
        self.assertEqual(MESSAGE_ROLE_ASSISTANT, messages[1].role)
        self.assertEqual(MESSAGE_COMMITTED, messages[1].status)
        self.assertEqual("你好世界", messages[1].content)
        self.assertEqual(
            ["chatInfo", "textChunk", "textChunk", "done"],
            [event.event_type for event in self.store.events.list_by_run("run-1")],
        )

    def test_error_commits_user_without_partial_assistant(self) -> None:
        events = [
            ChatStreamEvent("chatInfo", {"chatId": 10001}),
            ChatStreamEvent("textChunk", {"content": "半截"}),
            ChatStreamEvent("error", {"error": "boom"}),
        ]

        self.assertEqual(
            events,
            list(
                record_chat_run_events(
                    request=self.request,
                    events=events,
                    store=self.store,
                    chat_commands=self.commands,
                )
            ),
        )

        messages = self.store.messages.list_by_chat(self.conversation_id)
        run = self.store.runs.get("run-1")
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_FAILED, run.status)
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_ROLE_USER, messages[0].role)
        self.assertEqual(MESSAGE_COMMITTED, messages[0].status)

    def test_aborted_commits_user_without_partial_assistant(self) -> None:
        events = [
            ChatStreamEvent("textChunk", {"content": "半截"}),
            ChatStreamEvent("aborted", {}),
        ]

        list(
            record_chat_run_events(
                request=self.request,
                events=events,
                store=self.store,
                chat_commands=self.commands,
            )
        )

        messages = self.store.messages.list_by_chat(self.conversation_id)
        run = self.store.runs.get("run-1")
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_ABORTED, run.status)
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_ROLE_USER, messages[0].role)
        self.assertEqual(MESSAGE_COMMITTED, messages[0].status)

    def test_abort_request_stops_stream_before_next_chunk(self) -> None:
        stream = record_chat_run_events(
            request=self.request,
            events=[
                ChatStreamEvent("textChunk", {"content": "第一段"}),
                ChatStreamEvent("textChunk", {"content": "第二段"}),
                ChatStreamEvent("done", {"chatId": 10001}),
            ],
            store=self.store,
            chat_commands=self.commands,
        )

        first = next(stream)
        self.commands.request_abort(run_id="run-1")
        second = next(stream)

        self.assertEqual(ChatStreamEvent("textChunk", {"content": "第一段"}), first)
        self.assertEqual(ChatStreamEvent("aborted", {}), second)
        with self.assertRaises(StopIteration):
            next(stream)
        messages = self.store.messages.list_by_chat(self.conversation_id)
        run = self.store.runs.get("run-1")
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_ROLE_USER, messages[0].role)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_ABORTED, run.status)
        next_run = self.commands.start_chat_run(identity=self.identity)
        self.assertNotEqual("run-1", next_run.run_id)
        self.assertEqual(RUN_ACCEPTED, next_run.status)

    def test_abort_request_during_upstream_wait_wins_over_done_event(self) -> None:
        commands = self.commands

        class AbortBeforeDoneStream:
            def __init__(self) -> None:
                self.index = 0

            def __iter__(self):
                return self

            def __next__(self) -> ChatStreamEvent:
                if self.index == 0:
                    self.index += 1
                    return ChatStreamEvent("textChunk", {"content": "第一段"})
                if self.index == 1:
                    self.index += 1
                    commands.request_abort(run_id="run-1")
                    return ChatStreamEvent("done", {"chatId": 10001})
                raise StopIteration

        result = list(
            record_chat_run_events(
                request=self.request,
                events=AbortBeforeDoneStream(),
                store=self.store,
                chat_commands=self.commands,
            )
        )

        self.assertEqual(
            [
                ChatStreamEvent("textChunk", {"content": "第一段"}),
                ChatStreamEvent("aborted", {}),
            ],
            result,
        )
        messages = self.store.messages.list_by_chat(self.conversation_id)
        run = self.store.runs.get("run-1")
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_ROLE_USER, messages[0].role)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_ABORTED, run.status)

    def test_pre_requested_abort_does_not_consume_upstream_event(self) -> None:
        self.commands.request_abort(run_id="run-1")

        result = list(
            record_chat_run_events(
                request=self.request,
                events=[ChatStreamEvent("textChunk", {"content": "不会输出"})],
                store=self.store,
                chat_commands=self.commands,
            )
        )

        self.assertEqual([ChatStreamEvent("aborted", {})], result)
        messages = self.store.messages.list_by_chat(self.conversation_id)
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_ROLE_USER, messages[0].role)

    def test_abort_request_before_upstream_exception_yields_aborted(self) -> None:
        commands = self.commands

        class AbortThenRaiseStream:
            def __iter__(self):
                return self

            def __next__(self) -> ChatStreamEvent:
                commands.request_abort(run_id="run-1")
                raise RuntimeError("upstream closed")

        result = list(
            record_chat_run_events(
                request=self.request,
                events=AbortThenRaiseStream(),
                store=self.store,
                chat_commands=self.commands,
            )
        )

        self.assertEqual([ChatStreamEvent("aborted", {})], result)
        messages = self.store.messages.list_by_chat(self.conversation_id)
        run = self.store.runs.get("run-1")
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_ROLE_USER, messages[0].role)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_ABORTED, run.status)

    def test_abort_request_before_upstream_end_yields_aborted(self) -> None:
        commands = self.commands

        class AbortThenEndStream:
            def __iter__(self):
                return self

            def __next__(self) -> ChatStreamEvent:
                commands.request_abort(run_id="run-1")
                raise StopIteration

        result = list(
            record_chat_run_events(
                request=self.request,
                events=AbortThenEndStream(),
                store=self.store,
                chat_commands=self.commands,
            )
        )

        self.assertEqual([ChatStreamEvent("aborted", {})], result)
        messages = self.store.messages.list_by_chat(self.conversation_id)
        run = self.store.runs.get("run-1")
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_ROLE_USER, messages[0].role)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_ABORTED, run.status)

    def test_close_after_abort_marks_run_aborted(self) -> None:
        stream = record_chat_run_events(
            request=self.request,
            events=[
                ChatStreamEvent("textChunk", {"content": "第一段"}),
                ChatStreamEvent("done", {"chatId": 10001}),
            ],
            store=self.store,
            chat_commands=self.commands,
        )

        self.assertEqual(
            ChatStreamEvent("textChunk", {"content": "第一段"}),
            next(stream),
        )
        self.commands.request_abort(run_id="run-1")
        stream.close()

        messages = self.store.messages.list_by_chat(self.conversation_id)
        run = self.store.runs.get("run-1")
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_ROLE_USER, messages[0].role)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_ABORTED, run.status)

    def test_non_terminal_event_heartbeats_active_run(self) -> None:
        list(
            record_chat_run_events(
                request=self.request,
                events=[ChatStreamEvent("chatInfo", {"chatId": 10001})],
                store=self.store,
                chat_commands=self.commands,
            )
        )

        run = self.store.runs.get("run-1")
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_FAILED, run.status)
        self.assertIsNotNone(run.heartbeat_at)
        self.assertEqual(
            ["chatInfo", "error"],
            [event.event_type for event in self.store.events.list_by_run("run-1")],
        )

    def test_event_is_persisted_before_it_is_yielded(self) -> None:
        stream = record_chat_run_events(
            request=self.request,
            events=[
                ChatStreamEvent("textChunk", {"content": "第一段"}),
                ChatStreamEvent("done", {"chatId": 10001}),
            ],
            store=self.store,
            chat_commands=self.commands,
        )

        first = next(stream)

        self.assertEqual(ChatStreamEvent("textChunk", {"content": "第一段"}), first)
        self.assertEqual(
            ["textChunk"],
            [event.event_type for event in self.store.events.list_by_run("run-1")],
        )
        stream.close()

    def test_terminal_event_failure_rolls_back_message_and_run_terminal_state(self) -> None:
        self.store.messages.append(
            message_id="run-1:user",
            conversation_id=self.conversation_id,
            run_id="run-1",
            role=MESSAGE_ROLE_USER,
            content="请总结",
            status=MESSAGE_PENDING,
        )

        with patch.object(
            ChatRunEventRepository,
            "append_in_transaction",
            side_effect=RuntimeError("event write failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "event write failed"):
                self.commands.complete_chat_run_with_messages(
                    run_id="run-1",
                    user_message_id="run-1:user",
                    assistant_message_id="run-1:assistant",
                    assistant_content="必须回滚",
                    terminal_event=ChatStreamEvent("done", {"chatId": 10001}),
                )

        user = self.store.messages.list_by_chat(self.conversation_id)[0]
        run = self.store.runs.get("run-1")
        self.assertEqual(MESSAGE_PENDING, user.status)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_RUNNING, run.status)
        self.assertEqual((), self.store.events.list_by_run("run-1"))

    def test_non_terminal_event_persistence_failure_fails_run_without_yielding_it(self) -> None:
        with patch.object(
            self.store.events,
            "append",
            side_effect=RuntimeError("event ledger unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "event ledger unavailable"):
                list(
                    record_chat_run_events(
                        request=self.request,
                        events=[ChatStreamEvent("textChunk", {"content": "不会展示"})],
                        store=self.store,
                        chat_commands=self.commands,
                    )
                )

        run = self.store.runs.get("run-1")
        messages = self.store.messages.list_by_chat(self.conversation_id)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_FAILED, run.status)
        self.assertEqual([MESSAGE_ROLE_USER], [message.role for message in messages])
        self.assertEqual(
            ["error"],
            [event.event_type for event in self.store.events.list_by_run("run-1")],
        )

    def test_user_pending_append_is_idempotent_after_commit(self) -> None:
        events = [
            ChatStreamEvent("textChunk", {"content": "回答"}),
            ChatStreamEvent("done", {"chatId": 10001}),
        ]
        list(
            record_chat_run_events(
                request=self.request,
                events=events,
                store=self.store,
                chat_commands=self.commands,
            )
        )

        same = self.store.messages.append(
            message_id="run-1:user",
            conversation_id=self.conversation_id,
            run_id="run-1",
            role=MESSAGE_ROLE_USER,
            content="请总结",
            status=MESSAGE_PENDING,
            files=(("hash-a.pdf", "原名.pdf"),),
        )

        self.assertEqual(MESSAGE_COMMITTED, same.status)

    def test_success_terminal_transaction_rolls_back_user_on_assistant_write_error(self) -> None:
        self.store.messages.append(
            message_id="run-1:user",
            conversation_id=self.conversation_id,
            run_id="run-1",
            role=MESSAGE_ROLE_USER,
            content="请总结",
            status=MESSAGE_PENDING,
        )

        with self.assertRaises(ValueError):
            self.commands.complete_chat_run_with_messages(
                run_id="run-1",
                user_message_id="run-1:user",
                assistant_message_id="",
                assistant_content="必须导致事务回滚",
            )

        user = self.store.messages.list_by_chat(self.conversation_id)[0]
        run = self.store.runs.get("run-1")
        self.assertEqual(MESSAGE_PENDING, user.status)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_RUNNING, run.status)

    def test_high_frequency_chunks_do_not_write_a_heartbeat_per_chunk(self) -> None:
        events = [
            *(ChatStreamEvent("textChunk", {"content": "x"}) for _ in range(50)),
            ChatStreamEvent("done", {"chatId": 10001}),
        ]

        with patch.object(self.commands, "heartbeat_chat_run") as heartbeat:
            list(
                ChatRunEventRecorder(
                    self.store,
                    heartbeat_interval_seconds=60.0,
                ).record(
                    request=self.request,
                    events=events,
                    chat_commands=self.commands,
                )
            )

        heartbeat.assert_called_once_with(run_id="run-1")


class SynchronousChatRunExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.db_path = f"{self.tmp}/chat.sqlite3"
        self.store = ChatStore(self.db_path)
        self.commands = ChatCommandService(ChatRunLockService(self.db_path))
        self.resolver = _StaticDocumentResolver()

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def _executor(self, resolver=None) -> SynchronousChatRunExecutor:
        """构造不连接真实供应商的同步执行器。"""
        return SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=FakeChatConversationFactory(),
            document_resolver=resolver or self.resolver,
        )

    def test_non_empty_candidate_scope_resolves_only_explicit_files(self) -> None:
        resolver = _StaticDocumentResolver(all_file_names=("all.pdf",))
        candidates = self._executor(resolver).resolve_document_candidates(
            identity=_identity("candidate-explicit"),
            file_names=("explicit.pdf",),
        )

        self.assertEqual(
            ("explicit.pdf",),
            tuple(
                item.file_name for item in candidates.explicit_documents
            ),
        )
        self.assertEqual((), candidates.new_session_default_documents)
        self.assertEqual([("explicit.pdf",)], resolver.resolve_many_calls)
        self.assertEqual(0, resolver.resolve_all_available_calls)

    def test_existing_session_empty_scope_skips_catalog_scan(self) -> None:
        identity = _identity("candidate-existing")
        self.store.identities.create_conversation(identity)
        resolver = _StaticDocumentResolver(all_file_names=("all.pdf",))

        candidates = self._executor(resolver).resolve_document_candidates(
            identity=identity,
            file_names=(),
        )

        self.assertEqual((), candidates.explicit_documents)
        self.assertEqual((), candidates.new_session_default_documents)
        self.assertEqual([], resolver.resolve_many_calls)
        self.assertEqual(0, resolver.resolve_all_available_calls)

    def test_missing_session_empty_scope_prepares_default_catalog_once(self) -> None:
        resolver = _StaticDocumentResolver(
            all_file_names=("alpha.pdf", "beta.pdf")
        )

        candidates = self._executor(resolver).resolve_document_candidates(
            identity=_identity("candidate-new"),
            file_names=(),
        )

        self.assertEqual((), candidates.explicit_documents)
        self.assertEqual(
            ("alpha.pdf", "beta.pdf"),
            tuple(
                item.file_name
                for item in candidates.new_session_default_documents
            ),
        )
        self.assertEqual([], resolver.resolve_many_calls)
        self.assertEqual(1, resolver.resolve_all_available_calls)

    def test_stage_three_applies_default_candidates_to_new_public_run(self) -> None:
        resolver = _StaticDocumentResolver(all_file_names=("all.pdf",))
        executor = self._executor(resolver)

        prepared = executor.prepare_chat_run(
            identity=_identity("candidate-stage-gate"),
            message="question",
            file_names=(),
        )

        run_input = self.store.run_inputs.get(prepared.run_id)
        self.assertIsNotNone(run_input)
        assert run_input is not None
        self.assertEqual(
            ("all.pdf",),
            tuple(item.file_name for item in run_input.files),
        )
        self.assertEqual([], resolver.resolve_many_calls)
        self.assertEqual(1, resolver.resolve_all_available_calls)

    def test_accepted_run_survives_observability_count_read_failure(
        self,
    ) -> None:
        """受理后的日志计数读取失败不得把已提交运行变成调用方可见失败。"""

        resolver = _StaticDocumentResolver(all_file_names=("all.pdf",))
        executor = self._executor(resolver)
        with patch.object(
            self.store.run_inputs,
            "get",
            side_effect=RuntimeError("injected telemetry read failure"),
        ):
            with self.assertLogs(
                "app.modules.chat.application.run_executor",
                level="ERROR",
            ) as captured:
                prepared = executor.prepare_chat_run(
                    identity=_identity(20006),
                    message="question",
                    file_names=(),
                )

        # 退出故障注入后必须能读取并执行刚才已原子提交的事实，证明异常只影响日志。
        accepted_input = self.store.run_inputs.get(prepared.run_id)
        self.assertIsNotNone(accepted_input)
        assert accepted_input is not None
        self.assertEqual(("all.pdf",), tuple(
            item.file_name for item in accepted_input.files
        ))
        self.assertEqual(
            ["chatInfo", "textChunk", "done"],
            [
                event.event_type
                for event in executor.execute_chat_run(prepared.run_id)
            ],
        )
        self.assertTrue(any(
            "日志计数读取失败，继续返回已受理运行" in message
            for message in captured.output
        ))

    def test_failed_first_run_retry_reuses_frozen_active_scope(
        self,
    ) -> None:
        resolver = _StaticDocumentResolver(all_file_names=("all.pdf",))
        executor = self._executor(resolver)
        identity = _identity("candidate-first-failed")
        first = executor.prepare_chat_run(
            identity=identity,
            message="first",
            file_names=(),
        )
        self.commands.fail_chat_run(
            run_id=first.run_id,
            error_message="first run failed",
        )

        retry = executor.prepare_chat_run(
            identity=identity,
            message="retry",
            file_names=(),
        )

        first_input = self.store.run_inputs.get(first.run_id)
        retry_input = self.store.run_inputs.get(retry.run_id)
        self.assertIsNotNone(first_input)
        self.assertIsNotNone(retry_input)
        assert first_input is not None
        assert retry_input is not None
        self.assertEqual(("all.pdf",), tuple(
            item.file_name for item in first_input.files
        ))
        self.assertEqual(("all.pdf",), tuple(
            item.file_name for item in retry_input.files
        ))
        self.assertEqual((), retry_input.requested_files)
        self.assertEqual("active_scope_reuse", retry_input.selection_mode)
        self.assertEqual(
            first_input.effective_scope_revision_id,
            retry_input.effective_scope_revision_id,
        )
        self.assertEqual(1, resolver.resolve_all_available_calls)

    def test_default_documents_follow_same_execution_path_as_explicit_documents(
        self,
    ) -> None:
        """自动全量与显式文件必须复用同一 Port、绑定和历史提交链。"""

        resolver = _StaticDocumentResolver(
            all_file_names=("alpha.pdf", "beta.pdf")
        )
        factory = FakeChatConversationFactory(stream_contents=("answer",))
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            document_resolver=resolver,
        )
        explicit = executor.prepare_chat_run(
            identity=_identity(20001),
            message="explicit question",
            file_names=("alpha.pdf", "beta.pdf"),
        )
        default = executor.prepare_chat_run(
            identity=_identity(20002),
            message="default question",
            file_names=(),
        )

        explicit_events = list(executor.execute_chat_run(explicit.run_id))
        default_events = list(executor.execute_chat_run(default.run_id))

        expected_refs = ("document:alpha.pdf", "document:beta.pdf")
        self.assertEqual(2, len(factory.ports))
        self.assertEqual(0, factory.active_leases)
        self.assertIsNot(factory.ports[0], factory.ports[1])
        for events in (explicit_events, default_events):
            self.assertEqual(
                ["chatInfo", "textChunk", "done"],
                [event.event_type for event in events],
            )
            self.assertTrue(events[0].data["isNewChat"])
        for port in factory.ports:
            self.assertEqual(1, len(port.attach_document_calls))
            self.assertEqual(
                expected_refs,
                tuple(
                    item.document_ref
                    for item in port.attach_document_calls[0][1]
                ),
            )
            self.assertEqual(expected_refs, port.stream_message_calls[0][2])

        for prepared, expected_history_files in (
            (
                explicit,
                ("alpha.pdf.original", "beta.pdf.original"),
            ),
            (default, ()),
        ):
            bindings = self.store.document_bindings.list_current_by_chat(
                prepared.conversation_id
            )
            messages = self.store.messages.list_by_chat(prepared.conversation_id)
            leases = self.store.resource_leases.list_by_chat(
                prepared.conversation_id
            )
            self.assertEqual(
                expected_refs,
                tuple(item.document_ref for item in bindings),
            )
            self.assertEqual(
                expected_history_files,
                tuple(item.original_name for item in messages[0].files),
            )
            self.assertEqual(
                ["document_binding", "document_binding", "thread", "workspace"],
                sorted(item.resource_type for item in leases),
            )
            self.assertTrue(all(item.status == "active" for item in leases))
            self.assertTrue(
                all(item.run_id == prepared.run_id for item in leases)
            )

    def test_missing_remote_attachment_receipt_fails_closed_without_binding(
        self,
    ) -> None:
        """远端未回传所请求位置时，禁止用原始 document_ref 假装绑定成功。"""
        factory = FakeChatConversationFactory(stream_contents=("answer",))
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            document_resolver=self.resolver,
        )
        prepared = executor.prepare_chat_run(
            identity=_identity("20006"),
            message="question",
            file_names=("alpha.pdf",),
        )

        with patch.object(
            FakeChatConversationPort,
            "attach_documents",
            return_value=(),
        ):
            events = list(executor.execute_chat_run(prepared.run_id))

        self.assertEqual(["error"], [
            event.event_type for event in events
        ])
        self.assertEqual(
            (),
            self.store.document_bindings.list_current_by_chat(
                prepared.conversation_id
            ),
        )
        run = self.store.runs.get(prepared.run_id)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_FAILED, run.status)

    def test_duplicate_remote_attachment_receipt_fails_closed_without_binding(
        self,
    ) -> None:
        """同一远端位置出现多个回执时无法确定规范身份，整批不得落本地 Binding。"""
        factory = FakeChatConversationFactory(stream_contents=("answer",))
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            document_resolver=self.resolver,
        )
        prepared = executor.prepare_chat_run(
            identity=_identity("20007"),
            message="question",
            file_names=("alpha.pdf",),
        )
        location = "custom-documents/alpha.pdf.json"

        with patch.object(
            FakeChatConversationPort,
            "attach_documents",
            return_value=(
                ChatDocumentRef(
                    document_ref="document:canonical-a",
                    external_location=location,
                ),
                ChatDocumentRef(
                    document_ref="document:canonical-b",
                    external_location=location,
                ),
            ),
        ):
            events = list(executor.execute_chat_run(prepared.run_id))

        self.assertEqual(["error"], [
            event.event_type for event in events
        ])
        self.assertEqual(
            (),
            self.store.document_bindings.list_current_by_chat(
                prepared.conversation_id
            ),
        )

    def test_unique_remote_attachment_receipt_can_canonicalize_document_ref(
        self,
    ) -> None:
        """位置唯一匹配后，以供应商回执的规范 document_ref 驱动模型并持久化。"""
        factory = FakeChatConversationFactory(stream_contents=("answer",))
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            document_resolver=self.resolver,
        )
        prepared = executor.prepare_chat_run(
            identity=_identity("20008"),
            message="question",
            file_names=("alpha.pdf",),
        )

        with (
            patch.object(
                FakeChatConversationPort,
                "attach_documents",
                return_value=(
                    ChatDocumentRef(
                        document_ref="document:canonical-alpha",
                        external_location=r"custom-documents\alpha.pdf.json",
                    ),
                ),
            ),
            patch.object(
                FakeChatConversationPort,
                "stream_message",
                return_value=iter(
                    (
                        ChatChunk("answer", 1),
                        ChatSourceFinalization(sources=()),
                    )
                ),
            ) as stream_message,
        ):
            events = list(executor.execute_chat_run(prepared.run_id))

        self.assertEqual(
            ["chatInfo", "textChunk", "done"],
            [event.event_type for event in events],
        )
        bindings = self.store.document_bindings.list_current_by_chat(
            prepared.conversation_id
        )
        self.assertEqual(1, len(bindings))
        self.assertEqual("document:canonical-alpha", bindings[0].document_ref)
        self.assertEqual(
            "custom-documents/alpha.pdf.json",
            bindings[0].external_location,
        )
        self.assertEqual(
            ("document:canonical-alpha",),
            stream_message.call_args.kwargs["document_refs"],
        )

    def test_default_execution_uses_accepted_snapshot_after_catalog_changes(
        self,
    ) -> None:
        """受理后知识库候选变化，不得改变已冻结运行的执行文档。"""

        resolver = _StaticDocumentResolver(all_file_names=("alpha.pdf",))
        factory = FakeChatConversationFactory(stream_contents=("answer",))
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            document_resolver=resolver,
        )
        prepared = executor.prepare_chat_run(
            identity=_identity("20003"),
            message="question",
            file_names=(),
        )
        resolver.all_file_names = ("beta.pdf",)

        events = list(executor.execute_chat_run(prepared.run_id))

        self.assertEqual(
            ["chatInfo", "textChunk", "done"],
            [event.event_type for event in events],
        )
        self.assertEqual(1, resolver.resolve_all_available_calls)
        self.assertEqual(1, len(factory.ports))
        self.assertEqual(
            ("document:alpha.pdf",),
            tuple(
                item.document_ref
                for item in factory.ports[0].attach_document_calls[0][1]
            ),
        )
        self.assertEqual(
            ("document:alpha.pdf",),
            factory.ports[0].stream_message_calls[0][2],
        )
        self.assertEqual(
            ("document:alpha.pdf",),
            tuple(
                item.document_ref
                for item in self.store.document_bindings.list_current_by_chat(
                    prepared.conversation_id
                )
            ),
        )

    def test_restarted_executor_recovers_requested_and_effective_from_sqlite(
        self,
    ) -> None:
        """受理后重建执行器时，不依赖目录或进程内请求恢复两套范围。"""

        resolver = _StaticDocumentResolver(all_file_names=("alpha.pdf",))
        prepared = self._executor(resolver).prepare_chat_run(
            identity=_identity("30001"),
            message="question",
            file_names=(),
        )

        # 用同一数据库重新装配 Store、Coordinator 与执行器，模拟进程退出后由未来
        # Worker 仅凭 run_id 恢复；新 Resolver 的目录内容故意不同，且不应被调用。
        restarted_store = ChatStore(self.db_path)
        restarted_commands = ChatCommandService(
            ChatRunLockService(self.db_path)
        )
        restarted_resolver = _StaticDocumentResolver(
            all_file_names=("changed-after-acceptance.pdf",)
        )
        restarted_factory = FakeChatConversationFactory(
            stream_contents=("answer",)
        )
        restarted_executor = SynchronousChatRunExecutor(
            store=restarted_store,
            chat_commands=restarted_commands,
            conversation_factory=restarted_factory,
            document_resolver=restarted_resolver,
        )

        events = list(restarted_executor.execute_chat_run(prepared.run_id))

        self.assertEqual(
            ["chatInfo", "textChunk", "done"],
            [item.event_type for item in events],
        )
        self.assertEqual(0, restarted_resolver.resolve_all_available_calls)
        self.assertEqual([], restarted_resolver.resolve_many_calls)
        self.assertEqual(
            ("document:alpha.pdf",),
            restarted_factory.ports[0].stream_message_calls[0][2],
        )
        user_message = next(
            item
            for item in restarted_store.messages.list_by_chat(
                prepared.conversation_id
            )
            if item.role == MESSAGE_ROLE_USER
        )
        self.assertEqual((), user_message.files)

    def test_restarted_executor_recovers_frozen_architecture_scope_from_sqlite(
        self,
    ) -> None:
        """类别运行受理后重建进程时，只凭 run_id 恢复已冻结的有效范围。"""

        resolver = _ArchitectureDocumentResolver(
            architecture_file_names=("alpha.pdf",)
        )
        identity = _weaponry_identity(30011, 71)
        prepared = self._executor(resolver).prepare_chat_run(
            identity=identity,
            message="question",
            scope_selector=ChatScopeSelector.for_architecture(71),
        )

        # 新进程的目录解析器被设置为“禁止调用”：若执行阶段试图根据当前类别
        # 重新解析文件，本测试会立即失败，从而证明 Worker 仅消费持久化快照。
        restarted_store = ChatStore(self.db_path)
        restarted_commands = ChatCommandService(
            ChatRunLockService(self.db_path)
        )
        restarted_resolver = _ArchitectureDocumentResolver(
            architecture_file_names=("changed-after-acceptance.pdf",),
            fail_if_architecture_called=True,
        )
        restarted_factory = FakeChatConversationFactory(
            stream_contents=("answer",)
        )
        restarted_executor = SynchronousChatRunExecutor(
            store=restarted_store,
            chat_commands=restarted_commands,
            conversation_factory=restarted_factory,
            document_resolver=restarted_resolver,
        )

        events = list(restarted_executor.execute_chat_run(prepared.run_id))

        self.assertEqual(
            ["chatInfo", "textChunk", "source_snapshot", "done"],
            [item.event_type for item in events],
        )
        self.assertEqual([], restarted_resolver.resolve_architecture_calls)
        self.assertEqual(
            ("document:alpha.pdf",),
            restarted_factory.ports[0].stream_message_calls[0][2],
        )
        user_message = next(
            item
            for item in restarted_store.messages.list_by_chat(
                prepared.conversation_id
            )
            if item.role == MESSAGE_ROLE_USER
        )
        self.assertEqual(71, user_message.architecture_id)
        self.assertEqual((), user_message.files)

    def test_architecture_scope_ignores_extra_remote_workspace_documents(
        self,
    ) -> None:
        """远端 Workspace 多出的文件不得被静默加入类别对话的模型范围。"""

        resolver = _ArchitectureDocumentResolver(
            architecture_file_names=("alpha.pdf",)
        )
        factory = FakeChatConversationFactory(stream_contents=("answer",))
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            document_resolver=resolver,
        )
        selector = ChatScopeSelector.for_architecture(72)
        identity = _weaponry_identity(30012, 72)
        first = executor.prepare_chat_run(
            identity=identity,
            message="first",
            scope_selector=selector,
        )
        list(executor.execute_chat_run(first.run_id))

        # 模拟供应商 Workspace 被其他流程额外挂入文档。该文档真实存在于远端测试
        # 替身中，但不属于本 chatId 的持久化 Scope Revision。
        session = self.store.sessions.get(first.conversation_id)
        self.assertIsNotNone(session)
        assert session is not None
        with factory.create() as conversation:
            conversation.attach_documents(
                ChatSessionRefs(
                    context_ref=session.workspace_ref,
                    conversation_ref=session.thread_ref,
                ),
                (
                    ChatDocumentRef(
                        document_ref="document:beta.pdf",
                        external_location="custom-documents/beta.pdf.json",
                    ),
                ),
            )

        second = executor.prepare_chat_run(
            identity=identity,
            message="reuse",
            scope_selector=selector,
        )
        list(executor.execute_chat_run(second.run_id))

        self.assertEqual([72], resolver.resolve_architecture_calls)
        self.assertEqual(
            ("document:alpha.pdf",),
            factory.ports[2].stream_message_calls[0][2],
        )
        current_scope = self.store.scopes.get_current_revision(
            first.conversation_id
        )
        self.assertIsNotNone(current_scope)
        assert current_scope is not None
        self.assertEqual(
            ("alpha.pdf",),
            tuple(item.file_name for item in current_scope.members),
        )

    def test_architecture_execution_logs_do_not_leak_scope_or_body(self) -> None:
        """类别对话日志只保留计数和内部审计键，不输出业务身份、来源或正文。"""

        secret_file_name = "private-contract.pdf"
        secret_original_name = f"{secret_file_name}.original"
        secret_document_ref = f"document:{secret_file_name}"
        secret_location = f"custom-documents/{secret_file_name}.json"
        secret_message = "confidential-message-body"
        secret_chunk = " confidential-source-chunk\r\nΩ "
        secret_user_id = 9_007_199_254_740_991
        resolver = _ArchitectureDocumentResolver(
            architecture_file_names=(secret_file_name,)
        )
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=FakeChatConversationFactory(
                stream_contents=("answer",),
                stream_sources=(
                    ChatSourceEvidence(
                        content=secret_chunk,
                        structured_source_key=secret_location,
                    ),
                ),
            ),
            document_resolver=resolver,
        )

        with self.assertLogs("app.modules.chat", level="INFO") as captured:
            prepared = executor.prepare_chat_run(
                identity=WeaponryChatIdentity(
                    user_id=secret_user_id,
                    architecture_id=73,
                ),
                message=secret_message,
                scope_selector=ChatScopeSelector.for_architecture(73),
            )
            list(executor.execute_chat_run(prepared.run_id))

        combined_logs = "\n".join(captured.output)
        self.assertIn("requested_architecture_id=73", combined_logs)
        self.assertIn("lease_token=", combined_logs)
        self.assertNotIn(str(secret_user_id), combined_logs)
        self.assertNotIn(secret_file_name, combined_logs)
        self.assertNotIn(secret_original_name, combined_logs)
        self.assertNotIn(secret_document_ref, combined_logs)
        self.assertNotIn(secret_location, combined_logs)
        self.assertNotIn(secret_message, combined_logs)
        self.assertNotIn(secret_chunk, combined_logs)

    def test_workspace_bindings_accumulate_but_explicit_scope_replaces_model_range(
        self,
    ) -> None:
        """Workspace 保留旧绑定，但模型范围只能来自当前 Scope Revision。"""

        resolver = _StaticDocumentResolver()
        factory = FakeChatConversationFactory(stream_contents=("answer",))
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            document_resolver=resolver,
        )
        first = executor.prepare_chat_run(
            identity=_identity("30002"),
            message="first",
            file_names=("alpha.pdf", "beta.pdf"),
        )
        list(executor.execute_chat_run(first.run_id))
        second = executor.prepare_chat_run(
            identity=_identity("30002"),
            message="replace",
            file_names=("beta.pdf",),
        )
        list(executor.execute_chat_run(second.run_id))
        third = executor.prepare_chat_run(
            identity=_identity("30002"),
            message="reuse",
            file_names=(),
        )
        list(executor.execute_chat_run(third.run_id))

        self.assertEqual(
            ("alpha.pdf", "beta.pdf"),
            tuple(
                item.file_name
                for item in self.store.document_bindings.list_current_by_chat(
                    first.conversation_id
                )
            ),
        )
        self.assertEqual(
            ("document:beta.pdf",),
            factory.ports[1].stream_message_calls[0][2],
        )
        self.assertEqual(
            ("document:beta.pdf",),
            factory.ports[2].stream_message_calls[0][2],
        )
        self.assertEqual([], factory.ports[1].attach_document_calls)
        self.assertEqual([], factory.ports[2].attach_document_calls)
        user_messages = tuple(
            item
            for item in self.store.messages.list_by_chat(
                first.conversation_id
            )
            if item.role == MESSAGE_ROLE_USER
        )
        self.assertEqual(
            ("alpha.pdf", "beta.pdf"),
            tuple(item.file_name for item in user_messages[0].files),
        )
        self.assertEqual(
            ("beta.pdf",),
            tuple(item.file_name for item in user_messages[1].files),
        )
        self.assertEqual((), user_messages[2].files)

    def test_failed_explicit_run_still_becomes_next_empty_effective_scope(
        self,
    ) -> None:
        """显式范围一经受理即推进 Head，执行失败不得回退到旧范围。"""

        resolver = _StaticDocumentResolver(all_file_names=("alpha.pdf",))
        factory = FakeChatConversationFactory(stream_contents=("answer",))
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            document_resolver=resolver,
        )
        first = executor.prepare_chat_run(
            identity=_identity("30003"),
            message="first",
            file_names=(),
        )
        list(executor.execute_chat_run(first.run_id))
        failed = executor.prepare_chat_run(
            identity=_identity("30003"),
            message="replace",
            file_names=("beta.pdf",),
        )
        self.commands.fail_chat_run(
            run_id=failed.run_id,
            error_message="injected failure before execution",
        )

        retry = executor.prepare_chat_run(
            identity=_identity("30003"),
            message="reuse",
            file_names=(),
        )
        retry_input = self.store.run_inputs.get(retry.run_id)
        self.assertIsNotNone(retry_input)
        assert retry_input is not None
        self.assertEqual(
            ("beta.pdf",),
            tuple(item.file_name for item in retry_input.files),
        )
        self.assertEqual((), retry_input.requested_files)
        self.assertEqual("active_scope_reuse", retry_input.selection_mode)
        events = list(executor.execute_chat_run(retry.run_id))
        self.assertEqual("done", events[-1].event_type)
        self.assertEqual(
            ("document:beta.pdf",),
            factory.ports[-1].stream_message_calls[0][2],
        )

    def test_later_empty_run_reuses_binding_heads_without_reattach(self) -> None:
        """既有会话空数组只复用 current heads，不吸收新目录文件。"""

        resolver = _StaticDocumentResolver(all_file_names=("alpha.pdf",))
        factory = FakeChatConversationFactory(stream_contents=("answer",))
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            document_resolver=resolver,
        )
        first = executor.prepare_chat_run(
            identity=_identity("20004"),
            message="first",
            file_names=(),
        )
        first_events = list(executor.execute_chat_run(first.run_id))
        resolver.all_file_names = ("alpha.pdf", "new-beta.pdf")

        second = executor.prepare_chat_run(
            identity=_identity("20004"),
            message="second",
            file_names=(),
        )
        second_events = list(executor.execute_chat_run(second.run_id))

        second_input = self.store.run_inputs.get(second.run_id)
        self.assertIsNotNone(second_input)
        assert second_input is not None
        self.assertEqual(
            ("alpha.pdf",),
            tuple(item.file_name for item in second_input.files),
        )
        self.assertEqual((), second_input.requested_files)
        self.assertEqual("active_scope_reuse", second_input.selection_mode)
        self.assertEqual(1, resolver.resolve_all_available_calls)
        self.assertTrue(first_events[0].data["isNewChat"])
        self.assertFalse(second_events[0].data["isNewChat"])
        self.assertEqual(2, len(factory.ports))
        self.assertEqual([], factory.ports[1].attach_document_calls)
        self.assertEqual(
            ("document:alpha.pdf",),
            factory.ports[1].stream_message_calls[0][2],
        )
        self.assertEqual(
            ("alpha.pdf",),
            tuple(
                item.file_name
                for item in self.store.document_bindings.list_current_by_chat(
                    first.conversation_id
                )
            ),
        )
        user_messages = [
            item
            for item in self.store.messages.list_by_chat(
                first.conversation_id
            )
            if item.role == MESSAGE_ROLE_USER
        ]
        self.assertEqual((), user_messages[0].files)
        self.assertEqual((), user_messages[1].files)

    def test_empty_default_range_skips_attach_and_streams_without_documents(
        self,
    ) -> None:
        """空知识库首次会话不调用绑定，但仍通过同一流式执行链完成。"""

        resolver = _StaticDocumentResolver()
        factory = FakeChatConversationFactory(stream_contents=("answer",))
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            document_resolver=resolver,
        )
        prepared = executor.prepare_chat_run(
            identity=_identity("20005"),
            message="free question",
            file_names=(),
        )

        events = list(executor.execute_chat_run(prepared.run_id))

        self.assertEqual(
            ["chatInfo", "textChunk", "done"],
            [event.event_type for event in events],
        )
        self.assertTrue(events[0].data["isNewChat"])
        self.assertEqual(1, len(factory.ports))
        self.assertEqual([], factory.ports[0].attach_document_calls)
        self.assertEqual((), factory.ports[0].stream_message_calls[0][2])
        self.assertEqual(
            (),
            self.store.document_bindings.list_current_by_chat(
                prepared.conversation_id
            ),
        )
        messages = self.store.messages.list_by_chat(prepared.conversation_id)
        self.assertEqual((), messages[0].files)
        self.assertEqual(
            ["thread", "workspace"],
            sorted(
                item.resource_type
                for item in self.store.resource_leases.list_by_chat(
                    prepared.conversation_id
                )
            ),
        )

    def test_acceptance_freezes_input_before_stream_and_activates_resource_leases(self) -> None:
        factory = FakeChatConversationFactory(stream_contents=("answer",))
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            document_resolver=self.resolver,
        )
        prepared = executor.prepare_chat_run(
            identity=_identity("10002"),
            message="question",
            file_names=("hash-a.pdf",),
        )

        accepted_input = self.store.run_inputs.get(prepared.run_id)
        self.assertIsNotNone(accepted_input)
        assert accepted_input is not None
        self.assertEqual("question", accepted_input.message)
        self.assertEqual("document:hash-a.pdf", accepted_input.files[0].document_ref)

        events = list(executor.execute_chat_run(prepared.run_id))

        session = self.store.sessions.get(prepared.conversation_id)
        documents = self.store.document_bindings.list_current_by_chat(
            prepared.conversation_id
        )
        leases = self.store.resource_leases.list_by_chat(
            prepared.conversation_id
        )
        self.assertEqual(["chatInfo", "textChunk", "done"], [event.event_type for event in events])
        self.assertIsNotNone(session)
        assert session is not None
        self.assertTrue(session.workspace_ref)
        self.assertEqual("document:hash-a.pdf", documents[0].document_ref)
        self.assertTrue(all(lease.status == "active" for lease in leases))
        self.assertTrue(all(lease.run_id == prepared.run_id for lease in leases))

    def test_execution_lease_issue_failure_releases_the_accepted_run(self) -> None:
        """未来协调器领取失败时不能让已受理 run 永久占用 chatId。"""
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=FakeChatConversationFactory(),
            document_resolver=self.resolver,
        )

        prepared = executor.prepare_chat_run(
            identity=_identity("chat-lease-issue-failure"),
            message="question",
            file_names=(),
        )
        with patch.object(
            self.commands,
            "issue_execution_lease",
            side_effect=RuntimeError("lease issue failed"),
        ):
            events = list(executor.execute_chat_run(prepared.run_id))

        self.assertEqual(
            (),
            self.store.runs.list_active(prepared.conversation_id),
        )
        self.assertEqual(["error"], [event.event_type for event in events])
        messages = self.store.messages.list_by_chat(prepared.conversation_id)
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_DISCARDED, messages[0].status)
        run = self.store.runs.get(messages[0].run_id)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_FAILED, run.status)

    def test_duplicate_executor_does_not_discard_a_run_claimed_elsewhere(self) -> None:
        """重复投递领取失败时，不能清理已由其他执行器接管的用户消息。"""
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=FakeChatConversationFactory(),
            document_resolver=self.resolver,
        )
        prepared = executor.prepare_chat_run(
            identity=_identity("chat-duplicate-claim"),
            message="question",
            file_names=(),
        )

        # 模拟未来可靠队列重复投递：第一个执行器已经将 accepted 原子领取为
        # 运行已进入执行中状态后，第二个执行器只能报告本次投递未启动，绝不能改写该运行。
        self.commands.issue_execution_lease(run_id=prepared.run_id)
        events = list(executor.execute_chat_run(prepared.run_id))

        self.assertEqual(["error"], [event.event_type for event in events])
        messages = self.store.messages.list_by_chat(prepared.conversation_id)
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_PENDING, messages[0].status)
        run = self.store.runs.get(prepared.run_id)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_RUNNING, run.status)

    def test_compensated_open_failure_closes_unneeded_planned_leases(self) -> None:
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=FakeChatConversationFactory(
                open_conversation_error_message="workspace create failed"
            ),
            document_resolver=self.resolver,
        )
        prepared = executor.prepare_chat_run(
            identity=_identity("chat-open-fail"),
            message="question",
            file_names=(),
        )

        events = list(executor.execute_chat_run(prepared.run_id))

        run = self.store.runs.get(prepared.run_id)
        leases = self.store.resource_leases.list_by_chat(
            prepared.conversation_id
        )
        self.assertEqual(["error"], [event.event_type for event in events])
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_FAILED, run.status)
        self.assertEqual({"closed"}, {lease.status for lease in leases})

    def test_output_limit_ends_run_with_error_without_partial_assistant(self) -> None:
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=FakeChatConversationFactory(
                stream_contents=("too-long",)
            ),
            document_resolver=self.resolver,
            max_output_chars=3,
        )
        prepared = executor.prepare_chat_run(
            identity=_identity("10003"),
            message="question",
            file_names=(),
        )

        events = list(executor.execute_chat_run(prepared.run_id))

        messages = self.store.messages.list_by_chat(prepared.conversation_id)
        run = self.store.runs.get(prepared.run_id)
        self.assertEqual(["chatInfo", "error"], [event.event_type for event in events])
        self.assertEqual([MESSAGE_ROLE_USER], [message.role for message in messages])
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_FAILED, run.status)

    def test_uncompensated_workspace_reference_is_persisted_for_cleanup(self) -> None:
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=FakeChatConversationFactory(
                open_conversation_error_message="thread create failed",
                open_conversation_resource_refs=("workspace-orphan",),
            ),
            document_resolver=self.resolver,
        )
        prepared = executor.prepare_chat_run(
            identity=_identity("chat-orphan-workspace"),
            message="question",
            file_names=(),
        )

        list(executor.execute_chat_run(prepared.run_id))

        workspace_lease = self.store.resource_leases.get(
            f"chat:{prepared.conversation_id}:workspace"
        )
        self.assertIsNotNone(workspace_lease)
        assert workspace_lease is not None
        self.assertEqual("workspace-orphan", workspace_lease.external_ref)
        self.assertEqual("active", workspace_lease.status)
        thread_lease = self.store.resource_leases.get(
            f"chat:{prepared.conversation_id}:thread"
        )
        self.assertIsNotNone(thread_lease)
        assert thread_lease is not None
        self.assertEqual("closed", thread_lease.status)


if __name__ == "__main__":
    unittest.main()
