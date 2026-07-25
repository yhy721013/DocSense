"""文件对话流的 SSE 展示层辅助工具。"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Iterable, Iterator, Mapping

from app.services.chat.domain.chat_id import chat_id_public_value
from app.services.chat.domain.events import ChatStreamEvent


logger = logging.getLogger(__name__)
_TERMINAL_EVENT_TYPES = frozenset({"aborted", "done", "error"})
_CHAT_ID_EVENT_TYPES = frozenset({"aborted", "chatInfo", "done"})


def format_sse_event(event_type: str, data: Mapping[str, Any] | None = None) -> str:
    """将一个领域流事件格式化为 Server-Sent Events 载荷。"""
    normalized_type = str(event_type or "").strip()
    if not normalized_type:
        raise ValueError("event_type cannot be empty")
    payload = json.dumps(dict(data or {}), ensure_ascii=False)
    return f"event: {normalized_type}\ndata: {payload}\n\n"


def _normalize_public_event_data(event: ChatStreamEvent) -> dict[str, Any]:
    """在 SSE 最终输出边界确保 chatId 不会以字符串泄露给前端。

    运行、租约和事件账本内部仍以文本键关联；若未来替换执行器或任务队列时有新
    生产者遗漏了应用层转换，本函数仍可阻止错误类型进入 HTTP 协议。非规范存量
    值不能安全映射为公开正整数，因此记录可排查日志并中断该 SSE 输出。
    """

    payload = dict(event.data)
    if event.event_type not in _CHAT_ID_EVENT_TYPES:
        return payload

    try:
        payload["chatId"] = chat_id_public_value(payload.get("chatId"))
    except ValueError:
        logger.error(
            "拒绝输出非规范 chatId 的 SSE 事件: event_type=%s payload_keys=%s",
            event.event_type,
            sorted(payload.keys()),
        )
        raise
    return payload


def present_chat_stream(events: Iterable[ChatStreamEvent]) -> Iterator[str]:
    """将供应商无关的对话事件转换为 SSE 载荷。"""
    for event in events:
        if not isinstance(event, ChatStreamEvent):
            raise TypeError("chat stream must yield ChatStreamEvent")
        logger.debug("展示文件对话 SSE 事件: event_type=%s", event.event_type)
        yield format_sse_event(event.event_type, _normalize_public_event_data(event))


def close_chat_stream_resource(
    resource: Any,
    *,
    run_id: str,
    label: str,
) -> None:
    close = getattr(resource, "close", None)
    if not callable(close):
        logger.debug(
            "文件对话流资源无需关闭: run_id=%s resource=%s",
            run_id,
            label,
        )
        return
    try:
        close()
        logger.debug(
            "文件对话流资源已关闭: run_id=%s resource=%s",
            run_id,
            label,
        )
    except Exception:
        logger.exception(
            "关闭文件对话流资源失败: run_id=%s resource=%s",
            run_id,
            label,
        )


def finalize_chat_run_stream(
    *,
    stream: Iterable[ChatStreamEvent],
    run_id: str,
    on_close: Callable[[], None] | None = None,
) -> Iterator[str]:
    terminal_event_seen = False
    try:
        for event in stream:
            if not isinstance(event, ChatStreamEvent):
                raise TypeError("chat stream must yield ChatStreamEvent")
            is_terminal = event.event_type in _TERMINAL_EVENT_TYPES
            # Presenter 只负责协议转换和资源关闭，run 状态已经由 application
            # 层的 ChatRunEventRecorder 收敛，避免展示层与业务层双写状态。
            logger.debug(
                "准备发送文件对话 SSE 事件: run_id=%s event_type=%s terminal=%s",
                run_id,
                event.event_type,
                is_terminal,
            )
            yield format_sse_event(
                event.event_type,
                _normalize_public_event_data(event),
            )
            if is_terminal:
                terminal_event_seen = True
                logger.info(
                    "文件对话 SSE 流收到终态事件并准备关闭: run_id=%s event_type=%s",
                    run_id,
                    event.event_type,
                )
                break
    finally:
        if not terminal_event_seen:
            logger.warning(
                "文件对话 SSE 流在未观察到终态事件时关闭: run_id=%s",
                run_id,
            )
        close_chat_stream_resource(stream, run_id=run_id, label="stream")
        if on_close is not None:
            try:
                on_close()
                logger.debug("文件对话客户端关闭回调已完成: run_id=%s", run_id)
            except Exception:
                logger.exception(
                    "执行文件对话客户端关闭回调失败: run_id=%s",
                    run_id,
                )


__all__ = [
    "close_chat_stream_resource",
    "finalize_chat_run_stream",
    "format_sse_event",
    "present_chat_stream",
]
