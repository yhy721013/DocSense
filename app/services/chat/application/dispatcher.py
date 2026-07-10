"""Internal dispatch boundary for one accepted file-chat run."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.services.chat.application.run_executor import ChatRunStreamRequest
from app.services.chat.domain.events import ChatStreamEvent


@dataclass(frozen=True)
class ChatRunExecutionLease:
    """Opaque execution input handed from HTTP acceptance to an executor."""

    request: ChatRunStreamRequest

    def __post_init__(self) -> None:
        if not isinstance(self.request, ChatRunStreamRequest):
            raise TypeError("request must be ChatRunStreamRequest")

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

    def dispatch(self, lease: ChatRunExecutionLease) -> Iterable[ChatStreamEvent]:
        """Execute or schedule one run without emitting HTTP/SSE presentation text."""
        ...


class InlineChatRunDispatcher:
    """Single-instance synchronous dispatcher; it is not a reliable queue."""

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
    "ChatRunDispatcher",
    "ChatRunExecutionLease",
    "InlineChatRunDispatcher",
]
