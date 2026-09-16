"""Progress WebSocket 连接级有界投递缓冲。

该组件只处理框架无关的 ``ProgressSnapshot``，不持有 WebSocket，也不序列化公开
消息。生产者线程调用 :meth:`publish` 时只执行一次短临界区入队；连接自己的发送
循环负责调用 :meth:`take` 并执行网络 I/O，从而隔离慢客户端。
"""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from dataclasses import dataclass
from threading import Condition, RLock
from time import monotonic
from typing import Sequence

from app.modules.tasks.domain.models import ProgressKey, ProgressSnapshot


logger = logging.getLogger(__name__)


class ProgressDeliveryClosedError(RuntimeError):
    """连接投递缓冲已关闭且没有可消费通知。"""


class ProgressInitialBatchStateError(RuntimeError):
    """初始快照屏障被重复、跨连接或乱序操作。"""


@dataclass(frozen=True)
class ProgressInitialBatchToken:
    """一次“先发初始快照、再放行并发通知”的不透明屏障令牌。"""

    delivery_id: str
    generation: int


class ProgressDeliveryBuffer:
    """每个 WebSocket 连接独占的有界、可合并通知队列。

    队列饱和时不会阻塞任务线程。对于同一业务键只保留最新到达且序号不倒退的
    快照；仍无空间时淘汰最早的其他键快照。Progress 是“当前状态”通知而非可靠
    事件日志，因此中间进度允许合并，终态可靠性应由任务状态库和重新查询保证。
    """

    DEFAULT_CAPACITY = 256

    def __init__(
        self,
        *,
        delivery_id: str,
        capacity: int = DEFAULT_CAPACITY,
    ) -> None:
        normalized_id = str(delivery_id or "").strip()
        if not normalized_id:
            raise ValueError("delivery_id 不能为空")
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise TypeError("capacity 必须是整数")
        if not 1 <= capacity <= 1_000_000:
            raise ValueError("capacity 必须位于 1..1000000")

        self._delivery_id = normalized_id
        self._capacity = capacity
        self._condition = Condition(RLock())
        self._queue: deque[ProgressSnapshot] = deque()
        self._pending: OrderedDict[ProgressKey, ProgressSnapshot] = OrderedDict()
        # 队列项被连接线程取走后，仍需记住该 key 已接受的最新同任务序号。否则 Hub
        # 在锁外通知订阅者时发生回调乱序，迟到的旧通知会在新通知出队后重新入队，
        # 造成客户端进度倒退。该水位只属于当前连接，连接关闭时一并释放。
        self._accepted_watermarks: dict[ProgressKey, ProgressSnapshot] = {}
        self._active_batch: ProgressInitialBatchToken | None = None
        self._generation = 0
        self._closed = False
        self._dropped_count = 0
        self._coalesced_count = 0

    @property
    def delivery_id(self) -> str:
        return self._delivery_id

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def queued_count(self) -> int:
        with self._condition:
            return len(self._queue)

    @property
    def dropped_count(self) -> int:
        with self._condition:
            return self._dropped_count

    @property
    def coalesced_count(self) -> int:
        with self._condition:
            return self._coalesced_count

    @property
    def buffering_initial_batch(self) -> bool:
        with self._condition:
            return self._active_batch is not None

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def begin_initial_batch(self) -> ProgressInitialBatchToken:
        """在建立订阅前暂停通知出队，并返回只能使用一次的屏障令牌。"""

        with self._condition:
            if self._closed:
                raise ProgressDeliveryClosedError("Progress 投递缓冲已关闭")
            if self._active_batch is not None:
                raise ProgressInitialBatchStateError("已有初始快照批次尚未完成")
            self._generation += 1
            token = ProgressInitialBatchToken(
                delivery_id=self._delivery_id,
                generation=self._generation,
            )
            self._active_batch = token
            return token

    def finish_initial_batch(
        self,
        token: ProgressInitialBatchToken,
        *,
        authoritative_snapshots: Sequence[ProgressSnapshot] = (),
    ) -> None:
        """确认初始快照已经发送，然后按序放行真正更新的并发通知。

        同一 TaskId 下序号不大于初始快照的通知是重复/旧通知；不同 TaskId 的缓冲
        通知也以“订阅后读取到的当前快照”为准予以丢弃。具体 Progress Adapter 必须
        在通知 subscriber 前先更新其 latest 投影，才能满足这一屏障契约。
        """

        snapshots = tuple(authoritative_snapshots)
        if any(not isinstance(item, ProgressSnapshot) for item in snapshots):
            raise TypeError("authoritative_snapshots 只能包含 ProgressSnapshot")

        should_warn = False
        with self._condition:
            self._require_active_token(token)
            authoritative = {snapshot.key: snapshot for snapshot in snapshots}
            queued_before_barrier = tuple(self._queue)
            self._queue.clear()
            pending = tuple(self._pending.values())
            self._pending.clear()
            self._active_batch = None

            # 客户端刚刚收到的初始快照就是这些 key 的新投递水位。必须在处理屏障前后
            # 的暂存通知前写入；直接覆盖还能把旧执行遗留在连接队列中的水位切换到当前
            # TaskId。不同 TaskId 的最终 latest-wins 仍由阶段 1C 的持久化 Guard 负责。
            for snapshot in authoritative.values():
                self._accepted_watermarks[snapshot.key] = snapshot

            # 屏障建立前已经排队的通知一定早于本批次读取到的当前快照。即使 TaskId
            # 不同，也应以刚发送的权威快照为准；否则重复订阅可能先发新执行快照，
            # 随后又倒退发送旧执行遗留在连接队列中的通知。
            for snapshot in queued_before_barrier:
                current = authoritative.get(snapshot.key)
                if current is not None and (
                    snapshot.task_id != current.task_id
                    or snapshot.sequence_no <= current.sequence_no
                ):
                    self._coalesced_count += 1
                    continue
                dropped_before = self._dropped_count
                # 该项在屏障建立前已经通过水位检查；队列临时清空后不能再次拿它与
                # 自身水位比较，否则订阅其他 key 时会误删尚未发送的既有通知。
                self._accepted_watermarks[snapshot.key] = snapshot
                self._offer_accepted_queue_locked(snapshot)
                should_warn = should_warn or self._should_log_drop(
                    dropped_before,
                    self._dropped_count,
                )

            # ``pending`` 是本批屏障开启后到达的通知。相同 TaskId 下仍用序号过滤
            # 重复/倒退项；TaskId 不同时必须保留，因为它可能正是在当前快照读取之后
            # 受理的新执行。阶段 1C 会进一步通过持久化 latest-wins Guard 阻止旧执行
            # 写入；1B-2 的连接缓冲不能在缺少该持久化事实时擅自丢弃新执行通知。
            for snapshot in pending:
                current = authoritative.get(snapshot.key)
                if current is not None and (
                    snapshot.task_id == current.task_id
                    and snapshot.sequence_no <= current.sequence_no
                ):
                    self._coalesced_count += 1
                    continue
                dropped_before = self._dropped_count
                self._offer_queue_locked(snapshot)
                should_warn = should_warn or self._should_log_drop(
                    dropped_before,
                    self._dropped_count,
                )
            self._condition.notify_all()

        if should_warn:
            self._log_overflow()

    def abort_initial_batch(self, token: ProgressInitialBatchToken) -> None:
        """订阅建立或初始发送失败时丢弃暂存通知并解除屏障。"""

        with self._condition:
            self._require_active_token(token)
            self._pending.clear()
            self._active_batch = None
            self._condition.notify_all()

    def publish(self, snapshot: ProgressSnapshot) -> None:
        """供任务/Progress Adapter 调用的非阻塞 subscriber。

        关闭后的迟到通知会被安静忽略，避免连接清理与发布线程形成异常竞争。
        """

        if not isinstance(snapshot, ProgressSnapshot):
            raise TypeError("snapshot 必须是 ProgressSnapshot")

        should_warn = False
        with self._condition:
            if self._closed:
                return
            dropped_before = self._dropped_count
            if self._active_batch is not None:
                self._offer_pending_locked(snapshot)
            else:
                self._offer_queue_locked(snapshot)
                self._condition.notify()
            should_warn = self._should_log_drop(
                dropped_before,
                self._dropped_count,
            )

        if should_warn:
            self._log_overflow()

    def take(self, *, timeout_seconds: float | None = None) -> ProgressSnapshot:
        """由单个连接发送循环阻塞获取下一条通知；超时抛出 ``TimeoutError``。"""

        if timeout_seconds is not None:
            if isinstance(timeout_seconds, bool) or not isinstance(
                timeout_seconds,
                (int, float),
            ):
                raise TypeError("timeout_seconds 必须是数字或 None")
            timeout_seconds = float(timeout_seconds)
            if timeout_seconds < 0:
                raise ValueError("timeout_seconds 不能小于 0")
        deadline = (
            monotonic() + timeout_seconds
            if timeout_seconds is not None
            else None
        )

        with self._condition:
            while self._active_batch is not None or not self._queue:
                if self._closed:
                    raise ProgressDeliveryClosedError("Progress 投递缓冲已关闭")
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("等待 Progress 通知超时")
                self._condition.wait(remaining)
            return self._queue.popleft()

    def drain(self, *, max_items: int | None = None) -> tuple[ProgressSnapshot, ...]:
        """非阻塞取出当前队列内容，主要供连接循环批量发送和确定性测试使用。"""

        if max_items is not None and (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or max_items <= 0
        ):
            raise ValueError("max_items 必须是正整数或 None")
        with self._condition:
            if self._active_batch is not None:
                return ()
            count = len(self._queue) if max_items is None else min(
                max_items,
                len(self._queue),
            )
            return tuple(self._queue.popleft() for _ in range(count))

    def close(self) -> None:
        """幂等关闭并释放所有尚未发送的连接级内存。"""

        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._active_batch = None
            self._pending.clear()
            self._queue.clear()
            self._accepted_watermarks.clear()
            self._condition.notify_all()

    def _require_active_token(self, token: ProgressInitialBatchToken) -> None:
        if not isinstance(token, ProgressInitialBatchToken):
            raise TypeError("token 必须是 ProgressInitialBatchToken")
        if token.delivery_id != self._delivery_id or token != self._active_batch:
            raise ProgressInitialBatchStateError("初始快照屏障令牌无效或已经使用")

    def _offer_pending_locked(self, snapshot: ProgressSnapshot) -> None:
        existing = self._pending.get(snapshot.key)
        if existing is not None:
            self._coalesced_count += 1
            if self._is_stale_or_equal(snapshot, existing):
                return
            self._pending[snapshot.key] = snapshot
            self._pending.move_to_end(snapshot.key)
            return
        if len(self._pending) >= self._capacity:
            self._pending.popitem(last=False)
            self._dropped_count += 1
        self._pending[snapshot.key] = snapshot

    def _offer_queue_locked(self, snapshot: ProgressSnapshot) -> None:
        if not self._accept_against_watermark_locked(snapshot):
            return
        self._offer_accepted_queue_locked(snapshot)

    def _offer_accepted_queue_locked(self, snapshot: ProgressSnapshot) -> None:
        """把已经通过水位检查的快照写入有界队列。"""

        for index, existing in enumerate(self._queue):
            if existing.key != snapshot.key:
                continue
            self._coalesced_count += 1
            if not self._is_stale_or_equal(snapshot, existing):
                del self._queue[index]
                self._queue.append(snapshot)
            return
        if len(self._queue) >= self._capacity:
            self._queue.popleft()
            self._dropped_count += 1
        self._queue.append(snapshot)

    def _accept_against_watermark_locked(
        self,
        snapshot: ProgressSnapshot,
    ) -> bool:
        """按连接已接受水位拒绝同一 TaskId 的重复或倒退通知。

        水位在通知入队时更新，而不是在网络发送完成后更新。这样即使队列合并、容量
        淘汰或连接线程已经取走新通知，随后迟到的旧回调也不能重新进入队列。

        不同 TaskId 之间没有可比较的全局序号，因此当前阶段仍允许切换水位；判断旧
        执行是否已过期需要任务库中的当前 execution_id，按既定计划在阶段 1C 实现。
        """

        current = self._accepted_watermarks.get(snapshot.key)
        if current is not None and self._is_stale_or_equal(snapshot, current):
            self._coalesced_count += 1
            return False
        self._accepted_watermarks[snapshot.key] = snapshot
        return True

    @staticmethod
    def _is_stale_or_equal(
        candidate: ProgressSnapshot,
        current: ProgressSnapshot,
    ) -> bool:
        return (
            candidate.task_id == current.task_id
            and candidate.sequence_no <= current.sequence_no
        )

    @staticmethod
    def _should_log_drop(before: int, after: int) -> bool:
        if after <= before:
            return False
        # 只记录第 1、2、4、8...次，防止慢连接制造日志风暴。
        return after == 1 or (after & (after - 1)) == 0

    def _log_overflow(self) -> None:
        logger.warning(
            "Progress 连接缓冲已饱和并合并/淘汰旧快照: delivery_id=%s "
            "capacity=%s queued_count=%s dropped_count=%s coalesced_count=%s",
            self._delivery_id,
            self._capacity,
            self.queued_count,
            self.dropped_count,
            self.coalesced_count,
        )


__all__ = [
    "ProgressDeliveryBuffer",
    "ProgressDeliveryClosedError",
    "ProgressInitialBatchStateError",
    "ProgressInitialBatchToken",
]
