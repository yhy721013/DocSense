"""跨业务严格轮转的进程内重型执行容量仲裁器。"""

from __future__ import annotations

from collections import deque
import logging
from threading import Condition, Lock
from typing import Callable


logger = logging.getLogger(__name__)


class _BusinessPermit:
    def __init__(self, pool: "FairTaskExecutionPermitPool", business: str) -> None:
        self._pool = pool
        self._business = business

    def acquire_interruptibly(
        self,
        cancel_requested: Callable[[], bool],
        *,
        poll_interval_seconds: float,
    ) -> bool:
        return self._pool._acquire(
            self._business,
            cancel_requested,
            poll_interval_seconds=poll_interval_seconds,
        )

    def release(self) -> None:
        self._pool._release()

    @property
    def max_concurrency(self) -> int:
        """兼容现有容器诊断字段；容量事实仍由唯一 Pool 持有。"""

        return self._pool.capacity

    @property
    def waiting_count(self) -> int:
        return self._pool.waiting_counts[self._business]


class FairTaskExecutionPermitPool:
    """对非空业务等待队列按固定顺序 round-robin，不承诺完成顺序。"""

    def __init__(
        self,
        *,
        capacity: int = 1,
        business_order: tuple[str, ...] = ("report", "weaponry", "file"),
    ) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("capacity 必须是正整数")
        normalized = tuple(item.strip() for item in business_order)
        if not normalized or any(not item for item in normalized):
            raise ValueError("business_order 只能包含非空名称")
        if len(normalized) != len(set(normalized)):
            raise ValueError("business_order 不得重复")
        self._capacity = capacity
        self._available = capacity
        self._order = normalized
        self._turn = 0
        self._condition = Condition(Lock())
        self._next_ticket = 0
        self._waiters = {name: deque() for name in normalized}

    def for_business(self, business: str) -> _BusinessPermit:
        if business not in self._waiters:
            raise ValueError("business 不在冻结轮转顺序中")
        return _BusinessPermit(self, business)

    @property
    def capacity(self) -> int:
        return self._capacity

    def owns(self, permit: object) -> bool:
        """验证业务 Permit 是否由当前唯一容量池签发，供组合根 fail fast。"""

        return isinstance(permit, _BusinessPermit) and permit._pool is self

    @property
    def waiting_counts(self) -> dict[str, int]:
        with self._condition:
            return {name: len(queue) for name, queue in self._waiters.items()}

    def _next_waiting_business(self) -> str | None:
        for offset in range(len(self._order)):
            index = (self._turn + offset) % len(self._order)
            name = self._order[index]
            if self._waiters[name]:
                return name
        return None

    def _acquire(
        self,
        business: str,
        cancel_requested: Callable[[], bool],
        *,
        poll_interval_seconds: float,
    ) -> bool:
        if not callable(cancel_requested):
            raise TypeError("cancel_requested 必须可调用")
        if isinstance(poll_interval_seconds, bool) or not isinstance(
            poll_interval_seconds, (int, float)
        ) or float(poll_interval_seconds) <= 0:
            raise ValueError("poll_interval_seconds 必须是正数")
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            queue = self._waiters[business]
            queue.append(ticket)
            try:
                while True:
                    cancelled = cancel_requested()
                    if not isinstance(cancelled, bool):
                        raise TypeError("cancel_requested 必须返回 bool")
                    if cancelled:
                        queue.remove(ticket)
                        self._condition.notify_all()
                        return False
                    if (
                        self._available > 0
                        and queue
                        and queue[0] == ticket
                        and self._next_waiting_business() == business
                    ):
                        queue.popleft()
                        self._available -= 1
                        self._turn = (self._order.index(business) + 1) % len(self._order)
                        self._condition.notify_all()
                        return True
                    self._condition.wait(timeout=float(poll_interval_seconds))
            except BaseException:
                try:
                    queue.remove(ticket)
                except ValueError:
                    pass
                self._condition.notify_all()
                raise

    def _release(self) -> None:
        with self._condition:
            if self._available >= self._capacity:
                raise ValueError("公平容量许可被重复归还")
            self._available += 1
            self._condition.notify_all()
        logger.debug("已归还 Task 公平容量许可: capacity=%d", self._capacity)


__all__ = ["FairTaskExecutionPermitPool"]
