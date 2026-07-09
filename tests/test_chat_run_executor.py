"""Tests for the file-chat run execution boundary."""

from __future__ import annotations

import tempfile
import unittest

from app.services.chat import (
    ChatCommandService,
    ChatRunLockService,
    ChatRunExecutor,
    ChatRunStreamRequest,
    ChatStreamEvent,
    ChatStore,
    MESSAGE_COMMITTED,
    MESSAGE_PENDING,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    RUN_ABORTED,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    record_chat_run_events,
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


if __name__ == "__main__":
    unittest.main()
