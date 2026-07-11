"""Durable cleanup-job dispatch boundary for file chat.

Jobs are persisted before this boundary is invoked.  Dispatchers therefore
never receive a captured callback or request-local object; an external worker
can later load exactly the same job by ``job_id``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.services.chat.domain.models import ChatCleanupJob


@dataclass(frozen=True)
class ChatCleanupDispatchCapabilities:
    """Positive delivery capabilities exposed to the composition root."""

    supports_single_instance: bool
    supports_external_workers: bool
    reliable_delivery: bool
    supports_delayed_retry: bool
    supports_synchronous_completion: bool


INLINE_CHAT_CLEANUP_DISPATCH_CAPABILITIES = ChatCleanupDispatchCapabilities(
    supports_single_instance=True,
    supports_external_workers=False,
    reliable_delivery=False,
    supports_delayed_retry=False,
    supports_synchronous_completion=True,
)


@runtime_checkable
class ChatCleanupDispatcher(Protocol):
    """Notify a scheduler that an already-persisted cleanup job is ready."""

    @property
    def capabilities(self) -> ChatCleanupDispatchCapabilities:
        """Return verifiable delivery capabilities for this adapter."""
        ...

    def dispatch(self, *, job: ChatCleanupJob) -> ChatCleanupJob:
        """Dispatch a durable job and return its current persisted state."""
        ...


class InlineChatCleanupDispatcher:
    """Current synchronous-mode notification adapter.

    The adapter holds one application-level executor selected at composition
    time.  ``dispatch`` passes only the durable job ID to it, so it is neither
    a fake in-memory queue nor a request-specific callback registry.  Synchronous
    completion is required by the existing delete API, whose response reports
    whether remote cleanup has actually completed.
    """

    capabilities = INLINE_CHAT_CLEANUP_DISPATCH_CAPABILITIES

    def __init__(
        self,
        *,
        execute: Callable[..., ChatCleanupJob],
    ) -> None:
        if not callable(execute):
            raise TypeError("execute must be callable")
        self._execute = execute

    def dispatch(self, *, job: ChatCleanupJob) -> ChatCleanupJob:
        if not isinstance(job, ChatCleanupJob):
            raise TypeError("job must be ChatCleanupJob")
        result = self._execute(job_id=job.job_id)
        if not isinstance(result, ChatCleanupJob):
            raise TypeError("cleanup executor must return ChatCleanupJob")
        return result


__all__ = [
    "ChatCleanupDispatchCapabilities",
    "ChatCleanupDispatcher",
    "INLINE_CHAT_CLEANUP_DISPATCH_CAPABILITIES",
    "InlineChatCleanupDispatcher",
]
