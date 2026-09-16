"""Progress WebSocket 连接级 Registry 与投递缓冲所有权。"""

from __future__ import annotations

import logging
from threading import RLock

from app.modules.tasks.application import (
    ProgressDeliveryBuffer,
    ProgressSubscriptionReleaseError,
    ProgressSubscriptionResult,
    ProgressSubscriptionService,
)
from app.modules.tasks.ports import ProgressSubscription


logger = logging.getLogger(__name__)


class ProgressConnectionRegistry:
    """保存单个连接的唯一缓冲和全部待释放订阅令牌。

    Registry 不持有 WebSocket，也不发送消息。释放失败的令牌会继续留在内部，后续
    重试只处理尚未成功释放的部分，避免 ``finally`` 仅写日志后遗失回调引用。
    """

    def __init__(
        self,
        *,
        connection_id: str,
        delivery_capacity: int = ProgressDeliveryBuffer.DEFAULT_CAPACITY,
    ) -> None:
        normalized_id = str(connection_id or "").strip()
        if not normalized_id:
            raise ValueError("connection_id 不能为空")
        self._connection_id = normalized_id
        self._delivery = ProgressDeliveryBuffer(
            delivery_id=normalized_id,
            capacity=delivery_capacity,
        )
        self._lock = RLock()
        self._subscriptions: dict[str, ProgressSubscription] = {}

    @property
    def connection_id(self) -> str:
        return self._connection_id

    @property
    def delivery(self) -> ProgressDeliveryBuffer:
        return self._delivery

    @property
    def subscriptions(self) -> tuple[ProgressSubscription, ...]:
        with self._lock:
            return tuple(self._subscriptions.values())

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    def register_result(self, result: ProgressSubscriptionResult) -> None:
        """在发送初始快照前保存应用服务返回的全部活动令牌。"""

        if not isinstance(result, ProgressSubscriptionResult):
            raise TypeError("result 必须是 ProgressSubscriptionResult")
        if result.delivery_id != self._delivery.delivery_id:
            raise ValueError("订阅结果属于其他连接投递器")
        self.retain(result.active_subscriptions)

    def retain(self, subscriptions: tuple[ProgressSubscription, ...]) -> None:
        """保存需要在连接清理时释放或重试的令牌。"""

        tokens = tuple(subscriptions)
        for token in tokens:
            if not isinstance(token, ProgressSubscription):
                raise TypeError("subscriptions 只能包含 ProgressSubscription")
            if token.delivery_id != self._delivery.delivery_id:
                raise ValueError("不能保存其他连接的 Progress 订阅")
        with self._lock:
            for token in tokens:
                existing = self._subscriptions.get(token.subscription_id)
                if existing is not None and existing != token:
                    raise ValueError("subscription_id 对应的令牌内容冲突")
                self._subscriptions[token.subscription_id] = token

    def release_once(self, service: ProgressSubscriptionService) -> None:
        """尝试释放当前全部令牌；失败项继续保留以供下一次调用重试。"""

        if not isinstance(service, ProgressSubscriptionService):
            raise TypeError("service 必须是 ProgressSubscriptionService")
        attempted = self.subscriptions
        if not attempted:
            return
        try:
            service.release(
                attempted,
                connection_id=self._connection_id,
            )
        except ProgressSubscriptionReleaseError as exc:
            failed_ids = set(exc.failed_subscription_ids)
            with self._lock:
                for token in attempted:
                    if token.subscription_id not in failed_ids:
                        self._subscriptions.pop(token.subscription_id, None)
            raise
        else:
            with self._lock:
                for token in attempted:
                    self._subscriptions.pop(token.subscription_id, None)

    def close_and_release(
        self,
        service: ProgressSubscriptionService,
        *,
        max_attempts: int = 3,
    ) -> tuple[ProgressSubscription, ...]:
        """关闭缓冲并有限重试释放，返回仍失败的令牌供告警定位。"""

        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("max_attempts 必须是整数")
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于等于 1")

        # 先关闭缓冲，使已被发布线程复制出来的迟到回调也只做无害返回。
        self._delivery.close()
        for attempt in range(1, max_attempts + 1):
            if not self.subscriptions:
                break
            try:
                self.release_once(service)
            except ProgressSubscriptionReleaseError:
                logger.warning(
                    "Progress 连接订阅释放未完成，保留失败令牌重试: "
                    "connection_id=%s attempt=%s max_attempts=%s remaining_count=%s",
                    self._connection_id,
                    attempt,
                    max_attempts,
                    self.active_count,
                )
            except Exception:
                # 未知异常下无法判断哪些令牌成功，必须全部保留，不能猜测删除。
                logger.exception(
                    "Progress 连接订阅释放发生未知异常: connection_id=%s "
                    "attempt=%s remaining_count=%s",
                    self._connection_id,
                    attempt,
                    self.active_count,
                )
                break
        return self.subscriptions


__all__ = ["ProgressConnectionRegistry"]
