"""进程内 FIFO 重型文档处理许可。

这不是可靠任务队列，也不宣称支持多实例全局限流。它只限制当前实例实际进入
MinerU/OCR 的重型 I/O 数量；accepted/in-flight 业务事实仍由持久化任务记录持有。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from app.modules.document_processing.domain import (
    DocumentProcessingError,
    DocumentProcessingRequest,
)
from app.modules.document_processing.ports import (
    DocumentProcessorPort,
    ProcessorOutput,
    ResourcePort,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Waiter:
    ticket: int


class FIFOCapacityAdapter:
    """有界、公平、可观测的单实例许可实现。"""

    def __init__(
        self,
        capacity: int,
        *,
        acquire_timeout_seconds: float | None = None,
        resource_name: str = "document-heavy-io",
        max_waiters: int = 1024,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity 必须是正整数")
        if (
            acquire_timeout_seconds is not None
            and acquire_timeout_seconds <= 0
        ):
            raise ValueError("acquire_timeout_seconds 必须为正数或 None")
        if (
            isinstance(max_waiters, bool)
            or not isinstance(max_waiters, int)
            or max_waiters <= 0
        ):
            raise ValueError("max_waiters 必须是正整数")
        self._capacity = capacity
        self._timeout = acquire_timeout_seconds
        self._resource_name = str(resource_name).strip()
        self._max_waiters = max_waiters
        if not self._resource_name:
            raise ValueError("resource_name 不能为空")
        self._condition = threading.Condition()
        self._waiters: deque[_Waiter] = deque()
        self._active = 0
        self._next_ticket = 1

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def active_count(self) -> int:
        with self._condition:
            return self._active

    @property
    def waiting_count(self) -> int:
        with self._condition:
            return len(self._waiters)

    @contextmanager
    def acquire(
        self,
        request: DocumentProcessingRequest,
    ) -> Iterator[None]:
        if not isinstance(request, DocumentProcessingRequest):
            raise TypeError("request 必须是 DocumentProcessingRequest")
        started_at = time.monotonic()
        with self._condition:
            if len(self._waiters) >= self._max_waiters:
                logger.warning(
                    "重型文档处理进程内等待容量已满: resource=%s "
                    "task_id=%s waiting=%d max_waiters=%d",
                    self._resource_name,
                    request.task_id,
                    len(self._waiters),
                    self._max_waiters,
                )
                raise DocumentProcessingError(
                    "document_resource_queue_full",
                    "重型文档处理等待容量已满",
                )
            waiter = _Waiter(self._next_ticket)
            self._next_ticket += 1
            self._waiters.append(waiter)
            try:
                while (
                    self._waiters[0] is not waiter
                    or self._active >= self._capacity
                ):
                    remaining = None
                    if self._timeout is not None:
                        remaining = self._timeout - (time.monotonic() - started_at)
                        if remaining <= 0:
                            raise DocumentProcessingError(
                                "document_resource_acquire_timeout",
                                "等待重型文档处理许可超时",
                            )
                    self._condition.wait(timeout=remaining)
            except BaseException:
                # 超时、中断和取消都必须移除 waiter，否则队首幽灵会永久阻塞后续任务。
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
                self._condition.notify_all()
                raise
            self._waiters.popleft()
            self._active += 1
            self._condition.notify_all()

        logger.debug(
            "取得重型文档处理许可: resource=%s task_id=%s step_key=%s "
            "active=%d capacity=%d wait_ms=%d",
            self._resource_name,
            request.task_id,
            request.step_key[:12],
            self.active_count,
            self._capacity,
            int((time.monotonic() - started_at) * 1000),
        )
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                if self._active < 0:  # pragma: no cover - 防御性不变量
                    self._active = 0
                    raise RuntimeError("重型文档处理许可计数失衡")
                self._condition.notify_all()
            logger.debug(
                "释放重型文档处理许可: resource=%s task_id=%s step_key=%s "
                "active=%d capacity=%d",
                self._resource_name,
                request.task_id,
                request.step_key[:12],
                self.active_count,
                self._capacity,
            )


class ResourceLimitedDocumentProcessorAdapter:
    """只在真实 Processor I/O 周围持有许可的无状态装饰器。"""

    def __init__(
        self,
        *,
        processor: DocumentProcessorPort,
        resource: ResourcePort,
    ) -> None:
        if not isinstance(processor, DocumentProcessorPort):
            raise TypeError("processor 必须实现 DocumentProcessorPort")
        if not isinstance(resource, ResourcePort):
            raise TypeError("resource 必须实现 ResourcePort")
        self._processor = processor
        self._resource = resource

    def process(self, request: DocumentProcessingRequest) -> ProcessorOutput:
        with self._resource.acquire(request):
            return self._processor.process(request)


__all__ = [
    "FIFOCapacityAdapter",
    "ResourceLimitedDocumentProcessorAdapter",
]
