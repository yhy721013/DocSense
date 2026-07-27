"""file ``check-task`` 使用的同步回调恢复用例。"""

from __future__ import annotations

import logging
import threading

from app.modules.analysis.ports import (
    AnalysisCallbackAcquireOutcome,
    AnalysisCallbackAcquireResult,
    AnalysisCallbackDelivery,
    AnalysisCallbackDeliveryOutcome,
    AnalysisCallbackDeliveryRequest,
    AnalysisCallbackPort,
    AnalysisCallbackRecoveryCandidate,
    AnalysisCallbackRecoverySourcePort,
    AnalysisCallbackRequest,
    AnalysisCallbackWaitOutcome,
    WaitForAnalysisCallbackRelease,
)


logger = logging.getLogger(__name__)


class RecoverAnalysisCallbackSynchronously:
    """只补发已终态的 latest file 回调，复用正常执行的同一 Guard。

    本用例不创建新任务、不重跑模型/RAG。对于 ``outcome_unknown``，只有新的
    ``check-task`` 请求可以显式授权一次 at-least-once 补发；后台 Worker 与维护线程
    仍不得自动重试。候选读取只是优化，真正授权仍由 ``acquire`` 在 SQLite 短事务中
    完成。
    """

    def __init__(
        self,
        *,
        source: AnalysisCallbackRecoverySourcePort,
        callbacks: AnalysisCallbackPort,
        callback_url: str,
        wait_timeout_seconds: float = 1.0,
        wait_poll_seconds: float = 0.05,
    ) -> None:
        if not isinstance(source, AnalysisCallbackRecoverySourcePort):
            raise TypeError("source 必须实现 AnalysisCallbackRecoverySourcePort")
        if not isinstance(callbacks, AnalysisCallbackPort):
            raise TypeError("callbacks 必须实现 AnalysisCallbackPort")
        if not isinstance(callback_url, str):
            raise TypeError("callback_url 必须是 str")
        for name, value in (
            ("wait_timeout_seconds", wait_timeout_seconds),
            ("wait_poll_seconds", wait_poll_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} 必须是数字")
            normalized = float(value)
            if (
                normalized != normalized
                or normalized in (float("inf"), float("-inf"))
                or normalized <= 0.0
            ):
                raise ValueError(f"{name} 必须是正有限数字")
        self._source = source
        self._callbacks = callbacks
        self._callback_url = callback_url.strip()
        self._wait_timeout_seconds = float(wait_timeout_seconds)
        self._wait_poll_seconds = float(wait_poll_seconds)
        # 同一容器中的并发 check-task 可能在首个 owner 已经明确失败并释放 SQLite
        # Guard 后，才刚刚开始读取 recovery candidate。此时它们若直接读取新的
        # callback_attempts，就会被误判成下一轮人工重试并再次发送。
        #
        # 这里仅合并“当前仍在本进程执行”的同 fileName 恢复调用：集合只保存活跃键，
        # 无任务历史、无结果缓存，finally 中必定删除，因此不会随历史任务数量增长。
        # 跨进程唯一发送权仍完全由 SQLite Callback Guard 裁决；本地集合不是分布式锁，
        # 也不能替代将来的可靠队列或多实例协调机制。
        self._active_recovery_lock = threading.RLock()
        self._active_recovery_file_names: set[str] = set()

    @property
    def callbacks(self) -> AnalysisCallbackPort:
        """供未来组合根断言 Worker 与 check-task 共用同一 Guard Adapter。"""

        return self._callbacks

    def execute(self, file_name: str) -> bool:
        """同步尝试一次补发；仅严格 2xx 的 ``delivered`` 返回 ``True``。"""

        if not isinstance(file_name, str) or not file_name.strip():
            raise ValueError("file_name 必须是非空 str")
        normalized_file_name = file_name.strip()
        if not self._try_enter_local_recovery(normalized_file_name):
            # 跟随者不等待、也不把 owner 的结果伪造为自己的成功。公开 check-task 未来
            # 只需知道“本次没有完成一次严格成功投递”；真正的状态仍可由 Guard/投影读取。
            # 立即返回还能避免 50 个 HTTP 请求线程在进程内堆积等待，降低 SQLite 锁竞争。
            logger.info(
                "文件分析同步回调恢复已由本进程 owner 处理，跳过重复发送: file_name=%s",
                normalized_file_name,
            )
            return False
        try:
            candidate = self._source.load_recoverable(normalized_file_name)
            if candidate is None:
                return False
            return self._attempt_candidate(candidate, allow_wait=True)
        finally:
            self._leave_local_recovery(normalized_file_name)

    def _try_enter_local_recovery(self, file_name: str) -> bool:
        """登记当前进程内的一次恢复 owner，避免活跃调用重复形成下一轮 attempt。"""

        with self._active_recovery_lock:
            if file_name in self._active_recovery_file_names:
                return False
            self._active_recovery_file_names.add(file_name)
            return True

    def _leave_local_recovery(self, file_name: str) -> None:
        """释放活跃键；任何异常路径都必须调用，避免后续独立恢复被永久抑制。"""

        with self._active_recovery_lock:
            self._active_recovery_file_names.discard(file_name)

    def _attempt_candidate(
        self,
        candidate: AnalysisCallbackRecoveryCandidate,
        *,
        allow_wait: bool,
    ) -> bool:
        """对一个已验证候选获取发送权；最多等待并重新读取一次。"""

        # 这里不导入 Adapter 或 SQLite DTO；候选是 Port 层的不可变值对象，避免恢复用例
        # 反向依赖基础设施，也避免把动态字典中的业务键误当作可发送事实。
        execution = candidate.execution
        payload = candidate.payload
        acquire = self._callbacks.acquire(
            AnalysisCallbackRequest(
                execution=execution,
                callback_url=self._callback_url,
                payload=payload,
                allow_failed_retry=True,
                allow_outcome_unknown_retry=True,
                expected_callback_attempts=candidate.callback_attempts,
            )
        )
        if not isinstance(acquire, AnalysisCallbackAcquireResult):
            raise RuntimeError("Analysis callback acquire 返回类型错误")
        if acquire.outcome is AnalysisCallbackAcquireOutcome.WAIT_FOR_OWNER and allow_wait:
            waited = self._callbacks.wait_until_released(
                WaitForAnalysisCallbackRelease(
                    execution=execution,
                    timeout_seconds=self._wait_timeout_seconds,
                    poll_seconds=self._wait_poll_seconds,
                )
            )
            if waited.outcome is not AnalysisCallbackWaitOutcome.RELEASED:
                logger.info(
                    "文件分析同步回调等待未释放，保持现有Guard事实: task_id=%s outcome=%s",
                    execution.task_id,
                    waited.outcome.value,
                )
                return False
            # 释放后仍使用首次读取的 attempt 快照做一次原子复核。若前一个 owner 已经
            # 完成（包括明确失败），底层递增后的 callback_attempts 会使本次 acquire
            # 返回 stale，从而把同一批并发 check-task 收敛为至多一次 HTTP。下一次独立
            # check-task 会重新读取新快照，仍可显式发起一轮恢复。
            return self._attempt_candidate(candidate, allow_wait=False)
        if acquire.outcome is not AnalysisCallbackAcquireOutcome.ACQUIRED:
            logger.info(
                "文件分析同步回调未取得发送权: task_id=%s file_name=%s outcome=%s",
                execution.task_id,
                execution.file_name,
                acquire.outcome.value,
            )
            return False
        lease = acquire.lease
        if lease is None or lease.execution != execution:
            raise RuntimeError("Analysis callback Guard Lease 与恢复候选不一致")
        delivery = self._callbacks.deliver(
            AnalysisCallbackDeliveryRequest(
                lease=lease,
                callback_url=self._callback_url,
                payload=payload,
            )
        )
        if not isinstance(delivery, AnalysisCallbackDelivery):
            raise RuntimeError("Analysis callback deliver 返回类型错误")
        if delivery.outcome is AnalysisCallbackDeliveryOutcome.STALE:
            return False
        completed = self._callbacks.complete(lease, delivery, payload)
        if not isinstance(completed, bool):
            raise RuntimeError("Analysis callback complete 必须返回 bool")
        if not completed:
            # HTTP 可能已经抵达接收方；不能在当前请求内再次发送。过期 Guard 后续只会
            # 冻结为 unknown；后台不得自动发送，后续新的 file check-task 可在调用方
            # 接受至少一次语义后显式授权补发。
            logger.error(
                "文件分析同步回调完成权丢失，禁止重复投递: task_id=%s",
                execution.task_id,
            )
            return False
        delivered = delivery.outcome is AnalysisCallbackDeliveryOutcome.DELIVERED
        logger.log(
            logging.INFO if delivered else logging.WARNING,
            "文件分析同步回调恢复完成: task_id=%s file_name=%s outcome=%s",
            execution.task_id,
            execution.file_name,
            delivery.outcome.value,
        )
        return delivered


class FreezeExpiredAnalysisCallbackGuards:
    """供未来 Dispatcher 维护线程调用的有界 Guard 冻结协作器。"""

    def __init__(self, callbacks: AnalysisCallbackPort) -> None:
        if not isinstance(callbacks, AnalysisCallbackPort):
            raise TypeError("callbacks 必须实现 AnalysisCallbackPort")
        self._callbacks = callbacks

    @property
    def callbacks(self) -> AnalysisCallbackPort:
        return self._callbacks

    def run_once(self, *, limit: int) -> object:
        return self._callbacks.freeze_expired(limit=limit)


__all__ = (
    "FreezeExpiredAnalysisCallbackGuards",
    "RecoverAnalysisCallbackSynchronously",
)
