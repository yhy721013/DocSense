"""Internal dispatch boundary for a durably accepted file-chat run.

The boundary intentionally carries only ``run_id``.  Request snapshots,
execution leases and provider resources are loaded by the executor, which makes
the synchronous adapter follow the same shape a future worker will use.
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
    """Real capabilities of a run dispatch adapter.

    The values are intentionally positive capabilities.  A future adapter can
    support both single-instance and multi-instance deployments without being
    rejected merely because it is more capable than the current SQLite path.
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
    """Start one accepted run without importing HTTP/SSE presentation code."""

    @property
    def capabilities(self) -> ChatRunDispatchCapabilities:
        """Return the adapter's verifiable execution capabilities."""
        ...

    def dispatch(self, *, run_id: str) -> Iterable[ChatStreamEvent]:
        """Execute or relay one run identified by its durable internal key."""
        ...


class InlineChatRunDispatcher:
    """Current synchronous adapter; explicitly not a reliable queue."""

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
