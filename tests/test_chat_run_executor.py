"""Tests for the file-chat run execution boundary."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from app.ports import ChatDocumentRef
from app.services.chat import (
    ChatCommandService,
    ChatRunEventRepository,
    ChatRunLockService,
    ChatRunExecutor,
    ChatRunEventRecorder,
    ChatRunStreamRequest,
    ChatStreamEvent,
    ChatStore,
    ResolvedChatDocument,
    MESSAGE_COMMITTED,
    MESSAGE_PENDING,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    RUN_ABORTED,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    SynchronousChatRunExecutor,
    record_chat_run_events,
)
from tests.fakes import FakeChatConversationFactory


class _StaticDocumentResolver:
    def resolve_many(self, file_names):
        return tuple(
            ResolvedChatDocument(
                file_name=file_name,
                original_name=f"{file_name}.original",
                document=ChatDocumentRef(
                    document_ref=f"document:{file_name}",
                    external_location=f"custom-documents/{file_name}.json",
                ),
            )
            for file_name in file_names
        )


class ChatRunStreamRequestTests(unittest.TestCase):
    """Validate queue-safe inputs for future chat run executors."""

    def test_request_normalizes_text_and_file_snapshots(self) -> None:
        request = ChatRunStreamRequest(
            run_id=" run-1 ",
            chat_id=" chat-1 ",
            message=" 你好 ",
            file_names=(" hash-a.pdf ",),
            file_original_names=(" 原名.pdf ",),
        )

        self.assertEqual("run-1", request.run_id)
        self.assertEqual("chat-1", request.chat_id)
        self.assertEqual("你好", request.message)
        self.assertEqual(("hash-a.pdf",), request.file_names)
        self.assertEqual(("原名.pdf",), request.file_original_names)

    def test_request_rejects_ambiguous_file_sequences(self) -> None:
        with self.assertRaises(TypeError):
            ChatRunStreamRequest(
                run_id="run-1",
                chat_id="chat-1",
                message="hi",
                file_names="hash-a.pdf",  # type: ignore[arg-type]
                file_original_names=("原名.pdf",),
            )

    def test_request_requires_matching_file_snapshot_lengths(self) -> None:
        with self.assertRaises(ValueError):
            ChatRunStreamRequest(
                run_id="run-1",
                chat_id="chat-1",
                message="hi",
                file_names=("hash-a.pdf",),
                file_original_names=(),
            )

    def test_protocol_accepts_event_stream_executor(self) -> None:
        class FakeExecutor:
            def stream_chat_run(self, request: ChatRunStreamRequest):
                yield ChatStreamEvent("done", {"chatId": request.chat_id})

        self.assertIsInstance(FakeExecutor(), ChatRunExecutor)


class ChatRunEventRecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.db_path = f"{self.tmp}/chat.sqlite3"
        self.store = ChatStore(self.db_path)
        self.commands = ChatCommandService(ChatRunLockService(self.db_path))
        self.store.sessions.create_or_get(chat_id="chat-1")
        self.store.runs.create(run_id="run-1", chat_id="chat-1")
        self.store.runs.mark_running("run-1")
        self.request = ChatRunStreamRequest(
            run_id="run-1",
            chat_id="chat-1",
            message="请总结",
            file_names=("hash-a.pdf",),
            file_original_names=("原名.pdf",),
        )

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def test_done_commits_user_and_complete_assistant_message(self) -> None:
        events = [
            ChatStreamEvent("chatInfo", {"chatId": "chat-1"}),
            ChatStreamEvent("textChunk", {"content": "你好"}),
            ChatStreamEvent("textChunk", {"content": "世界"}),
            ChatStreamEvent("done", {"chatId": "chat-1"}),
        ]

        result = list(
            record_chat_run_events(
                request=self.request,
                events=events,
                store=self.store,
                chat_commands=self.commands,
            )
        )

        messages = self.store.messages.list_by_chat("chat-1")
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
            ChatStreamEvent("chatInfo", {"chatId": "chat-1"}),
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

        messages = self.store.messages.list_by_chat("chat-1")
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
            ChatStreamEvent("aborted", {"chatId": "chat-1"}),
        ]

        list(
            record_chat_run_events(
                request=self.request,
                events=events,
                store=self.store,
                chat_commands=self.commands,
            )
        )

        messages = self.store.messages.list_by_chat("chat-1")
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
                ChatStreamEvent("done", {"chatId": "chat-1"}),
            ],
            store=self.store,
            chat_commands=self.commands,
        )

        first = next(stream)
        self.commands.request_abort(run_id="run-1")
        second = next(stream)

        self.assertEqual(ChatStreamEvent("textChunk", {"content": "第一段"}), first)
        self.assertEqual(ChatStreamEvent("aborted", {"chatId": "chat-1"}), second)
        with self.assertRaises(StopIteration):
            next(stream)
        messages = self.store.messages.list_by_chat("chat-1")
        run = self.store.runs.get("run-1")
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_ROLE_USER, messages[0].role)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_ABORTED, run.status)
        next_run = self.commands.start_chat_run(chat_id="chat-1")
        self.assertNotEqual("run-1", next_run.run_id)
        self.assertEqual(RUN_RUNNING, next_run.status)

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
                    return ChatStreamEvent("done", {"chatId": "chat-1"})
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
                ChatStreamEvent("aborted", {"chatId": "chat-1"}),
            ],
            result,
        )
        messages = self.store.messages.list_by_chat("chat-1")
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

        self.assertEqual([ChatStreamEvent("aborted", {"chatId": "chat-1"})], result)
        messages = self.store.messages.list_by_chat("chat-1")
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

        self.assertEqual([ChatStreamEvent("aborted", {"chatId": "chat-1"})], result)
        messages = self.store.messages.list_by_chat("chat-1")
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

        self.assertEqual([ChatStreamEvent("aborted", {"chatId": "chat-1"})], result)
        messages = self.store.messages.list_by_chat("chat-1")
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
                ChatStreamEvent("done", {"chatId": "chat-1"}),
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

        messages = self.store.messages.list_by_chat("chat-1")
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
                events=[ChatStreamEvent("chatInfo", {"chatId": "chat-1"})],
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
                ChatStreamEvent("done", {"chatId": "chat-1"}),
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
            chat_id="chat-1",
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
                    terminal_event=ChatStreamEvent("done", {"chatId": "chat-1"}),
                )

        user = self.store.messages.list_by_chat("chat-1")[0]
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
        messages = self.store.messages.list_by_chat("chat-1")
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
            ChatStreamEvent("done", {"chatId": "chat-1"}),
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
            chat_id="chat-1",
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
            chat_id="chat-1",
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

        user = self.store.messages.list_by_chat("chat-1")[0]
        run = self.store.runs.get("run-1")
        self.assertEqual(MESSAGE_PENDING, user.status)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_RUNNING, run.status)

    def test_high_frequency_chunks_do_not_write_a_heartbeat_per_chunk(self) -> None:
        events = [
            *(ChatStreamEvent("textChunk", {"content": "x"}) for _ in range(50)),
            ChatStreamEvent("done", {"chatId": "chat-1"}),
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
        self.store = ChatStore(f"{self.tmp}/chat.sqlite3")
        self.commands = ChatCommandService(ChatRunLockService(self.store.db_path))
        self.resolver = _StaticDocumentResolver()

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def test_acceptance_freezes_input_before_stream_and_activates_resource_leases(self) -> None:
        factory = FakeChatConversationFactory(stream_contents=("answer",))
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            document_resolver=self.resolver,
        )
        prepared = executor.prepare_chat_run(
            chat_id="chat-executor",
            message="question",
            file_names=("hash-a.pdf",),
        )

        accepted_input = self.store.run_inputs.get(prepared.run_id)
        self.assertIsNotNone(accepted_input)
        assert accepted_input is not None
        self.assertEqual("question", accepted_input.message)
        self.assertEqual("document:hash-a.pdf", accepted_input.files[0].document_ref)

        events = list(
            record_chat_run_events(
                request=prepared.request,
                events=executor.stream_chat_run(prepared.request),
                store=self.store,
                chat_commands=self.commands,
            )
        )

        session = self.store.sessions.get("chat-executor")
        documents = self.store.documents.list_by_chat("chat-executor")
        leases = self.store.resource_leases.list_by_chat("chat-executor")
        self.assertEqual(["chatInfo", "textChunk", "done"], [event.event_type for event in events])
        self.assertIsNotNone(session)
        assert session is not None
        self.assertTrue(session.workspace_ref)
        self.assertEqual("document:hash-a.pdf", documents[0].document_ref)
        self.assertTrue(all(lease.status == "active" for lease in leases))
        self.assertTrue(all(lease.run_id == prepared.run_id for lease in leases))

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
            chat_id="chat-open-fail",
            message="question",
            file_names=(),
        )

        events = list(
            record_chat_run_events(
                request=prepared.request,
                events=executor.stream_chat_run(prepared.request),
                store=self.store,
                chat_commands=self.commands,
            )
        )

        run = self.store.runs.get(prepared.run_id)
        leases = self.store.resource_leases.list_by_chat("chat-open-fail")
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
            chat_id="chat-output-limit",
            message="question",
            file_names=(),
        )

        events = list(
            record_chat_run_events(
                request=prepared.request,
                events=executor.stream_chat_run(prepared.request),
                store=self.store,
                chat_commands=self.commands,
            )
        )

        messages = self.store.messages.list_by_chat("chat-output-limit")
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
            chat_id="chat-orphan-workspace",
            message="question",
            file_names=(),
        )

        list(
            record_chat_run_events(
                request=prepared.request,
                events=executor.stream_chat_run(prepared.request),
                store=self.store,
                chat_commands=self.commands,
            )
        )

        workspace_lease = self.store.resource_leases.get(
            "chat:chat-orphan-workspace:workspace"
        )
        self.assertIsNotNone(workspace_lease)
        assert workspace_lease is not None
        self.assertEqual("workspace-orphan", workspace_lease.external_ref)
        self.assertEqual("active", workspace_lease.status)
        thread_lease = self.store.resource_leases.get(
            "chat:chat-orphan-workspace:thread"
        )
        self.assertIsNotNone(thread_lease)
        assert thread_lease is not None
        self.assertEqual("closed", thread_lease.status)


if __name__ == "__main__":
    unittest.main()
