"""Progress 当前快照选择与连接级订阅编排。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from app.modules.tasks.domain.models import (
    ProgressKey,
    ProgressSnapshot,
    ProgressSubscriptionRequest,
    TaskSnapshot,
)
from app.modules.tasks.ports.progress import (
    ProgressSnapshotPort,
    ProgressSubscription,
    ProgressSubscriptionPort,
)
from app.modules.tasks.ports.task_read import TaskReadPort

from .progress_delivery import (
    ProgressDeliveryBuffer,
    ProgressInitialBatchToken,
)


logger = logging.getLogger(__name__)


class ProgressSnapshotSource(str, Enum):
    """当前快照的内部来源，不属于 WebSocket 协议字段。"""

    PROGRESS = "progress"
    TASK = "task"
    MISSING = "missing"


@dataclass(frozen=True)
class CurrentProgressItem:
    """一个请求位置对应的当前快照选择结果。"""

    key: ProgressKey
    snapshot: ProgressSnapshot | None
    source: ProgressSnapshotSource

    def __post_init__(self) -> None:
        if not isinstance(self.key, ProgressKey):
            raise TypeError("key 必须是 ProgressKey")
        if not isinstance(self.source, ProgressSnapshotSource):
            raise TypeError("source 必须是 ProgressSnapshotSource")
        if self.source is ProgressSnapshotSource.MISSING:
            if self.snapshot is not None:
                raise ValueError("MISSING 来源不得包含快照")
            return
        if not isinstance(self.snapshot, ProgressSnapshot):
            raise TypeError("非 MISSING 来源必须包含 ProgressSnapshot")
        if self.snapshot.key != self.key:
            raise ValueError("当前快照的 key 与请求 key 不一致")

    @property
    def exists(self) -> bool:
        """供 Presenter 映射既有 ``exists=false`` 缺失语义。"""

        return self.snapshot is not None


@dataclass(frozen=True)
class ProgressSubscriptionResult:
    """待发送的有序快照与连接 Registry 需要保存的订阅变化。"""

    current_items: tuple[CurrentProgressItem, ...]
    active_subscriptions: tuple[ProgressSubscription, ...]
    added_subscriptions: tuple[ProgressSubscription, ...]
    _delivery: ProgressDeliveryBuffer = field(repr=False, compare=False)
    _initial_batch: ProgressInitialBatchToken = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        current_items = tuple(self.current_items)
        active = tuple(self.active_subscriptions)
        added = tuple(self.added_subscriptions)
        if not current_items:
            raise ValueError("current_items 不能为空")
        if any(not isinstance(item, CurrentProgressItem) for item in current_items):
            raise TypeError("current_items 只能包含 CurrentProgressItem")
        if any(not isinstance(item, ProgressSubscription) for item in active):
            raise TypeError("active_subscriptions 类型无效")
        if any(not isinstance(item, ProgressSubscription) for item in added):
            raise TypeError("added_subscriptions 类型无效")
        if not set(added).issubset(set(active)):
            raise ValueError("added_subscriptions 必须属于 active_subscriptions")
        if not isinstance(self._delivery, ProgressDeliveryBuffer):
            raise TypeError("_delivery 必须是 ProgressDeliveryBuffer")
        if not isinstance(self._initial_batch, ProgressInitialBatchToken):
            raise TypeError("_initial_batch 必须是 ProgressInitialBatchToken")
        if self._initial_batch.delivery_id != self._delivery.delivery_id:
            raise ValueError("初始快照屏障与连接投递器不一致")
        object.__setattr__(self, "current_items", current_items)
        object.__setattr__(self, "active_subscriptions", active)
        object.__setattr__(self, "added_subscriptions", added)

    @property
    def delivery_id(self) -> str:
        """返回连接级投递器身份，供 Web Adapter 的 Registry 做一致性检查。"""

        return self._delivery.delivery_id

    def complete_initial_delivery(self) -> None:
        """Web Adapter 成功发送全部 ``current_items`` 后放行并发通知。"""

        self._delivery.finish_initial_batch(
            self._initial_batch,
            authoritative_snapshots=tuple(
                item.snapshot
                for item in self.current_items
                if item.snapshot is not None
            ),
        )

    def abort_initial_delivery(self) -> None:
        """初始消息发送失败时撤销本批屏障；调用方随后应关闭连接并释放令牌。"""

        self._delivery.abort_initial_batch(self._initial_batch)


class ProgressPortContractError(RuntimeError):
    """Progress/Task Read Adapter 返回了与请求不一致的数据。"""


class ProgressSubscriptionReleaseError(RuntimeError):
    """连接关闭时一个或多个订阅令牌释放失败。"""

    def __init__(
        self,
        failed_subscriptions: tuple[ProgressSubscription, ...],
    ) -> None:
        self.failed_subscriptions = failed_subscriptions
        self.failed_subscription_ids = tuple(
            item.subscription_id for item in failed_subscriptions
        )
        super().__init__(
            "释放 Progress 订阅失败: " + ",".join(self.failed_subscription_ids)
        )


class ProgressSubscriptionRollbackError(RuntimeError):
    """订阅建立失败后，部分新增令牌又未能完成补偿释放。"""

    def __init__(
        self,
        *,
        original_error: BaseException,
        failed_subscriptions: tuple[ProgressSubscription, ...],
    ) -> None:
        self.original_error = original_error
        self.failed_subscriptions = failed_subscriptions
        self.failed_subscription_ids = tuple(
            item.subscription_id for item in failed_subscriptions
        )
        super().__init__(
            "建立 Progress 订阅失败且补偿释放不完整: "
            + ",".join(self.failed_subscription_ids)
        )


class ProgressSubscriptionService:
    """建立类型化订阅并选择每个 key 的当前快照。

    服务只返回 ``ProgressSnapshot`` 和订阅令牌，不持有连接、不序列化 JSON，也不
    调用 WebSocket ``send``。Web Adapter 将 ``active_subscriptions`` 保存到当前
    连接 Registry，并在 ``finally`` 中交给 :meth:`release` 幂等清理。
    """

    def __init__(
        self,
        *,
        progress_snapshots: ProgressSnapshotPort,
        progress_subscriptions: ProgressSubscriptionPort,
        task_reader: TaskReadPort,
    ) -> None:
        self._progress_snapshots = progress_snapshots
        self._progress_subscriptions = progress_subscriptions
        self._task_reader = task_reader

    def subscribe(
        self,
        request: ProgressSubscriptionRequest,
        *,
        delivery: ProgressDeliveryBuffer,
        existing_subscriptions: Sequence[ProgressSubscription] = (),
        connection_id: str = "",
    ) -> ProgressSubscriptionResult:
        """先建立缺失订阅，再按请求顺序生成当前快照。

        先注册再读取可避免“读取旧快照后、注册前”完全漏掉一次发布。应用服务会先
        打开连接投递器的初始屏障；Web Adapter 发送完 ``current_items`` 后必须调用
        ``complete_initial_delivery``，此时才按 task ID/sequence 放行并发通知。
        这些内部字段不会进入公开响应。
        """

        if not isinstance(request, ProgressSubscriptionRequest):
            raise TypeError("request 必须是 ProgressSubscriptionRequest")
        if not isinstance(delivery, ProgressDeliveryBuffer):
            raise TypeError("delivery 必须是 ProgressDeliveryBuffer")
        connection_label = str(connection_id or "").strip() or "-"
        active = list(existing_subscriptions)
        by_key: dict[ProgressKey, ProgressSubscription] = {}
        subscription_ids: set[str] = set()
        for subscription in active:
            if not isinstance(subscription, ProgressSubscription):
                raise TypeError("existing_subscriptions 类型无效")
            if subscription.key in by_key:
                raise ValueError("existing_subscriptions 中存在重复 key")
            if subscription.subscription_id in subscription_ids:
                raise ValueError("existing_subscriptions 中存在重复 subscription_id")
            if subscription.delivery_id != delivery.delivery_id:
                raise ValueError("existing_subscriptions 属于其他连接投递器")
            by_key[subscription.key] = subscription
            subscription_ids.add(subscription.subscription_id)

        logger.info(
            "开始建立 Progress 订阅: connection_id=%s business_type=%s "
            "key_count=%s existing_count=%s",
            connection_label,
            request.business_type,
            len(request.ordered_keys),
            len(active),
        )
        added: list[ProgressSubscription] = []
        initial_batch = delivery.begin_initial_batch()
        try:
            # 同一帧中重复 key 仍产生重复的当前快照位置，但只建立一条底层订阅，
            # 避免一次连接收到成倍的后续通知。
            for key in request.ordered_keys:
                if key in by_key:
                    continue
                subscription = self._progress_subscriptions.subscribe(
                    key,
                    delivery.publish,
                    delivery_id=delivery.delivery_id,
                )
                if not isinstance(subscription, ProgressSubscription):
                    raise ProgressPortContractError(
                        "ProgressSubscriptionPort.subscribe 返回类型无效"
                    )
                # 先放入 added，确保令牌字段不合法时也会进入补偿释放。
                added.append(subscription)
                if subscription.key != key:
                    raise ProgressPortContractError(
                        "ProgressSubscriptionPort 返回了错误 key"
                    )
                if subscription.subscription_id in subscription_ids:
                    raise ProgressPortContractError(
                        "ProgressSubscriptionPort 返回了重复 subscription_id"
                    )
                if subscription.delivery_id != delivery.delivery_id:
                    raise ProgressPortContractError(
                        "ProgressSubscriptionPort 返回了错误 delivery_id"
                    )
                subscription_ids.add(subscription.subscription_id)
                by_key[key] = subscription
                active.append(subscription)

            current_items = tuple(
                self._select_current(key) for key in request.ordered_keys
            )
        except Exception as exc:
            delivery.abort_initial_batch(initial_batch)
            failed_rollbacks = self._rollback_added(
                added,
                connection_id=connection_label,
            )
            logger.exception(
                "建立 Progress 订阅失败，已尝试补偿本次新增令牌: "
                "connection_id=%s added_count=%s",
                connection_label,
                len(added),
            )
            if failed_rollbacks:
                raise ProgressSubscriptionRollbackError(
                    original_error=exc,
                    failed_subscriptions=failed_rollbacks,
                ) from exc
            raise

        logger.info(
            "Progress 订阅建立完成: connection_id=%s key_count=%s "
            "added_count=%s active_count=%s",
            connection_label,
            len(current_items),
            len(added),
            len(active),
        )
        return ProgressSubscriptionResult(
            current_items=current_items,
            active_subscriptions=tuple(active),
            added_subscriptions=tuple(added),
            _delivery=delivery,
            _initial_batch=initial_batch,
        )

    def release(
        self,
        subscriptions: Sequence[ProgressSubscription],
        *,
        connection_id: str = "",
    ) -> None:
        """尽量释放全部订阅，并在最后统一报告失败。

        Port 的 ``unsubscribe`` 必须幂等，因此 Web Adapter 可以在异常路径和
        ``finally`` 中重复调用。这里会去重同一令牌，但不会因某一项失败而跳过其余
        令牌，防止一个坏订阅让整个连接的回调引用残留。
        """

        connection_label = str(connection_id or "").strip() or "-"
        unique: list[ProgressSubscription] = []
        seen: set[str] = set()
        for subscription in subscriptions:
            if not isinstance(subscription, ProgressSubscription):
                raise TypeError("subscriptions 类型无效")
            if subscription.subscription_id in seen:
                continue
            seen.add(subscription.subscription_id)
            unique.append(subscription)

        failed: list[ProgressSubscription] = []
        for subscription in unique:
            try:
                self._progress_subscriptions.unsubscribe(subscription)
            except Exception:
                failed.append(subscription)
                logger.exception(
                    "释放 Progress 订阅失败: connection_id=%s "
                    "subscription_id=%s business_type=%s business_key=%s",
                    connection_label,
                    subscription.subscription_id,
                    subscription.key.business_type,
                    subscription.key.business_key,
                )
        logger.info(
            "Progress 连接订阅释放完成: connection_id=%s requested_count=%s "
            "failed_count=%s",
            connection_label,
            len(unique),
            len(failed),
        )
        if failed:
            raise ProgressSubscriptionReleaseError(tuple(failed))

    def _select_current(self, key: ProgressKey) -> CurrentProgressItem:
        progress_snapshot = self._progress_snapshots.get_latest(key)
        if progress_snapshot is not None:
            if not isinstance(progress_snapshot, ProgressSnapshot):
                raise ProgressPortContractError(
                    "ProgressSnapshotPort.get_latest 返回类型无效"
                )
            if progress_snapshot.key != key:
                raise ProgressPortContractError(
                    "ProgressSnapshotPort.get_latest 返回了错误 key"
                )
            logger.debug(
                "Progress 当前快照命中通知层: business_type=%s business_key=%s "
                "task_id=%s sequence_no=%s",
                key.business_type,
                key.business_key,
                progress_snapshot.task_id,
                progress_snapshot.sequence_no,
            )
            return CurrentProgressItem(
                key=key,
                snapshot=progress_snapshot,
                source=ProgressSnapshotSource.PROGRESS,
            )

        task_snapshot = self._task_reader.get_latest(key.business_ref)
        if task_snapshot is None:
            logger.debug(
                "Progress 当前快照未命中: business_type=%s business_key=%s",
                key.business_type,
                key.business_key,
            )
            return CurrentProgressItem(
                key=key,
                snapshot=None,
                source=ProgressSnapshotSource.MISSING,
            )
        self._validate_task_fallback(task_snapshot, key)
        # 兼容任务投影没有 progress 事件序号，内部使用 0 表示“非事件快照”。该值
        # 只用于应用层排序和测试，不会由 Presenter 输出。
        fallback_snapshot = ProgressSnapshot(
            key=key,
            task_id=task_snapshot.task_id,
            progress=task_snapshot.progress,
            message=task_snapshot.message,
            internal_state=task_snapshot.execution_state,
            sequence_no=0,
            updated_at=task_snapshot.updated_at,
        )
        logger.debug(
            "Progress 当前快照回退任务投影: business_type=%s business_key=%s "
            "task_id=%s",
            key.business_type,
            key.business_key,
            task_snapshot.task_id,
        )
        return CurrentProgressItem(
            key=key,
            snapshot=fallback_snapshot,
            source=ProgressSnapshotSource.TASK,
        )

    @staticmethod
    def _validate_task_fallback(snapshot: TaskSnapshot, key: ProgressKey) -> None:
        if not isinstance(snapshot, TaskSnapshot):
            raise ProgressPortContractError(
                "TaskReadPort.get_latest 返回了非 TaskSnapshot"
            )
        if snapshot.business_ref != key.business_ref:
            raise ProgressPortContractError(
                "TaskReadPort.get_latest 返回了错误业务键"
            )

    def _rollback_added(
        self,
        added: Sequence[ProgressSubscription],
        *,
        connection_id: str,
    ) -> tuple[ProgressSubscription, ...]:
        """逆序补偿本次新增令牌，并返回仍需由连接 Registry 重试的令牌。"""

        failed: list[ProgressSubscription] = []
        for subscription in reversed(tuple(added)):
            try:
                self._progress_subscriptions.unsubscribe(subscription)
            except Exception:
                failed.append(subscription)
                logger.exception(
                    "补偿释放 Progress 订阅失败: connection_id=%s "
                    "subscription_id=%s",
                    connection_id,
                    getattr(subscription, "subscription_id", "unknown"),
                )
        return tuple(failed)


__all__ = [
    "CurrentProgressItem",
    "ProgressPortContractError",
    "ProgressSnapshotSource",
    "ProgressSubscriptionRollbackError",
    "ProgressSubscriptionReleaseError",
    "ProgressSubscriptionResult",
    "ProgressSubscriptionService",
]
