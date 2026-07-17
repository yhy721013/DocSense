"""基于有界信号量的进程内重型任务执行许可 Adapter。"""

from __future__ import annotations

import logging
import threading
from typing import Callable, ParamSpec, TypeVar


logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _positive_interval(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("poll_interval_seconds 必须是数字")
    normalized = float(value)
    if (
        normalized != normalized
        or normalized in (float("inf"), float("-inf"))
        or normalized <= 0.0
    ):
        raise ValueError("poll_interval_seconds 必须是正有限数字")
    return normalized


class UploadTaskLimiter:
    """限制共享 Document Processor 的上传类任务并发数。

    普通 ``run`` 保留给尚未迁移的 analysis 后台线程；新的 Dispatcher 使用
    ``acquire_interruptibly``，从而能够在停机期间撤销尚未取得许可的等待。两条路径
    共享同一信号量，因此不会在迁移窗口中绕过原有单并发资源边界。
    """

    def __init__(self, max_concurrency: int = 1) -> None:
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or max_concurrency < 1
        ):
            raise ValueError("max_concurrency 必须是正整数")
        self._max_concurrency = max_concurrency
        self._semaphore = threading.BoundedSemaphore(max_concurrency)

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    def acquire_interruptibly(
        self,
        cancel_requested: Callable[[], bool],
        *,
        poll_interval_seconds: float,
    ) -> bool:
        """分段等待许可；取消后保证不再把尚未开始的任务交给业务执行器。"""

        if not callable(cancel_requested):
            raise TypeError("cancel_requested 必须可调用")
        poll_interval = _positive_interval(poll_interval_seconds)
        while True:
            cancelled = cancel_requested()
            if not isinstance(cancelled, bool):
                raise TypeError("cancel_requested 必须返回 bool")
            if cancelled:
                logger.info(
                    "上传任务许可等待已因停机取消: max_concurrency=%d",
                    self._max_concurrency,
                )
                return False
            if self._semaphore.acquire(timeout=poll_interval):
                logger.debug(
                    "获得上传任务并发许可: max_concurrency=%d",
                    self._max_concurrency,
                )
                return True

    def release(self) -> None:
        """归还一个已取得的许可；重复归还由 BoundedSemaphore 明确拒绝。"""

        self._semaphore.release()
        logger.debug(
            "归还上传任务并发许可: max_concurrency=%d",
            self._max_concurrency,
        )

    def run(
        self,
        function: Callable[_P, _R],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _R:
        """在并发许可内执行函数，并在所有退出路径上归还许可。"""

        if not callable(function):
            raise TypeError("function 必须可调用")
        logger.debug(
            "等待上传任务并发许可: max_concurrency=%d",
            self._max_concurrency,
        )
        self._semaphore.acquire()
        try:
            logger.debug(
                "获得上传任务并发许可: max_concurrency=%d",
                self._max_concurrency,
            )
            return function(*args, **kwargs)
        finally:
            self.release()


__all__ = ["UploadTaskLimiter"]
