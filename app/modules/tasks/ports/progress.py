"""进度快照与订阅端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from app.modules.tasks.domain.models import ProgressKey, ProgressSnapshot, TaskId


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


@dataclass(frozen=True)
class ProgressPublication:
    """业务 Application 交给 Progress 通知层的类型化更新。

    该对象不包含 WebSocket JSON 字典；具体 Adapter/Presenter 根据 ``ProgressKey`` 映射
    既有公开字段。``expected_task_id`` 仅用于抑制旧执行通知，禁止进入公开载荷。
    """

    key: ProgressKey
    expected_task_id: TaskId
    progress: float
    message: str
    internal_state: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, ProgressKey):
            raise TypeError("key 必须是 ProgressKey")
        if not isinstance(self.expected_task_id, TaskId):
            raise TypeError("expected_task_id 必须是 TaskId")
        # 复用 ProgressSnapshot 的领域校验，避免两个 Port 对进度范围产生分歧。
        validated = ProgressSnapshot(
            key=self.key,
            task_id=self.expected_task_id,
            progress=self.progress,
            message=self.message,
            internal_state=self.internal_state,
            sequence_no=0,
            updated_at="port-validation",
        )
        object.__setattr__(self, "progress", validated.progress)
        object.__setattr__(self, "message", validated.message)
        object.__setattr__(self, "internal_state", validated.internal_state)


@runtime_checkable
class ProgressPublisherPort(Protocol):
    """发布已完成条件持久化的最新 Progress 通知。"""

    def publish(self, publication: ProgressPublication) -> None:
        """通知订阅者；不得把通知失败误当成任务事实回滚。"""
        ...


@runtime_checkable
class GuardedProgressPublisherPort(Protocol):
    """在通知投影的原子临界区内复核持久化 owner 后发布。

    ``is_current`` 必须是只读、无外部副作用且不持有数据库写事务的判断。实现需要按
    业务键在“复核 owner”与“更新 latest 投影”之间禁止同键发布插入，才能封闭旧
    accepted 在预检查后迟到覆盖新任务的竞态；不同键不得被慢 Repository 查询全局
    阻塞。返回 ``False`` 表示 owner 已变化，未写入投影。
    """

    def publish_guarded(
        self,
        publication: ProgressPublication,
        *,
        is_current: Callable[[], bool],
    ) -> bool:
        ...


__all__ = [
    "GuardedProgressPublisherPort",
    "ProgressPublication",
    "ProgressPublisherPort",
    "ProgressSnapshotPort",
    "ProgressSubscriber",
    "ProgressSubscription",
    "ProgressSubscriptionPort",
]
