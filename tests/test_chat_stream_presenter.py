"""SSE presenter tests for file-chat domain events."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.presenters.chat_stream import (
    finalize_chat_run_stream,
    format_sse_event,
    present_chat_stream,
)
from app.services.chat import ChatStreamEvent


class ChatStreamPresenterTests(unittest.TestCase):
    """Verify that SSE formatting stays outside chat application services."""

    def test_format_sse_event_keeps_chinese_json(self) -> None:
        self.assertEqual(
            'event: textChunk\ndata: {"content": "你好"}\n\n',
            format_sse_event("textChunk", {"content": "你好"}),
        )

    def test_present_chat_stream_formats_domain_events(self) -> None:
        body = "".join(
            present_chat_stream(
                [
                    ChatStreamEvent("chatInfo", {"chatId": "c1", "isNewChat": True}),
                    ChatStreamEvent("textChunk", {"content": "第一段"}),
                    ChatStreamEvent("done", {"chatId": "c1"}),
                ]
            )
        )

        self.assertEqual(
            'event: chatInfo\ndata: {"chatId": "c1", "isNewChat": true}\n\n'
            'event: textChunk\ndata: {"content": "第一段"}\n\n'
            'event: done\ndata: {"chatId": "c1"}\n\n',
            body,
        )

    def test_error_event_marks_run_failed(self) -> None:
        commands = MagicMock()

        body = "".join(
            finalize_chat_run_stream(
                stream=iter([ChatStreamEvent("error", {"error": "boom"})]),
                chat_commands=commands,
                run_id="run-error",
            )
        )

        self.assertEqual('event: error\ndata: {"error": "boom"}\n\n', body)
        commands.heartbeat_chat_run.assert_called_once_with(run_id="run-error")
        commands.fail_chat_run.assert_called_once_with(
            run_id="run-error",
            error_message="chat stream emitted error event",
        )
        commands.complete_chat_run.assert_not_called()
        commands.abort_chat_run.assert_not_called()

    def test_first_terminal_event_wins_and_closes_stream(self) -> None:
        commands = MagicMock()
        closed = False

        def stream():
            nonlocal closed
            try:
                yield ChatStreamEvent("error", {"error": "boom"})
                yield ChatStreamEvent("done", {"chatId": "c1"})
            finally:
                closed = True

        body = "".join(
            finalize_chat_run_stream(
                stream=stream(),
                chat_commands=commands,
                run_id="run-terminal",
            )
        )

        self.assertEqual('event: error\ndata: {"error": "boom"}\n\n', body)
        self.assertTrue(closed)
        commands.fail_chat_run.assert_called_once_with(
            run_id="run-terminal",
            error_message="chat stream emitted error event",
        )
        commands.complete_chat_run.assert_not_called()
        commands.abort_chat_run.assert_not_called()

    def test_aborted_event_marks_run_aborted(self) -> None:
        commands = MagicMock()

        body = "".join(
            finalize_chat_run_stream(
                stream=iter([ChatStreamEvent("aborted", {"chatId": "c-abort"})]),
                chat_commands=commands,
                run_id="run-abort",
            )
        )

        self.assertEqual(
            'event: aborted\ndata: {"chatId": "c-abort"}\n\n',
            body,
        )
        commands.heartbeat_chat_run.assert_called_once_with(run_id="run-abort")
        commands.abort_chat_run.assert_called_once_with(run_id="run-abort")
        commands.complete_chat_run.assert_not_called()
        commands.fail_chat_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
