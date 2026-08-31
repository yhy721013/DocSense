"""甲方 check-task 触发的报告回调同步恢复用例。"""

from __future__ import annotations

import logging
import threading
from uuid import uuid4

from app.modules.report.domain import ReportPortContractError, ReportId
from app.modules.tasks.domain import TaskId
from app.modules.report.ports import (
    DeliverReportCallback,
    ReportCallbackAcquire,
    ReportCallbackAcquireOutcome,
    ReportCallbackAcquireReason,
    ReportCallbackAcquireResult,
    ReportCallbackDeliveryOutcome,
    ReportCallbackDeliveryResult,
    ReportCallbackPort,
    ReportCallbackRecoverySourcePort,
)


logger = logging.getLogger(__name__)


class RecoverReportCallbackSynchronously:
    """保留 check-task 同步副作用，同时复用报告主链路的 Callback Guard。

    该用例只编排一次显式恢复，不创建新报告 execution，也不执行后台盲重试。候选读取
    之后仍可能发生新任务受理，因此真正的 latest-wins 判断必须由 ``acquire`` 在短事务
    中再次完成；只有拿到租约的调用方才允许发出 HTTP。
    """

    def __init__(
        self,
        *,
        source: ReportCallbackRecoverySourcePort,
        callbacks: ReportCallbackPort,
    ) -> None:
        if not isinstance(source, ReportCallbackRecoverySourcePort):
            raise TypeError("source 必须实现 ReportCallbackRecoverySourcePort")
        if not isinstance(callbacks, ReportCallbackPort):
            raise TypeError("callbacks 必须实现 ReportCallbackPort")
        self._source = source
        self._callbacks = callbacks
        # 同一进程内并发 check-task 在首个 owner 已完成明确失败后，后启动线程若重新
        # 读取 callback_attempts 会被误认为下一轮独立授权。活跃键合并仅压缩当前调用，
        # 跨进程唯一性仍由 SQLite attempt CAS 和 fencing token 保证。
        self._active_recovery_lock = threading.RLock()
        self._active_report_keys: set[str] = set()

    @property
    def callbacks(self) -> ReportCallbackPort:
        """暴露只读依赖身份，供组合根验证同步入口与正常 Worker 共用 Guard。"""

        return self._callbacks

    def execute(
        self,
        report_id: ReportId,
        *,
        request_trace_id: str = "",
        expected_task_id: TaskId | None = None,
    ) -> bool:
        """同步尝试一次恢复；仅真正收到 2xx 时返回 ``True``。

        ``False`` 包含无需恢复、旧执行过期、其他执行者正在发送、结果未知冻结、未配置
        回调地址以及接收方拒绝等情况。具体状态已由 Guard/投影持久化，公开路由继续沿用
        既有响应结构，不把内部分类扩张成新的接口字段。
        """

        if not isinstance(report_id, ReportId):
            raise TypeError("report_id 必须是 ReportId")
        if not isinstance(request_trace_id, str):
            raise TypeError("request_trace_id 必须是 str")
        if expected_task_id is not None and not isinstance(expected_task_id, TaskId):
            raise TypeError("expected_task_id 必须是 TaskId 或 None")
        normalized_trace_id = request_trace_id.strip()
        if len(normalized_trace_id) > 128:
            raise ValueError("request_trace_id 最多 128 个字符")
        effective_trace_id = normalized_trace_id or uuid4().hex
        business_key = report_id.business_key
        if not self._try_enter_local_recovery(business_key):
            logger.info(
                "报告同步回调恢复已由本进程 owner 处理，跳过重复发送: report_id=%s",
                report_id.public_value,
            )
            return False
        try:
            return self._execute_owned(
                report_id,
                request_trace_id=effective_trace_id,
                expected_task_id=expected_task_id,
            )
        finally:
            self._leave_local_recovery(business_key)

    def _try_enter_local_recovery(self, business_key: str) -> bool:
        """登记本进程活跃 report 键；集合不保存历史或结果。"""

        with self._active_recovery_lock:
            if business_key in self._active_report_keys:
                return False
            self._active_report_keys.add(business_key)
            return True

    def _leave_local_recovery(self, business_key: str) -> None:
        with self._active_recovery_lock:
            self._active_report_keys.discard(business_key)

    def _execute_owned(
        self,
        report_id: ReportId,
        *,
        request_trace_id: str,
        expected_task_id: TaskId | None,
    ) -> bool:
        """由当前进程内 owner 使用首次候选快照完成至多一次发送。"""

        candidate = self._source.load_recoverable(report_id)
        if candidate is None:
            logger.debug(
                "报告同步回调无需恢复: report_id=%s",
                report_id.public_value,
            )
            return False
        if expected_task_id is not None and candidate.task_id != expected_task_id:
            logger.info(
                "报告同步回调跳过已切换的 latest execution: "
                "business_type=report reason=expected_task_changed"
            )
            return False

        acquire = self._callbacks.acquire(
            ReportCallbackAcquire(
                candidate.task_id,
                candidate.report_id,
                ReportCallbackAcquireReason.EXPLICIT_CHECK_TASK_RECOVERY,
                candidate.callback_attempts,
                request_trace_id,
            )
        )
        if not isinstance(acquire, ReportCallbackAcquireResult):
            raise ReportPortContractError("Callback acquire 返回类型错误")
        if acquire.outcome is not ReportCallbackAcquireOutcome.ACQUIRED:
            logger.info(
                "报告同步回调未取得发送权: task_id=%s report_id=%s outcome=%s",
                candidate.task_id,
                report_id.public_value,
                acquire.outcome.value,
            )
            return False
        lease = acquire.lease
        if lease is None or lease.task_id != candidate.task_id:
            raise ReportPortContractError("Callback Guard Lease 与恢复任务不一致")

        delivery = self._callbacks.deliver(
            DeliverReportCallback(lease, candidate.payload)
        )
        if not isinstance(delivery, ReportCallbackDeliveryResult):
            raise ReportPortContractError("Callback deliver 返回类型错误")
        if delivery.outcome is ReportCallbackDeliveryOutcome.STALE:
            # 网络调用前的二次复核已经失败，不能再用旧租约完成或覆盖新 owner。
            logger.info(
                "报告同步回调在发送前变为过期: task_id=%s report_id=%s",
                candidate.task_id,
                report_id.public_value,
            )
            return False

        completed = self._callbacks.complete(lease, delivery, candidate.payload)
        if not isinstance(completed, bool):
            raise ReportPortContractError("Callback complete 必须返回 bool")
        if not completed:
            # HTTP 可能已经到达甲方，完成权丢失时必须显式暴露内部故障，不能伪装为普通
            # 失败后再次发送。Guard 扫描会把过期发送权冻结为 outcome_unknown。
            raise ReportPortContractError("Callback Guard 完成权已过期")

        replayed = delivery.outcome is ReportCallbackDeliveryOutcome.SUCCESS
        logger.log(
            logging.INFO if replayed else logging.WARNING,
            "报告同步回调恢复完成: task_id=%s report_id=%s outcome=%s",
            candidate.task_id,
            report_id.public_value,
            delivery.outcome.value,
        )
        return replayed


__all__ = ["RecoverReportCallbackSynchronously"]
