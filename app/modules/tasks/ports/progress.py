"""进度快照与订阅端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain.models import ProgressKey, ProgressSnapshot


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


@dataclass(frozen=True)
class ProgressSubscription:
    """订阅实现返回给连接级 Registry 保存的不透明令牌。"""

    subscription_id: str
    key: ProgressKey
    delivery_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscription_id",
            _required_text(self.subscription_id, name="subscription_id"),
        )
        if not isinstance(self.key, ProgressKey):
            raise TypeError("key 必须是 ProgressKey")
        object.__setattr__(
            self,
            "delivery_id",
            _required_text(self.delivery_id, name="delivery_id"),
        )


class ProgressSubscriber(Protocol):
    """接收类型化进度快照的回调，不代表 WebSocket ``send`` 本身。"""

    def __call__(self, snapshot: ProgressSnapshot) -> None:
        ...


@runtime_checkable
class ProgressSnapshotPort(Protocol):
    """读取进程内或共享通知层当前最新进度快照。"""

    def get_latest(self, key: ProgressKey) -> ProgressSnapshot | None:
        """返回目标键的最新快照；不存在时返回 ``None``。"""
        ...


@runtime_checkable
class ProgressSubscriptionPort(Protocol):
    """注册和释放进度通知的抽象边界。

    具体实现必须线程安全，不得在持有内部锁时调用 ``subscriber``，并且必须保证
    ``unsubscribe`` 幂等。发布时必须先更新 ``get_latest`` 可见的快照，再通知
    subscriber；初始快照屏障依赖该顺序过滤重复/过期通知。单个订阅者异常的隔离由
    阶段 1B 的 InMemory Adapter 实现并测试。
    """

    def subscribe(
        self,
        key: ProgressKey,
        subscriber: ProgressSubscriber,
        *,
        delivery_id: str,
    ) -> ProgressSubscription:
        """为一个键注册回调，并把令牌绑定到连接级投递器。"""
        ...

    def unsubscribe(self, subscription: ProgressSubscription) -> None:
        """幂等释放一个订阅令牌。"""
        ...


__all__ = [
    "ProgressSnapshotPort",
    "ProgressSubscriber",
    "ProgressSubscription",
    "ProgressSubscriptionPort",
]
