"""Execution boundary for one file-chat run."""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from app.services.chat.domain.events import ChatStreamEvent


@runtime_checkable
class ChatRunExecutor(Protocol):
    """Executes a chat run and yields supplier-neutral stream events."""

    def execute(self, *, run_id: str) -> Iterable[ChatStreamEvent]:
        """Execute one accepted/running run."""
        ...


__all__ = ["ChatRunExecutor"]
