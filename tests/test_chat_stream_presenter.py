"""文件对话领域事件的 SSE 展示层测试。"""

from __future__ import annotations

import unittest

from app.presenters.chat_stream import (
    finalize_chat_run_stream,
    format_sse_event,
    present_chat_stream,
)
from app.services.chat import ChatStreamEvent


class ChatStreamPresenterTests(unittest.TestCase):
    """验证 SSE 格式化逻辑不进入文件对话应用服务。"""

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

    def test_first_terminal_event_wins_and_closes_stream(self) -> None:
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
                run_id="run-terminal",
            )
        )

        self.assertEqual('event: error\ndata: {"error": "boom"}\n\n', body)
        self.assertTrue(closed)

    def test_finalize_closes_client_callback(self) -> None:
        closed = False

        def close_client() -> None:
            nonlocal closed
            closed = True

        body = "".join(
            finalize_chat_run_stream(
                stream=iter([ChatStreamEvent("done", {"chatId": "c-close"})]),
                run_id="run-close",
                on_close=close_client,
            )
        )

        self.assertEqual('event: done\ndata: {"chatId": "c-close"}\n\n', body)
        self.assertTrue(closed)


if __name__ == "__main__":
    unittest.main()
