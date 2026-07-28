"""甲方 check-task 触发的武器谱回调同步恢复用例。"""

from __future__ import annotations

import logging
import threading
from uuid import uuid4

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
        # 只合并当前进程正在处理的同一 architectureId，防止一批并发 check-task 在
        # owner 明确失败后继续读取新 attempt。跨实例互斥仍由持久化 Guard/CAS 负责。
        self._active_recovery_lock = threading.RLock()
        self._active_architecture_ids: set[int] = set()

    @property
    def callbacks(self) -> WeaponryCallbackPort:
        """供组合根验证同步入口与正常 Worker 共用 Guard 实例。"""

        return self._callbacks

    def execute(self, architecture_id: int, *, request_trace_id: str = "") -> bool:
        """同步尝试一次；只有本次 HTTP 收到严格 2xx 才返回 ``True``。"""

        normalized_id = normalize_architecture_id_value(architecture_id)
        if not isinstance(request_trace_id, str):
            raise TypeError("request_trace_id 必须是 str")
        normalized_trace_id = request_trace_id.strip()
        if len(normalized_trace_id) > 128:
            raise ValueError("request_trace_id 最多 128 个字符")
        effective_trace_id = normalized_trace_id or uuid4().hex
        if not self._try_enter_local_recovery(normalized_id):
            logger.info(
                "武器谱同步回调恢复已由本进程 owner 处理，跳过重复发送: "
                "architecture_id=%s",
                normalized_id,
            )
            return False
        try:
            return self._execute_owned(
                normalized_id,
                request_trace_id=effective_trace_id,
            )
        finally:
            self._leave_local_recovery(normalized_id)

    def _try_enter_local_recovery(self, architecture_id: int) -> bool:
        with self._active_recovery_lock:
            if architecture_id in self._active_architecture_ids:
                return False
            self._active_architecture_ids.add(architecture_id)
            return True

    def _leave_local_recovery(self, architecture_id: int) -> None:
        with self._active_recovery_lock:
            self._active_architecture_ids.discard(architecture_id)

    def _execute_owned(
        self,
        architecture_id: int,
        *,
        request_trace_id: str,
    ) -> bool:
        """由本进程 owner 使用首次候选快照尝试至多一次 Callback。"""

        normalized_id = architecture_id
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
                expected_callback_attempts=candidate.callback_attempts,
                request_trace_id=request_trace_id,
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
