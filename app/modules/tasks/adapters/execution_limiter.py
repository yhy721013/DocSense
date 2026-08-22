"""基于严格 FIFO 许可队列的进程内重型任务执行 Adapter。"""

from __future__ import annotations

from collections import deque
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

    普通 ``run`` 仅保留给离线旧链回归夹具；三个 v2 Dispatcher 使用
    ``acquire_interruptibly``，从而能够在停机期间撤销尚未取得许可的等待。两条路径
    共享同一组许可，因此不会在迁移窗口中绕过原有单并发资源边界。等待者使用严格 FIFO
    队列；Report、Weaponry 和 Analysis 即使刚归还许可，也必须排到已经等待的任务
    后面，避免某一业务 Dispatcher 在持续积压时反复抢占唯一重型资源。
    """

    def __init__(self, max_concurrency: int = 1) -> None:
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or max_concurrency < 1
        ):
            raise ValueError("max_concurrency 必须是正整数")
        self._max_concurrency = max_concurrency
        self._condition = threading.Condition(threading.Lock())
        self._available = max_concurrency
        self._next_ticket = 0
        self._waiters: deque[int] = deque()

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def waiting_count(self) -> int:
        """返回当前 FIFO 等待者数量，供 readiness、诊断和确定性并发测试读取。"""

        with self._condition:
            return len(self._waiters)

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
        return self._acquire_fifo(
            cancel_requested=cancel_requested,
            poll_interval_seconds=poll_interval,
        )

    def release(self) -> None:
        """归还一个已取得的许可；重复归还会被明确拒绝。"""

        with self._condition:
            if self._available >= self._max_concurrency:
                raise ValueError("上传任务并发许可被重复归还")
            self._available += 1
            # 唤醒全部等待者，由队首按 ticket 取得许可；Condition 本身的唤醒顺序不再
            # 影响业务公平性。
            self._condition.notify_all()
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
        self._acquire_fifo()
        try:
            logger.debug(
                "获得上传任务并发许可: max_concurrency=%d",
                self._max_concurrency,
            )
            return function(*args, **kwargs)
        finally:
            self.release()

    def _acquire_fifo(
        self,
        *,
        cancel_requested: Callable[[], bool] | None = None,
        poll_interval_seconds: float | None = None,
    ) -> bool:
        """按 ticket 顺序取得许可，并保证取消/异常不会在队列中留下幽灵等待者。"""

        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            self._waiters.append(ticket)
            try:
                while True:
                    if cancel_requested is not None:
                        cancelled = cancel_requested()
                        if not isinstance(cancelled, bool):
                            raise TypeError("cancel_requested 必须返回 bool")
                        if cancelled:
                            self._waiters.remove(ticket)
                            self._condition.notify_all()
                            logger.info(
                                "上传任务许可等待已因停机取消: "
                                "max_concurrency=%d ticket=%d",
                                self._max_concurrency,
                                ticket,
                            )
                            return False
                    if (
                        self._available > 0
                        and self._waiters
                        and self._waiters[0] == ticket
                    ):
                        self._waiters.popleft()
                        self._available -= 1
                        # max_concurrency > 1 时允许下一位立即消费剩余许可。
                        self._condition.notify_all()
                        logger.debug(
                            "获得上传任务并发许可: max_concurrency=%d ticket=%d",
                            self._max_concurrency,
                            ticket,
                        )
                        return True
                    self._condition.wait(timeout=poll_interval_seconds)
            except BaseException:
                # 只有尚未取得许可的 ticket 仍可能留在队列。remove 的 ValueError 表示它
                # 已经成功出队，不得因此掩盖原始异常。
                try:
                    self._waiters.remove(ticket)
                except ValueError:
                    pass
                self._condition.notify_all()
                raise


__all__ = ["UploadTaskLimiter"]
