"""甲方 check-task 触发的报告回调同步恢复用例。"""

from __future__ import annotations

import logging

from app.modules.report.domain import ReportPortContractError, ReportId
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

    @property
    def callbacks(self) -> ReportCallbackPort:
        """暴露只读依赖身份，供组合根验证同步入口与正常 Worker 共用 Guard。"""

        return self._callbacks

    def execute(self, report_id: ReportId) -> bool:
        """同步尝试一次恢复；仅真正收到 2xx 时返回 ``True``。

        ``False`` 包含无需恢复、旧执行过期、其他执行者正在发送、结果未知冻结、未配置
        回调地址以及接收方拒绝等情况。具体状态已由 Guard/投影持久化，公开路由继续沿用
        既有响应结构，不把内部分类扩张成新的接口字段。
        """

        if not isinstance(report_id, ReportId):
            raise TypeError("report_id 必须是 ReportId")
        candidate = self._source.load_recoverable(report_id)
        if candidate is None:
            logger.debug(
                "报告同步回调无需恢复: report_id=%s",
                report_id.public_value,
            )
            return False

        acquire = self._callbacks.acquire(
            ReportCallbackAcquire(
                candidate.task_id,
                candidate.report_id,
                ReportCallbackAcquireReason.EXPLICIT_CHECK_TASK_RECOVERY,
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
