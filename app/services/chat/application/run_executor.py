"""Execution boundary for one file-chat run."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.services.chat.domain.events import ChatStreamEvent


def _required_text(value: str, *, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


def _text_tuple(values: Sequence[str], *, name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be a sequence of strings")
    normalized: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise TypeError(f"{name}[{index}] must be str")
        item = str(value or "").strip()
        if not item:
            raise ValueError(f"{name}[{index}] cannot be empty")
        normalized.append(item)
    return tuple(normalized)


@dataclass(frozen=True)
class ChatRunStreamRequest:
    """Application input required to execute one file-chat stream."""

    run_id: str
    chat_id: str
    message: str
    file_names: tuple[str, ...] = ()
    file_original_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "run_id",
            _required_text(self.run_id, name="run_id"),
        )
        object.__setattr__(
            self,
            "chat_id",
            _required_text(self.chat_id, name="chat_id"),
        )
        object.__setattr__(
            self,
            "file_names",
            _text_tuple(self.file_names, name="file_names"),
        )
        object.__setattr__(
            self,
            "file_original_names",
            _text_tuple(self.file_original_names, name="file_original_names"),
        )
        object.__setattr__(
            self,
            "message",
            _required_text(self.message, name="message"),
        )
        if len(self.file_names) != len(self.file_original_names):
            raise ValueError(
                "file_names and file_original_names must have the same length"
            )


@runtime_checkable
class ChatRunExecutor(Protocol):
    """Executes a chat run and yields supplier-neutral stream events."""

    def stream_chat_run(
        self,
        request: ChatRunStreamRequest,
    ) -> Iterable[ChatStreamEvent]:
        """Execute one accepted/running run without producing presentation text."""
        ...


__all__ = ["ChatRunExecutor", "ChatRunStreamRequest"]
