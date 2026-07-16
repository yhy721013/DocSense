"""线程安全 ``LLMProgressHub`` 到 Progress Port 的兼容适配器。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import RLock
from typing import Callable
from uuid import uuid4

from app.modules.tasks.domain import ProgressKey, ProgressSnapshot, TaskId
from app.modules.tasks.ports import (
    ProgressSubscriber,
    ProgressSubscription,
)
from app.services.core.progress import normalize_progress
from app.services.core.progress_hub import LLMProgressHub, ProgressHubEvent


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Registration:
    """Adapter 内部令牌到 Hub 回调的绑定。"""

    subscription: ProgressSubscription
    callback: Callable[[ProgressHubEvent], None]


class InMemoryProgressAdapter:
    """用单一权威 Hub 实现类型化快照读取与订阅端口。

    Adapter 自身只保存不透明订阅令牌；latest、序号和订阅者集合仍由注入的同一个
    ``LLMProgressHub`` 管理。这样旧发布方和新应用服务不会各维护一份内存状态。
    """

    def __init__(self, hub: LLMProgressHub) -> None:
        if not isinstance(hub, LLMProgressHub):
            raise TypeError("hub 必须是 LLMProgressHub")
        self._hub = hub
        self._lock = RLock()
        self._registrations: dict[str, _Registration] = {}

    @property
    def active_subscription_count(self) -> int:
        """返回测试与运行指标使用的当前令牌数。"""

        with self._lock:
            return len(self._registrations)

    def get_latest(self, key: ProgressKey) -> ProgressSnapshot | None:
        if not isinstance(key, ProgressKey):
            raise TypeError("key 必须是 ProgressKey")
        event = self._hub.get_latest_event(
            key.business_type,
            key.business_key,
        )
        return self._to_snapshot(event, expected_key=key) if event is not None else None

    def subscribe(
        self,
        key: ProgressKey,
        subscriber: ProgressSubscriber,
        *,
        delivery_id: str,
    ) -> ProgressSubscription:
        if not isinstance(key, ProgressKey):
            raise TypeError("key 必须是 ProgressKey")
        if not callable(subscriber):
            raise TypeError("subscriber 必须可调用")

        subscription = ProgressSubscription(
            subscription_id=f"progress-{uuid4().hex}",
            key=key,
            delivery_id=delivery_id,
        )

        def _forward(event: ProgressHubEvent) -> None:
            # Hub 已保证在内部锁外调用本函数。这里只完成纯内存 DTO 转换和连接缓冲
            # 入队，绝不执行 ws.send、数据库查询或外部网络请求。
            try:
                snapshot = self._to_snapshot(event, expected_key=key)
                subscriber(snapshot)
            except Exception:
                logger.exception(
                    "Progress subscriber 执行失败，已隔离: subscription_id=%s "
                    "business_type=%s business_key=%s delivery_id=%s",
                    subscription.subscription_id,
                    key.business_type,
                    key.business_key,
                    subscription.delivery_id,
                )

        registration = _Registration(
            subscription=subscription,
            callback=_forward,
        )
        with self._lock:
            self._registrations[subscription.subscription_id] = registration
        try:
            # 应用服务会在注册完成后显式读取 latest，并通过初始快照屏障排序；此处
            # 禁止 Hub 自动重放，否则同一快照会进入两条投递路径。
            self._hub.subscribe_event(
                key.business_type,
                key.business_key,
                _forward,
                replay_latest=False,
            )
        except Exception:
            with self._lock:
                self._registrations.pop(subscription.subscription_id, None)
            raise

        logger.debug(
            "Progress 类型化订阅已建立: subscription_id=%s business_type=%s "
            "business_key=%s delivery_id=%s active_count=%s",
            subscription.subscription_id,
            key.business_type,
            key.business_key,
            subscription.delivery_id,
            self.active_subscription_count,
        )
        return subscription

    def unsubscribe(self, subscription: ProgressSubscription) -> None:
        """幂等释放令牌；Hub 成功移除后才删除 Registry，便于失败重试。"""

        if not isinstance(subscription, ProgressSubscription):
            raise TypeError("subscription 必须是 ProgressSubscription")
        with self._lock:
            registration = self._registrations.get(subscription.subscription_id)
        if registration is None:
            return
        if registration.subscription != subscription:
            raise ValueError("subscription_id 与已登记令牌内容不一致")

        self._hub.unsubscribe_event(
            subscription.key.business_type,
            subscription.key.business_key,
            registration.callback,
        )
        with self._lock:
            current = self._registrations.get(subscription.subscription_id)
            if current == registration:
                self._registrations.pop(subscription.subscription_id, None)
        logger.debug(
            "Progress 类型化订阅已释放: subscription_id=%s active_count=%s",
            subscription.subscription_id,
            self.active_subscription_count,
        )

    @staticmethod
    def _to_snapshot(
        event: ProgressHubEvent,
        *,
        expected_key: ProgressKey,
    ) -> ProgressSnapshot:
        if not isinstance(event, ProgressHubEvent):
            raise TypeError("event 必须是 ProgressHubEvent")
        actual_key = ProgressKey(event.business_type, event.business_key)
        if actual_key != expected_key:
            raise ValueError("Hub 事件业务键与订阅键不一致")

        data = event.payload.get("data")
        if not isinstance(data, dict):
            data = {}
        raw_message = data.get("message", event.payload.get("message", ""))
        message = raw_message if isinstance(raw_message, str) else ""
        raw_state = data.get("state", event.payload.get("state", ""))
        internal_state = (
            raw_state.strip()
            if isinstance(raw_state, str) and raw_state.strip()
            else "legacy_progress"
        )
        return ProgressSnapshot(
            key=actual_key,
            task_id=TaskId(event.task_id),
            progress=normalize_progress(data.get("progress", 0.0)),
            message=message,
            internal_state=internal_state,
            sequence_no=event.sequence_no,
            updated_at=event.updated_at,
        )


__all__ = ["InMemoryProgressAdapter"]
