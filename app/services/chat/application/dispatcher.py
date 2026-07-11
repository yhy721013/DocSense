"""Internal dispatch boundary for one accepted file-chat run."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.services.chat.application.run_executor import ChatRunStreamRequest
from app.services.chat.domain.events import ChatStreamEvent
from app.services.chat.locking.lease import ChatRunLease


@dataclass(frozen=True)
class ChatRunDispatchCapabilities:
    """运行调度器的真实能力，用于阻止同步实现被误标为可靠队列。"""

    single_instance_only: bool
    reliable_delivery: bool
    supports_external_workers: bool


INLINE_CHAT_RUN_DISPATCH_CAPABILITIES = ChatRunDispatchCapabilities(
    single_instance_only=True,
    reliable_delivery=False,
    supports_external_workers=False,
)


@dataclass(frozen=True)
class ChatRunExecutionLease:
    """从 HTTP 受理传给内部执行器的不可变输入与运行权证明。

    ``ownership_lease`` 只在服务端内部使用，绝不交给 Presenter 或 HTTP/SSE。
    它允许未来 worker 在 heartbeat 和终态提交时携带 token/fencing 条件，而
    当前单实例适配器会使用一个明确无 fencing 的租约对象。
    """

    request: ChatRunStreamRequest
    ownership_lease: ChatRunLease | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, ChatRunStreamRequest):
            raise TypeError("request must be ChatRunStreamRequest")
        if self.ownership_lease is not None:
            if not isinstance(self.ownership_lease, ChatRunLease):
                raise TypeError("ownership_lease must be ChatRunLease or None")
            if self.ownership_lease.run_id != self.request.run_id:
                raise ValueError("ownership_lease does not match request.run_id")
            if self.ownership_lease.chat_id != self.request.chat_id:
                raise ValueError("ownership_lease does not match request.chat_id")

    @property
    def run_id(self) -> str:
        """Return the internal run key without exposing it to HTTP callers."""
        return self.request.run_id

    @property
    def chat_id(self) -> str:
        """Return the external conversation key used by the run."""
        return self.request.chat_id


@runtime_checkable
class ChatRunDispatcher(Protocol):
    """Dispatches one accepted run and returns its internal domain-event stream."""

    @property
    def capabilities(self) -> ChatRunDispatchCapabilities:
        """返回该调度器实际提供的执行与投递能力。"""
        ...

    def dispatch(self, lease: ChatRunExecutionLease) -> Iterable[ChatStreamEvent]:
        """Execute or schedule one run without emitting HTTP/SSE presentation text."""
        ...


class InlineChatRunDispatcher:
    """Single-instance synchronous dispatcher; it is not a reliable queue."""

    capabilities = INLINE_CHAT_RUN_DISPATCH_CAPABILITIES

    def __init__(
        self,
        *,
        execute: Callable[[ChatRunExecutionLease], Iterable[ChatStreamEvent]],
    ) -> None:
        if not callable(execute):
            raise TypeError("execute must be callable")
        self._execute = execute

    def dispatch(self, lease: ChatRunExecutionLease) -> Iterable[ChatStreamEvent]:
        if not isinstance(lease, ChatRunExecutionLease):
            raise TypeError("lease must be ChatRunExecutionLease")
        events = self._execute(lease)
        if not isinstance(events, Iterable):
            raise TypeError("execute must return an iterable of ChatStreamEvent")
        return events


__all__ = [
    "ChatRunDispatchCapabilities",
    "ChatRunDispatcher",
    "ChatRunExecutionLease",
    "INLINE_CHAT_RUN_DISPATCH_CAPABILITIES",
    "InlineChatRunDispatcher",
]
