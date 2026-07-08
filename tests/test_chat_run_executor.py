"""Tests for the file-chat run execution boundary."""

from __future__ import annotations

import unittest

from app.services.chat import ChatRunExecutor, ChatRunStreamRequest, ChatStreamEvent


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


if __name__ == "__main__":
    unittest.main()
