"""知识谱系对话独立 SSE Presenter。"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Iterator

from app.modules.chat.domain.events import ChatStreamEvent
from app.modules.chat.domain.identity import WeaponryChatIdentity
from app.presenters.sse import close_sse_resource, format_sse_event


logger = logging.getLogger(__name__)
_TERMINALS = frozenset({"done", "aborted", "error"})


def _public_event(
    event: ChatStreamEvent,
    *,
    identity: WeaponryChatIdentity,
) -> tuple[str, dict]:
    """把内部事件转换为冻结公开字段，绝不输出内部或供应商身份。"""
    if event.event_type == "chatInfo":
        return "chatInfo", {
            "userId": identity.user_id,
            "architectureId": identity.architecture_id,
            "isNewChat": bool(event.data.get("isNewChat")),
        }
    if event.event_type == "textChunk":
        content = event.data.get("content")
        if not isinstance(content, str) or content == "":
            raise ValueError("textChunk content is invalid")
        return "textChunk", {"content": content}
    if event.event_type == "source_snapshot":
        if set(event.data) != {"chunks"}:
            raise ValueError("source_snapshot fields are invalid")
        return "sourceChunks", {
            "userId": identity.user_id,
            "architectureId": identity.architecture_id,
            "chunks": event.data["chunks"],
        }
    if event.event_type in {"done", "aborted"}:
        return event.event_type, {
            "userId": identity.user_id,
            "architectureId": identity.architecture_id,
        }
    if event.event_type == "error":
        error = event.data.get("error")
        if not isinstance(error, str) or not error:
            raise ValueError("error event content is invalid")
        return "error", {"error": error}
    raise ValueError("unsupported weaponry chat stream event")


def finalize_weaponry_chat_stream(
    *,
    stream: Iterable[ChatStreamEvent],
    run_id: str,
    identity: WeaponryChatIdentity,
    on_close: Callable[[], None] | None = None,
) -> Iterator[str]:
    """强制首事件、来源事件和互斥终态顺序，并在退出时关闭执行资源。"""
    state = "start"
    terminal_seen = False
    try:
        for event in stream:
            if not isinstance(event, ChatStreamEvent):
                raise TypeError("chat stream must yield ChatStreamEvent")
            public_type, public_data = _public_event(event, identity=identity)
            if state == "start":
                if public_type != "chatInfo":
                    raise ValueError("weaponry chat first event must be chatInfo")
                state = "text"
            elif public_type == "chatInfo":
                raise ValueError("weaponry chat emitted duplicate chatInfo")
            elif public_type == "textChunk":
                if state != "text":
                    raise ValueError("textChunk must precede sourceChunks")
            elif public_type == "sourceChunks":
                if state != "text":
                    raise ValueError("sourceChunks must appear exactly once")
                state = "sources"
            elif public_type == "done":
                if state != "sources":
                    raise ValueError("done requires one sourceChunks event")
                state = "terminal"
            elif public_type in {"aborted", "error"}:
                if state not in {"text", "sources"}:
                    raise ValueError("weaponry chat terminal event is invalid")
                # Application 不会在失败/中断前产出来源；Presenter 再次失败关闭。
                if state == "sources":
                    raise ValueError("failed stream cannot expose sourceChunks")
                state = "terminal"

            yield format_sse_event(public_type, public_data)
            if public_type in _TERMINALS:
                terminal_seen = True
                break
    finally:
        if not terminal_seen:
            logger.warning(
                "知识谱系对话 SSE 未观察到终态即关闭: run_id=%s",
                run_id,
            )
        close_sse_resource(stream, run_id=run_id, label="weaponry_stream")
        if on_close is not None:
            try:
                on_close()
            except Exception:
                logger.exception(
                    "知识谱系对话 SSE 关闭回调失败: run_id=%s",
                    run_id,
                )


__all__ = ["finalize_weaponry_chat_stream"]
