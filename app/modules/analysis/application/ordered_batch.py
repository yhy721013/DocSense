"""文件分析已受理批次的顺序协调。

该模块不保存队列、锁或跨请求可变状态。请求内顺序由受理事务写入的 ``batch_sequence``
冻结，全局顺序由同一事务分配的 ``dispatch_sequence`` 决定；这里仅在 Application 边界
复核 Port 返回的执行顺序，防止未来 Adapter 错位后仍然唤醒 Worker。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.analysis.ports import (
    AnalysisBatchAdmission,
    AnalysisBatchAdmissionOutcome,
    AnalysisBatchCommand,
    AnalysisExecutionRef,
)
from app.modules.tasks.domain import TaskId


class AnalysisBatchOrderContractError(RuntimeError):
    """批量受理 Port 返回了错位、缺失或不可连续的内部执行身份。"""


@dataclass(frozen=True)
class OrderedAnalysisBatch:
    """已提交批次的不可变顺序视图，只在内部协调和日志中使用。"""

    batch_id: str
    executions: tuple[AnalysisExecutionRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.batch_id, str) or not self.batch_id:
            raise ValueError("batch_id 必须是非空 str")
        executions = tuple(self.executions)
        if not executions:
            raise ValueError("executions 不能为空")
        if any(not isinstance(item, AnalysisExecutionRef) for item in executions):
            raise TypeError("executions 只能包含 AnalysisExecutionRef")
        if any(item.batch_id != self.batch_id for item in executions):
            raise ValueError("executions 必须属于同一 batch_id")
        if tuple(item.batch_sequence for item in executions) != tuple(
            range(1, len(executions) + 1)
        ):
            raise ValueError("executions.batch_sequence 必须从1连续递增")
        object.__setattr__(self, "executions", executions)

    @property
    def task_ids(self) -> tuple[TaskId, ...]:
        """按请求原顺序返回内部任务身份；禁止将其传给公开 Presenter。"""

        return tuple(item.task_id for item in self.executions)


class AnalysisBatchOrderCoordinator:
    """验证受理结果与不可变请求顺序完全一致。"""

    @staticmethod
    def from_admission(
        command: AnalysisBatchCommand,
        admission: AnalysisBatchAdmission,
    ) -> OrderedAnalysisBatch:
        """只接受成功批次，并逐项比对 fileName、序号和共同 batch_id。"""

        if not isinstance(command, AnalysisBatchCommand):
            raise TypeError("command 必须是 AnalysisBatchCommand")
        if not isinstance(admission, AnalysisBatchAdmission):
            raise TypeError("admission 必须是 AnalysisBatchAdmission")
        if admission.outcome is not AnalysisBatchAdmissionOutcome.ACCEPTED:
            raise AnalysisBatchOrderContractError("未受理批次不存在可协调执行顺序")
        executions = admission.executions
        if len(executions) != len(command.submissions):
            raise AnalysisBatchOrderContractError("受理execution数量与请求项数量不一致")
        expected_file_names = tuple(item.file_name for item in command.submissions)
        actual_file_names = tuple(item.file_name for item in executions)
        if actual_file_names != expected_file_names:
            raise AnalysisBatchOrderContractError("受理execution文件顺序与请求顺序不一致")
        batch_ids = {item.batch_id for item in executions}
        if len(batch_ids) != 1:
            raise AnalysisBatchOrderContractError("受理execution缺少唯一batch_id")
        try:
            return OrderedAnalysisBatch(next(iter(batch_ids)), executions)
        except (TypeError, ValueError) as exc:
            raise AnalysisBatchOrderContractError("受理execution顺序数据无效") from exc


__all__ = (
    "AnalysisBatchOrderContractError",
    "AnalysisBatchOrderCoordinator",
    "OrderedAnalysisBatch",
)
