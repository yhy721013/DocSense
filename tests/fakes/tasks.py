"""任务模块 Port 的可编程内存测试替身。

这些替身只验证应用编排，不模拟 SQLite、HTTP 或 WebSocket 协议。调用记录和故障
注入均显式暴露，避免测试因为替身自动“帮忙修正”错误契约而产生假通过。
"""

from __future__ import annotations

from itertools import count
from threading import RLock
from typing import Sequence

from app.modules.tasks.domain import (
    ProgressKey,
    ProgressSnapshot,
    TaskBusinessRef,
    TaskId,
    TaskSnapshot,
)
from app.modules.tasks.ports import (
    CallbackRecoveryResult,
    ProgressSubscriber,
    ProgressSubscription,
)


_UNSET = object()


class FakeTaskReadPort:
    """按 TaskId 保存历史快照、按业务键保存最新投影的只读替身。"""

    def __init__(self, snapshots: Sequence[TaskSnapshot] = ()) -> None:
        self._by_id: dict[TaskId, TaskSnapshot] = {}
        self._latest_ids: dict[TaskBusinessRef, TaskId] = {}
        self.by_id_calls: list[TaskId] = []
        self.latest_calls: list[TaskBusinessRef] = []
        self.latest_many_calls: list[tuple[TaskBusinessRef, ...]] = []
        self.forced_many_result: tuple[TaskSnapshot | None, ...] | None = None
        self.latest_errors: dict[TaskBusinessRef, BaseException] = {}
        for snapshot in snapshots:
            self.put(snapshot)

    def put(self, snapshot: TaskSnapshot, *, as_latest: bool = True) -> None:
        """写入测试快照；默认把它设为业务键的最新投影。"""

        if not isinstance(snapshot, TaskSnapshot):
            raise TypeError("snapshot 必须是 TaskSnapshot")
        self._by_id[snapshot.task_id] = snapshot
        if as_latest:
            self._latest_ids[snapshot.business_ref] = snapshot.task_id

    def replace_by_id(self, snapshot: TaskSnapshot) -> None:
        """更新同一 TaskId，并保持原有最新投影归属。"""

        if snapshot.task_id not in self._by_id:
            raise KeyError(f"未知 TaskId: {snapshot.task_id}")
        previous = self._by_id[snapshot.task_id]
        self._by_id[snapshot.task_id] = snapshot
        if self._latest_ids.get(previous.business_ref) == snapshot.task_id:
            self._latest_ids.pop(previous.business_ref, None)
            self._latest_ids[snapshot.business_ref] = snapshot.task_id

    def set_latest_for_ref(
        self,
        requested_ref: TaskBusinessRef,
        snapshot: TaskSnapshot,
    ) -> None:
        """允许故意配置业务引用不一致的快照，以验证生产端口门禁。"""

        if not isinstance(snapshot, TaskSnapshot):
            raise TypeError("snapshot 必须是 TaskSnapshot")
        self._by_id[snapshot.task_id] = snapshot
        self._latest_ids[requested_ref] = snapshot.task_id

    def remove_by_id(self, task_id: TaskId) -> None:
        """移除 TaskId，用于模拟恢复后读取丢失等端口异常。"""

        snapshot = self._by_id.pop(task_id, None)
        if snapshot is None:
            return
        if self._latest_ids.get(snapshot.business_ref) == task_id:
            self._latest_ids.pop(snapshot.business_ref, None)

    def get_by_id(self, task_id: TaskId) -> TaskSnapshot | None:
        self.by_id_calls.append(task_id)
        return self._by_id.get(task_id)

    def get_latest(self, business_ref: TaskBusinessRef) -> TaskSnapshot | None:
        self.latest_calls.append(business_ref)
        error = self.latest_errors.get(business_ref)
        if error is not None:
            raise error
        task_id = self._latest_ids.get(business_ref)
        return self._by_id.get(task_id) if task_id is not None else None

    def get_latest_many(
        self,
        business_refs: tuple[TaskBusinessRef, ...],
    ) -> tuple[TaskSnapshot | None, ...]:
        refs = tuple(business_refs)
        self.latest_many_calls.append(refs)
        if self.forced_many_result is not None:
            return self.forced_many_result
        return tuple(
            self._by_id.get(task_id) if task_id is not None else None
            for task_id in (self._latest_ids.get(ref) for ref in refs)
        )


class FakeCallbackRecoveryPort:
    """按 TaskId 配置恢复结果、快照写入或异常的替身。"""

    def __init__(self, task_reader: FakeTaskReadPort) -> None:
        self._task_reader = task_reader
        self._results: dict[TaskId, CallbackRecoveryResult] = {}
        self._updates: dict[TaskId, TaskSnapshot | None | object] = {}
        self._errors: dict[TaskId, BaseException] = {}
        self.recovery_calls: list[TaskId] = []

    def configure(
        self,
        task_id: TaskId,
        *,
        result: CallbackRecoveryResult | None = None,
        updated_snapshot: TaskSnapshot | None | object = _UNSET,
        error: BaseException | None = None,
    ) -> None:
        """配置一次恢复；``updated_snapshot=None`` 表示模拟读取目标丢失。"""

        if result is None and error is None:
            raise ValueError("result 和 error 至少配置一项")
        if result is not None:
            self._results[task_id] = result
        if updated_snapshot is not _UNSET:
            self._updates[task_id] = updated_snapshot
        if error is not None:
            self._errors[task_id] = error

    def recover_if_needed(self, task_id: TaskId) -> CallbackRecoveryResult:
        self.recovery_calls.append(task_id)
        error = self._errors.get(task_id)
        if error is not None:
            raise error
        if task_id not in self._results:
            raise AssertionError(f"未配置 TaskId={task_id} 的恢复结果")
        update = self._updates.get(task_id, _UNSET)
        if update is None:
            self._task_reader.remove_by_id(task_id)
        elif update is not _UNSET:
            assert isinstance(update, TaskSnapshot)
            self._task_reader.replace_by_id(update)
        return self._results[task_id]


class FakeProgressSnapshotPort:
    """可按请求 key 返回任意快照或异常的 Progress Snapshot 替身。"""

    def __init__(self, snapshots: Sequence[ProgressSnapshot] = ()) -> None:
        self._snapshots = {snapshot.key: snapshot for snapshot in snapshots}
        self.errors: dict[ProgressKey, BaseException] = {}
        self.calls: list[ProgressKey] = []

    def set_for_key(
        self,
        requested_key: ProgressKey,
        snapshot: ProgressSnapshot,
    ) -> None:
        """允许故意配置 key 不一致的快照，以验证生产端口门禁。"""

        self._snapshots[requested_key] = snapshot

    def get_latest(self, key: ProgressKey) -> ProgressSnapshot | None:
        self.calls.append(key)
        error = self.errors.get(key)
        if error is not None:
            raise error
        return self._snapshots.get(key)


class FakeProgressSubscriptionPort:
    """记录订阅生命周期并支持发布类型化快照的内存替身。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._sequence = count(1)
        self._active: dict[
            str,
            tuple[ProgressSubscription, ProgressSubscriber],
        ] = {}
        self.subscribe_errors: dict[ProgressKey, BaseException] = {}
        self.unsubscribe_errors: dict[str, BaseException] = {}
        self.forced_subscriptions: dict[ProgressKey, ProgressSubscription] = {}
        self.subscribe_calls: list[ProgressKey] = []
        self.unsubscribe_calls: list[str] = []

    @property
    def active_subscriptions(self) -> tuple[ProgressSubscription, ...]:
        with self._lock:
            return tuple(item[0] for item in self._active.values())

    def subscribe(
        self,
        key: ProgressKey,
        subscriber: ProgressSubscriber,
        *,
        delivery_id: str,
    ) -> ProgressSubscription:
        self.subscribe_calls.append(key)
        error = self.subscribe_errors.get(key)
        if error is not None:
            raise error
        subscription = self.forced_subscriptions.get(key)
        if subscription is None:
            subscription = ProgressSubscription(
                subscription_id=f"fake-progress-{next(self._sequence)}",
                key=key,
                delivery_id=delivery_id,
            )
        with self._lock:
            self._active[subscription.subscription_id] = (
                subscription,
                subscriber,
            )
        return subscription

    def unsubscribe(self, subscription: ProgressSubscription) -> None:
        self.unsubscribe_calls.append(subscription.subscription_id)
        error = self.unsubscribe_errors.get(subscription.subscription_id)
        if error is not None:
            raise error
        with self._lock:
            self._active.pop(subscription.subscription_id, None)

    def publish(self, snapshot: ProgressSnapshot) -> None:
        """在锁外调用当前 key 的订阅者，便于应用测试观察通知。"""

        with self._lock:
            subscribers = tuple(
                subscriber
                for subscription, subscriber in self._active.values()
                if subscription.key == snapshot.key
            )
        for subscriber in subscribers:
            subscriber(snapshot)


__all__ = [
    "FakeCallbackRecoveryPort",
    "FakeProgressSnapshotPort",
    "FakeProgressSubscriptionPort",
    "FakeTaskReadPort",
]
