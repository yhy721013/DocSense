"""文件分析批量受理的框架无关 Application 用例。

受理成功只表示 SQLite execution 事实已经提交。Dispatcher 的 Event 是有界唤醒信号，
不是可靠队列：唤醒失败时必须保留已提交批次并记录异常，后续持久扫描才能恢复发现它。
本模块不读取当前任务、不创建线程，也不把 task/batch 身份映射到任何 HTTP 响应。
"""

from __future__ import annotations

import logging

from app.modules.analysis.ports import (
    AnalysisBatchAdmission,
    AnalysisBatchAdmissionOutcome,
    AnalysisBatchCommand,
    AnalysisBatchCommandPort,
    AnalysisDispatcherPort,
)

from .ordered_batch import AnalysisBatchOrderCoordinator


logger = logging.getLogger(__name__)


class SubmitAnalysisBatch:
    """编排“一次原子受理 → 一次有界唤醒”，不承担 Worker 调度。"""

    def __init__(
        self,
        *,
        batch_commands: AnalysisBatchCommandPort,
        dispatcher: AnalysisDispatcherPort,
    ) -> None:
        if not isinstance(batch_commands, AnalysisBatchCommandPort):
            raise TypeError("batch_commands 必须实现 AnalysisBatchCommandPort")
        if not isinstance(dispatcher, AnalysisDispatcherPort):
            raise TypeError("dispatcher 必须实现 AnalysisDispatcherPort")
        self._batch_commands = batch_commands
        self._dispatcher = dispatcher

    @property
    def dispatcher(self) -> AnalysisDispatcherPort:
        """暴露只读 Dispatcher 身份，供组合根验证受理与 Worker 使用同一实例。"""

        return self._dispatcher

    def execute(self, command: AnalysisBatchCommand) -> AnalysisBatchAdmission:
        """受理一批已校验快照；唤醒错误不得撤销已提交事实。"""

        if not isinstance(command, AnalysisBatchCommand):
            raise TypeError("command 必须是 AnalysisBatchCommand")
        logger.info(
            "开始原子受理文件分析批次: item_count=%d trace_id=%s",
            len(command.submissions),
            command.trace_id,
        )
        admission = self._batch_commands.create_batch_if_allowed(command)
        if not isinstance(admission, AnalysisBatchAdmission):
            raise TypeError("AnalysisBatchCommandPort 必须返回 AnalysisBatchAdmission")
        if admission.outcome is not AnalysisBatchAdmissionOutcome.ACCEPTED:
            logger.info(
                "文件分析批次未受理，不唤醒Dispatcher: item_count=%d outcome=%s "
                "trace_id=%s",
                len(command.submissions),
                admission.outcome.value,
                command.trace_id,
            )
            return admission

        # Port 返回的 execution 必须与请求文件顺序一一对应。这里没有创建内存队列；
        # 只在提交后校验持久事实的内部回显，后续 Dispatcher 仍按全局 dispatch_sequence
        # 扫描，因而即使进程重启也不会依赖此临时对象。
        ordered = AnalysisBatchOrderCoordinator.from_admission(command, admission)
        try:
            self._dispatcher.wake_up()
        except Exception:
            logger.exception(
                "文件分析批次已受理但Dispatcher唤醒失败，等待持久扫描恢复: "
                "batch_id=%s item_count=%d trace_id=%s",
                ordered.batch_id,
                len(ordered.task_ids),
                command.trace_id,
            )
        else:
            logger.info(
                "文件分析批次已受理并发送一次Dispatcher唤醒: batch_id=%s "
                "item_count=%d trace_id=%s",
                ordered.batch_id,
                len(ordered.task_ids),
                command.trace_id,
            )
        return admission


__all__ = ("SubmitAnalysisBatch",)
