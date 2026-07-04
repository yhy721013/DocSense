"""AnythingLLM 线程接口及 SSE 回答清理的原子客户端。

该客户端负责线程创建、删除、同步问答、流式问答和历史消息读取。HTTP 与 SSE 分帧由
``AnythingLLMTransport`` 统一处理；本层只解释线程 API 的 JSON 事件语义，并把来源字段
转换为稳定 DTO。
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import closing
from typing import Any, Iterator, Mapping, Optional, Sequence
from urllib.parse import quote

from app.integrations.anythingllm.errors import AnythingLLMProtocolError
from app.integrations.anythingllm.models import (
    AnythingLLMAnswer,
    AnythingLLMSource,
    AnythingLLMThread,
    json_text,
    require_mapping,
    require_sequence,
)
from app.integrations.anythingllm.transport import AnythingLLMTransport, SSEEvent


logger = logging.getLogger(__name__)


class AnythingLLMThreadClient:
    """提供不依赖工作区客户端或文档客户端的线程原子操作。"""

    _ALLOWED_CHAT_MODES = frozenset({"chat", "query"})
    """AnythingLLM 线程 API 允许的模式白名单。"""

    def __init__(self, transport: AnythingLLMTransport) -> None:
        """绑定任务级传输对象，但不拥有其关闭职责。"""
        self._transport = transport

    def create_thread(
        self,
        workspace_slug: str,
        name: str,
        *,
        user_id: int | None = None,
    ) -> AnythingLLMThread:
        """在指定工作区创建线程并归一化线程标识字段。"""
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("线程 name 不能为空")
        payload: dict[str, Any] = {"name": normalized_name}
        if user_id is not None:
            payload["userId"] = user_id
        path = self._thread_collection_path(workspace_slug)
        body = self._transport.post_json(path, payload, user_id=user_id)
        response = require_mapping(body, context="创建线程响应")
        if response.get("error"):
            raise AnythingLLMProtocolError("AnythingLLM 明确拒绝创建线程")
        thread = AnythingLLMThread.from_payload(response.get("thread") or response)
        logger.info(
            "AnythingLLM 线程创建完成: workspace_slug=%s thread_slug=%s "
            "thread_id=%s has_user_context=%s",
            workspace_slug,
            thread.slug,
            thread.id,
            user_id is not None,
        )
        return thread

    def delete_thread(
        self,
        workspace_slug: str,
        thread_slug: str,
        *,
        user_id: int | None = None,
    ) -> None:
        """删除指定线程，仅以 HTTP 状态码判断成功并忽略不稳定响应正文。"""
        path = self._thread_path(workspace_slug, thread_slug)
        self._transport.delete_status(path, user_id=user_id)
        logger.info(
            "AnythingLLM 线程删除完成: workspace_slug=%s thread_slug=%s "
            "has_user_context=%s",
            workspace_slug,
            thread_slug,
            user_id is not None,
        )

    def ask(
        self,
        workspace_slug: str,
        thread_slug: str,
        prompt: str,
        *,
        mode: str,
        user_id: int | None = None,
        document_ids: Optional[Sequence[str]] = None,
    ) -> AnythingLLMAnswer:
        """向线程发送提示词并汇总 SSE 为一次完整回答。

        ``mode`` 是必填关键字参数，调用方必须明确选择 ``chat`` 或 ``query``，不能依赖
        原子 Client 猜测业务知识边界。

        仅当 ``document_ids`` 显式包含非空值时才发送 ``files`` 字段。纯方案 B 不传该
        参数，使模型是否使用目标文档只能由工作区 Pin 和后续 sources 校验决定。

        回答中的 ``<think>`` 与 Markdown JSON 代码块只从 ``text`` 中移除，原始文本原样
        保存在 ``raw_text``，以便审计记录模型真实输出。
        """
        normalized_prompt = str(prompt or "").strip()
        if not normalized_prompt:
            raise ValueError("prompt 不能为空")
        payload = self._chat_payload(
            normalized_prompt,
            mode=mode,
            document_ids=document_ids,
            user_id=user_id,
        )
        path = f"{self._thread_path(workspace_slug, thread_slug)}/chat"
        logger.info(
            "开始 AnythingLLM 线程问答: workspace_slug=%s thread_slug=%s "
            "mode=%s prompt_chars=%d file_count=%d has_user_context=%s",
            workspace_slug,
            thread_slug,
            payload["mode"],
            len(normalized_prompt),
            len(payload.get("files", [])),
            user_id is not None,
        )

        chunks: list[str] = []
        final_payload: Mapping[str, Any] | None = None
        # ask 在收到最终事件后会提前结束 SSE 消费；closing 确保此时立即释放响应。
        with closing(
            self._transport.stream_sse(
                path,
                payload,
                user_id=user_id,
                allow_json_lines=True,
            )
        ) as events:
            for event in events:
                for decoded in self._decode_sse_events(event):
                    event_type = str(decoded.get("type") or event.event or "")
                    text_value = decoded.get("textResponse")
                    if event_type == "textResponseChunk" and isinstance(text_value, str):
                        chunks.append(text_value)
                    if decoded.get("close") or event_type == "textResponse":
                        final_payload = decoded
                        break
                if final_payload is not None:
                    break

        if final_payload is None:
            if not chunks:
                raise AnythingLLMProtocolError(
                    "AnythingLLM 线程问答未返回最终事件或文本片段"
                )
            final_payload = {"textResponse": "".join(chunks), "sources": []}

        raw_text = self._answer_text(final_payload.get("textResponse"), chunks=chunks)
        cleaned_text = self.clean_answer(raw_text)
        if not cleaned_text:
            raise AnythingLLMProtocolError("AnythingLLM 线程问答返回空文本")
        sources_value = require_sequence(
            final_payload.get("sources", []),
            context="线程回答 sources 字段",
        )
        sources = tuple(AnythingLLMSource.from_payload(item) for item in sources_value)
        unresolved_source_count = sum(not source.document_ref for source in sources)
        marked_source_count = sum(bool(source.source_marker) for source in sources)
        if unresolved_source_count:
            logger.warning(
                "AnythingLLM 线程回答存在无法生成 legacy 展示引用的来源: workspace_slug=%s "
                "thread_slug=%s source_count=%d unresolved_source_count=%d",
                workspace_slug,
                thread_slug,
                len(sources),
                unresolved_source_count,
            )
        logger.info(
            "AnythingLLM 线程问答完成: workspace_slug=%s thread_slug=%s "
            "text_chars=%d raw_text_chars=%d source_count=%d marked_source_count=%d",
            workspace_slug,
            thread_slug,
            len(cleaned_text),
            len(raw_text),
            len(sources),
            marked_source_count,
        )
        return AnythingLLMAnswer(
            text=cleaned_text,
            raw_text=raw_text,
            sources=sources,
        )

    def stream(
        self,
        workspace_slug: str,
        thread_slug: str,
        message: str,
        *,
        mode: str,
        user_id: int | None = None,
        document_ids: Optional[Sequence[str]] = None,
    ) -> Iterator[str]:
        """向线程发送流式消息并依次产出可显示的文本片段。

        ``mode`` 与同步问答一致为必填关键字参数，确保流式和非流式接口不会产生不同的
        隐式默认行为。

        本方法保持 AnythingLLM 的事件顺序，不拼接或清理文本。调用方提前结束消费时应
        关闭生成器，关闭动作会继续传递到传输层并释放上游响应。
        """
        normalized_message = str(message or "").strip()
        if not normalized_message:
            raise ValueError("流式消息不能为空")
        payload = self._chat_payload(
            normalized_message,
            mode=mode,
            document_ids=document_ids,
            user_id=user_id,
        )
        path = f"{self._thread_path(workspace_slug, thread_slug)}/stream-chat"
        emitted_chunk_count = 0
        final_event_received = False
        logger.debug(
            "开始 AnythingLLM 流式线程问答: workspace_slug=%s thread_slug=%s "
            "mode=%s message_chars=%d file_count=%d",
            workspace_slug,
            thread_slug,
            payload["mode"],
            len(normalized_message),
            len(payload.get("files", [])),
        )
        try:
            with closing(
                self._transport.stream_sse(
                    path,
                    payload,
                    user_id=user_id,
                    allow_json_lines=True,
                )
            ) as events:
                for event in events:
                    for decoded in self._decode_sse_events(event):
                        event_type = str(decoded.get("type") or event.event or "")
                        text_value = decoded.get("textResponse")
                        if decoded.get("close") or event_type == "textResponse":
                            final_event_received = True
                            if isinstance(text_value, str) and text_value:
                                emitted_chunk_count += 1
                                yield text_value
                            return
                        if event_type == "textResponseChunk" and isinstance(text_value, str):
                            emitted_chunk_count += 1
                            yield text_value
        finally:
            logger.debug(
                "结束 AnythingLLM 流式线程问答: workspace_slug=%s thread_slug=%s "
                "emitted_chunk_count=%d final_event_received=%s",
                workspace_slug,
                thread_slug,
                emitted_chunk_count,
                final_event_received,
            )

    def history(
        self,
        workspace_slug: str,
        thread_slug: str,
        *,
        user_id: int | None = None,
    ) -> list[Mapping[str, Any]]:
        """读取线程历史，并确保每条历史记录都是 JSON 对象。"""
        path = f"{self._thread_path(workspace_slug, thread_slug)}/chats"
        body = self._transport.get_json(path, user_id=user_id)
        response = require_mapping(body, context="线程历史响应")
        history_value = require_sequence(response.get("history", []), context="history 字段")
        history = [require_mapping(item, context="历史消息") for item in history_value]
        logger.debug(
            "获取 AnythingLLM 线程历史完成: workspace_slug=%s thread_slug=%s "
            "message_count=%d",
            workspace_slug,
            thread_slug,
            len(history),
        )
        return history

    @staticmethod
    def clean_answer(value: str) -> str:
        """移除模型思维标记和可选 Markdown 代码围栏。

        优先取最后一个 ``</think>`` 之后的内容，以兼容模型在思维块之前或内部产生其他
        文本。代码围栏允许标注 ``json``，也兼容上游截断导致缺少闭合围栏的情况。
        """
        cleaned = value.rsplit("</think>", 1)[-1] if "</think>" in value else value
        cleaned = cleaned.replace("<think>", "")
        fenced = re.search(
            r"```(?:json)?\s*([\s\S]*?)\s*```",
            cleaned,
            flags=re.IGNORECASE,
        )
        if fenced:
            return fenced.group(1).strip()
        unclosed = re.search(
            r"```(?:json)?\s*([\s\S]*)",
            cleaned,
            flags=re.IGNORECASE,
        )
        return (unclosed.group(1) if unclosed else cleaned).strip()

    @staticmethod
    def _decode_sse_events(event: SSEEvent) -> Iterator[Mapping[str, Any]]:
        """从一个标准 SSE 事件中解析一个或多个 AnythingLLM JSON 消息。

        首先把完整 ``data`` 作为一个 JSON 文档解析，以正确支持标准 SSE 的多行 JSON。
        如果完整解析失败，再逐行解析，以兼容部分 AnythingLLM 部署连续输出多个
        ``data: {json}`` 行却不使用空行分隔事件的非标准行为。Transport 会按 SSE 规范
        用换行符合并这些 data 行，因此这里仍能恢复原始消息边界。

        无效行、心跳和 ``[DONE]`` 终止标记不会产生消息。该兼容策略仅存在于供应商线程
        适配层，不改变通用 Transport 的标准 SSE 分帧语义。
        """
        data = event.data.strip()
        if not data or data == "[DONE]":
            return
        try:
            decoded = json.loads(data)
        except json.JSONDecodeError:
            # 多个逐行 JSON 被 Transport 合并时，完整 data 不是单个合法 JSON。
            parsed_line_count = 0
            for raw_line in data.splitlines():
                line = raw_line.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    continue
                try:
                    line_value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(line_value, Mapping):
                    parsed_line_count += 1
                    yield line_value
            logger.debug(
                "按逐行 JSON 兼容模式解析 AnythingLLM SSE data: "
                "data_chars=%d line_count=%d parsed_line_count=%d",
                len(data),
                len(data.splitlines()),
                parsed_line_count,
            )
            return
        if isinstance(decoded, Mapping):
            yield decoded

    @staticmethod
    def _answer_text(value: Any, *, chunks: Sequence[str]) -> str:
        """取得最终回答文本，必要时使用已收集片段或 JSON 序列化结果。"""
        if isinstance(value, str) and value:
            return value
        if chunks:
            return "".join(chunks)
        if value is None:
            return ""
        serialized = json_text(value)
        return "" if serialized in {"{}", "null"} else serialized

    @staticmethod
    def _chat_payload(
        message: str,
        *,
        mode: str,
        document_ids: Optional[Sequence[str]],
        user_id: int | None,
    ) -> dict[str, Any]:
        """构造经过模式白名单校验的问答请求体。

        ``chat`` 与 ``query`` 在 AnythingLLM 中具有不同知识边界。调用方拼写错误时静默
        回退会改变 RAG 语义，因此这里只接受两个明确值；允许去除首尾空白和大小写归一，
        但拒绝 ``None``、空串及其他字符串。文件列表仍只在存在非空 ID 时发送。
        """
        normalized_mode = AnythingLLMThreadClient._normalize_mode(mode)
        payload: dict[str, Any] = {
            "message": message,
            "mode": normalized_mode,
        }
        files = [str(item).strip() for item in document_ids or () if str(item).strip()]
        if files:
            payload["files"] = files
        if user_id is not None:
            payload["userId"] = user_id
        return payload

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        """规范化并校验 AnythingLLM 问答模式，禁止隐式默认和未知模式透传。"""
        if not isinstance(mode, str):
            raise ValueError("mode 必须是 chat 或 query")
        normalized = mode.strip().casefold()
        if normalized not in AnythingLLMThreadClient._ALLOWED_CHAT_MODES:
            raise ValueError("mode 必须是 chat 或 query")
        return normalized

    @classmethod
    def _thread_collection_path(cls, workspace_slug: str) -> str:
        """构造指定工作区的线程创建路径。"""
        return f"workspace/{cls._segment(workspace_slug, name='workspace_slug')}/thread/new"

    @classmethod
    def _thread_path(cls, workspace_slug: str, thread_slug: str) -> str:
        """构造指定工作区和线程的基础路径。"""
        workspace = cls._segment(workspace_slug, name="workspace_slug")
        thread = cls._segment(thread_slug, name="thread_slug")
        return f"workspace/{workspace}/thread/{thread}"

    @staticmethod
    def _segment(value: str, *, name: str) -> str:
        """校验并编码一个线程 API 路径段。"""
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return quote(normalized, safe="")
