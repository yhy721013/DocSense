"""甲方 check-task 触发的武器谱回调同步恢复用例。"""

from __future__ import annotations

import logging

from app.modules.weaponry.domain import normalize_architecture_id_value
from app.modules.weaponry.ports import (
    AcquireWeaponryCallback,
    DeliverWeaponryCallback,
    WeaponryCallbackAcquireOutcome,
    WeaponryCallbackAcquireReason,
    WeaponryCallbackAcquireResult,
    WeaponryCallbackDeliveryOutcome,
    WeaponryCallbackDeliveryResult,
    WeaponryCallbackPort,
    WeaponryCallbackRecoverySourcePort,
)

from .errors import WeaponryPortContractError


logger = logging.getLogger(__name__)


class RecoverWeaponryCallbackSynchronously:
    """保留 check-task 同步副作用，同时复用正常 Worker 的同一 Callback Guard。"""

    def __init__(
        self,
        *,
        source: WeaponryCallbackRecoverySourcePort,
        callbacks: WeaponryCallbackPort,
    ) -> None:
        if not isinstance(source, WeaponryCallbackRecoverySourcePort):
            raise TypeError("source 必须实现 WeaponryCallbackRecoverySourcePort")
        if not isinstance(callbacks, WeaponryCallbackPort):
            raise TypeError("callbacks 必须实现 WeaponryCallbackPort")
        self._source = source
        self._callbacks = callbacks

    @property
    def callbacks(self) -> WeaponryCallbackPort:
        """供组合根验证同步入口与正常 Worker 共用 Guard 实例。"""

        return self._callbacks

    def execute(self, architecture_id: int) -> bool:
        """同步尝试一次；只有本次 HTTP 收到严格 2xx 才返回 ``True``。"""

        normalized_id = normalize_architecture_id_value(architecture_id)
        candidate = self._source.load_recoverable(normalized_id)
        if candidate is None:
            logger.debug(
                "武器谱同步回调无需恢复: architecture_id=%s",
                normalized_id,
            )
            return False
        acquire = self._callbacks.acquire(
            AcquireWeaponryCallback(
                task_id=candidate.task_id,
                architecture_id=candidate.architecture_id,
                reason=(
                    WeaponryCallbackAcquireReason.EXPLICIT_CHECK_TASK_RECOVERY
                ),
            )
        )
        if not isinstance(acquire, WeaponryCallbackAcquireResult):
            raise WeaponryPortContractError("Callback acquire 返回类型错误")
        if acquire.outcome is not WeaponryCallbackAcquireOutcome.ACQUIRED:
            logger.info(
                "武器谱同步回调未取得发送权: task_id=%s architecture_id=%s outcome=%s",
                candidate.task_id.value,
                normalized_id,
                acquire.outcome.value,
            )
            return False
        lease = acquire.lease
        if lease is None or lease.task_id != candidate.task_id:
            raise WeaponryPortContractError("Callback Guard Lease 与恢复任务不一致")

        delivery = self._callbacks.deliver(
            DeliverWeaponryCallback(lease=lease, payload=candidate.payload)
        )
        if not isinstance(delivery, WeaponryCallbackDeliveryResult):
            raise WeaponryPortContractError("Callback deliver 返回类型错误")
        if delivery.outcome is WeaponryCallbackDeliveryOutcome.STALE:
            logger.info(
                "武器谱同步回调在发送前变为过期: task_id=%s architecture_id=%s",
                candidate.task_id.value,
                normalized_id,
            )
            return False

        completed = self._callbacks.complete(lease, delivery, candidate.payload)
        if not isinstance(completed, bool):
            raise WeaponryPortContractError("Callback complete 必须返回 bool")
        if not completed:
            raise WeaponryPortContractError("Callback Guard 完成权已过期")

        replayed = delivery.outcome is WeaponryCallbackDeliveryOutcome.SUCCESS
        logger.log(
            logging.INFO if replayed else logging.WARNING,
            "武器谱同步回调恢复完成: task_id=%s architecture_id=%s outcome=%s",
            candidate.task_id.value,
            normalized_id,
            delivery.outcome.value,
        )
        return replayed


class FreezeExpiredWeaponryCallbackGuards:
    """Dispatcher 独立维护线程使用的有界过期 Guard 冻结入口。"""

    def __init__(self, callbacks: WeaponryCallbackPort) -> None:
        if not isinstance(callbacks, WeaponryCallbackPort):
            raise TypeError("callbacks 必须实现 WeaponryCallbackPort")
        self._callbacks = callbacks

    @property
    def callbacks(self) -> WeaponryCallbackPort:
        return self._callbacks

    def run_once(self, *, limit: int) -> object:
        result = self._callbacks.freeze_expired(limit=limit)
        logger.log(
            logging.WARNING if result.frozen_count else logging.DEBUG,
            "武器谱 Callback Guard 有界扫描完成: limit=%d scanned=%d frozen=%d",
            limit,
            result.scanned_count,
            result.frozen_count,
        )
        return result


__all__ = [
    "FreezeExpiredWeaponryCallbackGuards",
    "RecoverWeaponryCallbackSynchronously",
]
