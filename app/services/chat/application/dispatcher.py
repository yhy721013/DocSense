"""已持久化受理的文件对话运行的内部调度边界。

该边界刻意只传递 ``run_id``。请求快照、执行租约和供应商资源均由执行器加载，
使当前同步适配器与未来工作进程保持相同的调用形态。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.services.chat.domain.events import ChatStreamEvent


def _required_text(value: str, *, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


@dataclass(frozen=True)
class ChatRunDispatchCapabilities:
    """运行调度适配器真实具备的能力。

    字段使用正向能力语义。未来适配器即使同时支持单实例和多实例部署，也不会
    因能力强于当前 SQLite 路径而被错误拒绝。
    """

    supports_single_instance: bool
    supports_external_workers: bool
    reliable_delivery: bool

INLINE_CHAT_RUN_DISPATCH_CAPABILITIES = ChatRunDispatchCapabilities(
    supports_single_instance=True,
    supports_external_workers=False,
    reliable_delivery=False,
)


@runtime_checkable
class ChatRunDispatcher(Protocol):
    """启动一条已受理运行，但不导入 HTTP/SSE 展示层代码。"""

    @property
    def capabilities(self) -> ChatRunDispatchCapabilities:
        """返回该适配器可验证的执行能力。"""
        ...

    def dispatch(self, *, run_id: str) -> Iterable[ChatStreamEvent]:
        """执行或转交一条由持久化内部标识定位的运行。"""
        ...


class InlineChatRunDispatcher:
    """当前同步适配器；明确不属于可靠队列。"""

    capabilities = INLINE_CHAT_RUN_DISPATCH_CAPABILITIES

    def __init__(
        self,
        *,
        execute: Callable[[str], Iterable[ChatStreamEvent]],
    ) -> None:
        if not callable(execute):
            raise TypeError("execute must be callable")
        self._execute = execute

    def dispatch(self, *, run_id: str) -> Iterable[ChatStreamEvent]:
        normalized_run_id = _required_text(run_id, name="run_id")
        events = self._execute(normalized_run_id)
        if not isinstance(events, Iterable):
            raise TypeError("execute must return an iterable of ChatStreamEvent")
        return events


__all__ = [
    "ChatRunDispatchCapabilities",
    "ChatRunDispatcher",
    "INLINE_CHAT_RUN_DISPATCH_CAPABILITIES",
    "InlineChatRunDispatcher",
]
