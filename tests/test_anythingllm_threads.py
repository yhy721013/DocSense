"""AnythingLLM 线程原子客户端与 SSE 回答清理的离线契约测试。"""

from __future__ import annotations

import unittest
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

from app.integrations.anythingllm.errors import AnythingLLMProtocolError
from app.integrations.anythingllm.models import (
    AnythingLLMFinalization,
    AnythingLLMQueryRejection,
    AnythingLLMTextDelta,
)
from app.integrations.anythingllm.threads import AnythingLLMThreadClient
from app.integrations.anythingllm.transport import AnythingLLMTransport, SSEEvent


def _event_stream(*events: SSEEvent) -> Iterator[SSEEvent]:
    """把固定 SSE 事件转换为支持 ``close`` 的生成器。"""
    yield from events


class AnythingLLMThreadClientTests(unittest.TestCase):
    """验证线程标识、问答事件、来源 DTO、历史消息和流式资源释放。"""

    def setUp(self) -> None:
        """创建每个用例独立的 Transport 替身和线程客户端。"""
        self.transport = MagicMock()
        self.client = AnythingLLMThreadClient(self.transport)

    def test_create_thread_normalizes_alias_and_sends_user_id(self) -> None:
        """创建线程应统一 threadSlug 别名，并同时传递请求体和请求头用户标识。"""
        self.transport.post_json.return_value = {
            "thread": {"id": 9, "threadSlug": "thread-a"}
        }

        thread = self.client.create_thread("workspace-a", "会话 A", user_id=7)

        self.assertEqual(thread.id, "9")
        self.assertEqual(thread.slug, "thread-a")
        self.transport.post_json.assert_called_once_with(
            "workspace/workspace-a/thread/new",
            {"name": "会话 A", "userId": 7},
            user_id=7,
        )

    def test_ask_cleans_answer_normalizes_sources_and_omits_empty_files(self) -> None:
        """同步问答应保留原文、清理代码块、统一来源，且默认不发送 files。"""
        marker = "docsense_ref:0123456789abcdef0123456789abcdef"
        raw_answer = '<think>推理</think>```json\n{"summary":"摘要"}\n```'
        self.transport.stream_sse.return_value = _event_stream(
            SSEEvent(
                data=(
                    '{"type":"textResponse","textResponse":'
                    '"<think>推理</think>```json\\n{\\"summary\\":\\"摘要\\"}\\n```",'
                    '"sources":[{"text":"证据",'
                    f'"docSource":"{marker}",'
                    '"sourceDocument":"custom-documents/%E7%A4%BA%E4%BE%8B.json"}],'
                    '"close":true}'
                )
            )
        )

        with self.assertLogs(
            "app.integrations.anythingllm.threads",
            level="INFO",
        ) as captured_logs:
            answer = self.client.ask(
                "workspace-a",
                "thread-a",
                "提取摘要",
                mode="chat",
                user_id=8,
            )

        self.assertEqual(answer.text, '{"summary":"摘要"}')
        self.assertEqual(answer.raw_text, raw_answer)
        self.assertEqual(answer.sources[0].document_ref, "name:示例.json")
        self.assertEqual(answer.sources[0].source_marker, marker)
        request_payload = self.transport.stream_sse.call_args.args[1]
        self.assertNotIn("files", request_payload)
        self.assertEqual(request_payload["userId"], 8)
        self.assertEqual(request_payload["mode"], "chat")
        logs = "\n".join(captured_logs.output)
        self.assertIn("prompt_chars=4", logs)
        self.assertIn("source_count=1", logs)
        self.assertNotIn("提取摘要", logs)
        self.assertNotIn("证据", logs)

    def test_ask_sends_files_only_when_non_empty_values_are_explicit(self) -> None:
        """显式文件 ID 应过滤空值后发送，满足旧调用方的附件问答能力。"""
        self.transport.stream_sse.return_value = _event_stream(
            SSEEvent(
                data=(
                    '{"type":"textResponse","textResponse":"完成",'
                    '"sources":[],"close":true}'
                )
            )
        )

        self.client.ask(
            "workspace-a",
            "thread-a",
            "问题",
            mode="chat",
            document_ids=["doc-1", "", " doc-2 "],
        )

        request_payload = self.transport.stream_sse.call_args.args[1]
        self.assertEqual(request_payload["files"], ["doc-1", "doc-2"])

    def test_ask_sends_canonical_prompt_without_changing_internal_layout(self) -> None:
        """线程请求必须发送公共契约生成的规范 Prompt，而不是自行采用另一套裁剪规则。"""
        self.transport.stream_sse.return_value = _event_stream(
            SSEEvent(
                data=(
                    '{"type":"textResponse","textResponse":"完成",'
                    '"sources":[],"close":true}'
                )
            )
        )

        self.client.ask(
            "workspace-a",
            "thread-a",
            "\r\n第一行\r\n  第二行\r\n",
            mode="query",
        )

        request_payload = self.transport.stream_sse.call_args.args[1]
        self.assertEqual(request_payload["message"], "第一行\n  第二行")

    def test_ask_explicit_query_mode_is_sent_without_files(self) -> None:
        """Document RAG 显式传入 query 时，请求体必须保持该模式且不生成 files。"""
        self.transport.stream_sse.return_value = _event_stream(
            SSEEvent(
                data=(
                    '{"type":"textResponse","textResponse":"完成",'
                    '"sources":[],"close":true}'
                )
            )
        )

        self.client.ask(
            "workspace-a",
            "thread-a",
            "问题",
            mode=" query ",
        )

        request_payload = self.transport.stream_sse.call_args.args[1]
        self.assertEqual("query", request_payload["mode"])
        self.assertNotIn("files", request_payload)

    def test_ask_rejects_unknown_mode_before_http(self) -> None:
        """模式拼写错误不得静默回退为 chat 或被透传到 AnythingLLM。"""
        invalid_modes = ("", "search", "query-mode", None, True)
        for mode in invalid_modes:
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    self.client.ask(
                        "workspace-a",
                        "thread-a",
                        "问题",
                        mode=mode,  # type: ignore[arg-type]
                    )

        self.transport.stream_sse.assert_not_called()

    def test_ask_rejects_accumulated_chunks_when_final_event_is_missing(self) -> None:
        """没有合法终态的半截文本不能伪装成完整可审计回答。"""
        self.transport.stream_sse.return_value = _event_stream(
            SSEEvent(
                data='{"type":"textResponseChunk","textResponse":"你"}'
            ),
            SSEEvent(
                data='{"type":"textResponseChunk","textResponse":"好"}'
            ),
        )

        with self.assertRaises(AnythingLLMProtocolError):
            self.client.ask(
                "workspace-a",
                "thread-a",
                "问候",
                mode="chat",
            )

    def test_ask_rejects_stream_without_final_event_or_text(self) -> None:
        """只有心跳或无效 JSON 的流不能被误判为成功回答。"""
        self.transport.stream_sse.return_value = _event_stream(
            SSEEvent(data="not-json"),
            SSEEvent(data="[DONE]"),
        )

        with self.assertRaises(AnythingLLMProtocolError):
            self.client.ask("workspace-a", "thread-a", "问题", mode="chat")

    def test_ask_accepts_consecutive_data_lines_without_blank_separator(self) -> None:
        """真实 Transport 合并非标准连续 data 行后，线程层仍应逐行恢复 JSON 消息。"""
        session = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.iter_lines.return_value = iter(
            [
                'data: {"type":"textResponseChunk","textResponse":"你"}',
                (
                    'data: {"type":"textResponse","textResponse":"你好",'
                    '"sources":[],"close":true}'
                ),
            ]
        )
        session.request.return_value = response
        transport = AnythingLLMTransport(
            base_url="http://anythingllm.local/api/v1",
            api_key="test-key",
            timeout=30,
            session=session,
        )
        client = AnythingLLMThreadClient(transport)
        try:
            answer = client.ask(
                "workspace-a",
                "thread-a",
                "问候",
                mode="chat",
            )
        finally:
            transport.close()

        self.assertEqual(answer.text, "你好")
        self.assertEqual(answer.raw_text, "你好")
        response.close.assert_called_once_with()

    def test_ask_accepts_raw_json_lines_without_sse_prefix(self) -> None:
        """Thread 显式兼容旧接口返回的 NDJSON 行，但不改变 Transport 默认 SSE 语义。"""
        session = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.iter_lines.return_value = iter(
            [
                '{"type":"textResponseChunk","textResponse":"你"}',
                (
                    '{"type":"textResponse","textResponse":"你好",'
                    '"sources":[],"close":true}'
                ),
            ]
        )
        session.request.return_value = response
        transport = AnythingLLMTransport(
            base_url="http://anythingllm.local/api/v1",
            api_key="test-key",
            timeout=30,
            session=session,
        )
        client = AnythingLLMThreadClient(transport)
        try:
            answer = client.ask(
                "workspace-a",
                "thread-a",
                "问候",
                mode="chat",
            )
        finally:
            transport.close()

        self.assertEqual(answer.text, "你好")
        response.close.assert_called_once_with()

    def test_ask_preserves_standard_multiline_json_event(self) -> None:
        """兼容逐行消息时仍应优先解析标准 SSE 中跨 data 行组成的单个 JSON。"""
        self.transport.stream_sse.return_value = _event_stream(
            SSEEvent(
                data=(
                    "{\n"
                    '  "type": "textResponse",\n'
                    '  "textResponse": "完成",\n'
                    '  "sources": [],\n'
                    '  "close": true\n'
                    "}"
                )
            )
        )

        answer = self.client.ask(
            "workspace-a",
            "thread-a",
            "问题",
            mode="chat",
        )

        self.assertEqual(answer.text, "完成")

    def test_stream_yields_chunks_and_closes_inner_generator_early(self) -> None:
        """外层消费者提前关闭时，应通过 closing 立即关闭 Transport SSE 生成器。"""
        closed: list[bool] = []

        def events():
            try:
                yield SSEEvent(
                    data='{"type":"textResponseChunk","textResponse":"第一段"}'
                )
                yield SSEEvent(
                    data='{"type":"textResponseChunk","textResponse":"第二段"}'
                )
            finally:
                closed.append(True)

        self.transport.stream_sse.return_value = events()
        stream = self.client.stream(
            "workspace-a",
            "thread-a",
            "问题",
            mode="query",
        )

        self.assertEqual(next(stream).content, "第一段")
        stream.close()

        self.assertEqual(closed, [True])

    def test_stream_fixture_reads_finalization_after_close_text_chunk(self) -> None:
        """v1.15.0 的文本 close 不能吞掉后续四条来源。"""

        fixture_path = (
            Path(__file__).parent
            / "contracts"
            / "anythingllm_v1_15_stream.jsonl"
        )
        rows = [
            json.loads(line)
            for line in fixture_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        normal = [row["event"] for row in rows if row["scenario"] == "normal_sources"]
        self.transport.stream_sse.return_value = _event_stream(
            *(SSEEvent(data=json.dumps(item, ensure_ascii=False)) for item in normal)
        )

        events = list(
            self.client.stream(
                "workspace-a",
                "thread-a",
                "问题",
                mode="query",
            )
        )

        self.assertIsInstance(events[0], AnythingLLMTextDelta)
        self.assertEqual("第一段", events[0].content)
        self.assertIsInstance(events[1], AnythingLLMFinalization)
        self.assertEqual(4, len(events[1].sources))
        self.assertEqual(
            ["来源一", "来源二", "来源三", "来源四"],
            [item.content for item in events[1].sources],
        )

    def test_stream_fixture_models_query_no_context_as_distinct_terminal(self) -> None:
        fixture_path = (
            Path(__file__).parent
            / "contracts"
            / "anythingllm_v1_15_stream.jsonl"
        )
        row = next(
            json.loads(line)
            for line in fixture_path.read_text(encoding="utf-8").splitlines()
            if '"query_no_context"' in line
        )
        self.transport.stream_sse.return_value = _event_stream(
            SSEEvent(data=json.dumps(row["event"], ensure_ascii=False))
        )

        events = list(
            self.client.stream(
                "workspace-a",
                "thread-a",
                "问题",
                mode="query",
            )
        )

        self.assertEqual(1, len(events))
        self.assertIsInstance(events[0], AnythingLLMQueryRejection)
        self.assertNotIsInstance(events[0], AnythingLLMFinalization)

    def test_stream_source_content_is_lossless_and_strictly_typed(self) -> None:
        marker = "docsense_ref:0123456789abcdef0123456789abcdef"
        original = (
            "<document_metadata>\nsourceDocument: internal.pdf\n"
            "</document_metadata>\n\n  第一行\r\n第二行 e\u0301 与 é  "
        )
        payload = {
            "type": "finalizeResponseStream",
            "sources": [{"text": original, "docSource": marker}],
            "close": True,
        }
        self.transport.stream_sse.return_value = _event_stream(
            SSEEvent(data=json.dumps(payload, ensure_ascii=False))
        )
        terminal = list(
            self.client.stream("workspace-a", "thread-a", "问题", mode="chat")
        )[0]
        self.assertEqual(original, terminal.sources[0].content)

        payload["sources"][0]["text"] = 123
        self.transport.stream_sse.return_value = _event_stream(
            SSEEvent(data=json.dumps(payload, ensure_ascii=False))
        )
        with self.assertRaises(AnythingLLMProtocolError):
            list(self.client.stream("workspace-a", "thread-a", "问题", mode="chat"))

    def test_stream_rejects_missing_or_duplicate_finalization(self) -> None:
        self.transport.stream_sse.return_value = _event_stream(
            SSEEvent(data='{"type":"textResponseChunk","textResponse":"片段","close":true}')
        )
        with self.assertRaises(AnythingLLMProtocolError):
            list(self.client.stream("workspace-a", "thread-a", "问题", mode="chat"))

        final = SSEEvent(data='{"type":"finalizeResponseStream","sources":[],"close":true}')
        self.transport.stream_sse.return_value = _event_stream(final, final)
        with self.assertRaises(AnythingLLMProtocolError):
            list(self.client.stream("workspace-a", "thread-a", "问题", mode="chat"))

        # 终态后的文本不能被客户端悄悄重排到 Finalization 之前，否则公开 SSE
        # 看似合法，实际却不再对应供应商原始事件顺序。
        trailing_text = SSEEvent(
            data='{"type":"textResponseChunk","textResponse":"越界片段"}'
        )
        self.transport.stream_sse.return_value = _event_stream(
            final,
            trailing_text,
        )
        with self.assertRaisesRegex(
            AnythingLLMProtocolError,
            "终态后返回额外事件",
        ):
            list(self.client.stream("workspace-a", "thread-a", "问题", mode="chat"))

    def test_stream_rejects_conflicting_structured_source_keys(self) -> None:
        payload = {
            "type": "finalizeResponseStream",
            "sources": [{
                "text": "证据",
                "docSource": "docsense_ref:0123456789abcdef0123456789abcdef",
                "metadata": {
                    "docSource": "docsense_ref:fedcba9876543210fedcba9876543210"
                },
            }],
            "close": True,
        }
        self.transport.stream_sse.return_value = _event_stream(
            SSEEvent(data=json.dumps(payload))
        )
        with self.assertRaises(AnythingLLMProtocolError):
            list(self.client.stream("workspace-a", "thread-a", "问题", mode="chat"))

    def test_stream_rejects_unknown_mode_before_creating_generator(self) -> None:
        """流式入口必须复用同步问答的相同模式白名单。"""
        stream = self.client.stream(
            "workspace-a",
            "thread-a",
            "问题",
            mode="invalid",
        )

        with self.assertRaises(ValueError):
            next(stream)

        self.transport.stream_sse.assert_not_called()

    def test_history_requires_object_items_and_returns_mappings(self) -> None:
        """历史响应必须是对象数组，合法记录保持字段内容返回。"""
        self.transport.get_json.return_value = {
            "history": [{"role": "user", "content": "你好"}]
        }

        history = self.client.history("workspace-a", "thread-a", user_id=9)

        self.assertEqual(history, [{"role": "user", "content": "你好"}])
        self.transport.get_json.assert_called_once_with(
            "workspace/workspace-a/thread/thread-a/chats",
            user_id=9,
        )

    def test_delete_thread_uses_status_only_transport_contract(self) -> None:
        """线程删除应忽略不稳定响应正文，只依赖 Transport 的状态码校验。"""

        self.client.delete_thread("workspace-a", "thread-a", user_id=10)

        self.transport.delete_status.assert_called_once_with(
            "workspace/workspace-a/thread/thread-a",
            user_id=10,
        )


if __name__ == "__main__":
    unittest.main()
