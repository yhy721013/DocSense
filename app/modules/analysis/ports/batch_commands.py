"""文件分析批量受理、加载和条件领取的抽象端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.analysis.domain.task_inputs import (
    AnalysisSubmissionSnapshot,
    AnalysisTaskInputV1,
    FrozenJsonArray,
    FrozenJsonObject,
)
from app.modules.analysis.domain.models import MAX_ANALYSIS_PARAMS_PER_REQUEST
from app.modules.tasks.domain import TaskId

from .common import AnalysisExecutionRef


class AnalysisBatchAdmissionOutcome(str, Enum):
    """批量受理对 Application 可见的有限结果，不混入 HTTP 状态码。"""

    ACCEPTED = "accepted"
    CONFLICT_ACTIVE = "conflict_active"
    CONFLICT_CALLBACK_PENDING = "conflict_callback_pending"
    BUSY = "busy"


class AnalysisTaskClaimOutcome(str, Enum):
    """Worker 领取持久 execution 的条件结果。"""

    CLAIMED = "claimed"
    MISSING = "missing"
    NOT_ACCEPTED = "not_accepted"
    STALE = "stale"


@dataclass(frozen=True)
class AnalysisBatchCommand:
    """Application 提交给事务 Adapter 的整批不可变受理命令。"""

    request_projection: FrozenJsonObject
    submissions: tuple[AnalysisSubmissionSnapshot, ...]
    trace_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.request_projection, FrozenJsonObject):
            raise TypeError("request_projection 必须是 FrozenJsonObject")
        if not isinstance(self.submissions, (tuple, list)) or not self.submissions:
            raise ValueError("submissions 必须是非空有序序列")
        submissions = tuple(self.submissions)
        if any(not isinstance(item, AnalysisSubmissionSnapshot) for item in submissions):
            raise TypeError("submissions 只能包含 AnalysisSubmissionSnapshot")
        if len(submissions) > MAX_ANALYSIS_PARAMS_PER_REQUEST:
            raise ValueError(
                f"submissions 数量不能超过 {MAX_ANALYSIS_PARAMS_PER_REQUEST}"
            )
        if not isinstance(self.trace_id, str) or not self.trace_id.strip():
            raise ValueError("trace_id 必须是非空 str")
        if self.request_projection.get("businessType") != "file":
            raise ValueError("request_projection.businessType 必须是 file")
        params = self.request_projection.get("params")
        if not isinstance(params, FrozenJsonArray) or params.values != tuple(
            item.raw_params for item in submissions
        ):
            raise ValueError("request_projection.params 与 submissions 不一致")
        object.__setattr__(self, "submissions", submissions)
        object.__setattr__(self, "trace_id", self.trace_id.strip())


@dataclass(frozen=True)
class AnalysisBatchAdmission:
    """原子受理结果；成功时仅向 Application 返回内部 execution 身份。"""

    outcome: AnalysisBatchAdmissionOutcome
    executions: tuple[AnalysisExecutionRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, AnalysisBatchAdmissionOutcome):
            raise TypeError("outcome 必须是 AnalysisBatchAdmissionOutcome")
        executions = tuple(self.executions)
        if any(not isinstance(item, AnalysisExecutionRef) for item in executions):
            raise TypeError("executions 只能包含 AnalysisExecutionRef")
        if self.outcome is AnalysisBatchAdmissionOutcome.ACCEPTED and not executions:
            raise ValueError("accepted 结果必须携带 execution")
        if self.outcome is not AnalysisBatchAdmissionOutcome.ACCEPTED and executions:
            raise ValueError("未受理结果不得携带 execution")
        if executions:
            task_ids = tuple(item.task_id for item in executions)
            file_names = tuple(item.file_name for item in executions)
            batch_ids = {item.batch_id for item in executions}
            sequences = tuple(item.batch_sequence for item in executions)
            if len(set(task_ids)) != len(task_ids):
                raise ValueError("accepted executions 的 task_id 不得重复")
            if len(set(file_names)) != len(file_names):
                raise ValueError("accepted executions 的 file_name 不得重复")
            if len(batch_ids) != 1:
                raise ValueError("accepted executions 必须属于同一 batch_id")
            if sequences != tuple(range(1, len(executions) + 1)):
                raise ValueError("accepted executions 的 batch_sequence 必须从 1 连续递增")
        object.__setattr__(self, "executions", executions)


@dataclass(frozen=True)
class AnalysisTaskClaim:
    """条件领取结果；非 claimed 状态不允许伪造任务快照。"""

    outcome: AnalysisTaskClaimOutcome
    execution: AnalysisExecutionRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, AnalysisTaskClaimOutcome):
            raise TypeError("outcome 必须是 AnalysisTaskClaimOutcome")
        if self.execution is not None and not isinstance(
            self.execution,
            AnalysisExecutionRef,
        ):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if self.outcome is AnalysisTaskClaimOutcome.CLAIMED and self.execution is None:
            raise ValueError("claimed 结果必须携带 execution")
        if self.outcome is not AnalysisTaskClaimOutcome.CLAIMED and self.execution is not None:
            raise ValueError("未领取结果不得携带 execution")


@runtime_checkable
class AnalysisBatchAdmissionPort(Protocol):
    """只负责批量受理的窄事务边界；执行期不得复用它补造 Authority。"""

    def create_batch_if_allowed(
        self,
        command: AnalysisBatchCommand,
    ) -> AnalysisBatchAdmission:
        ...


@runtime_checkable
class AnalysisBatchCommandPort(AnalysisBatchAdmissionPort, Protocol):
    """旧迁移夹具使用的受理、加载和领取组合端口。"""

    def load_input(self, task_id: TaskId) -> AnalysisTaskInputV1 | None:
        ...

    def claim_if_accepted(self, task_id: TaskId) -> AnalysisTaskClaim:
        ...


@runtime_checkable
class AnalysisPoisonTaskCommandPort(Protocol):
    """无需解码坏输入即可把新 Analysis accepted 记录收敛为失败的控制面。"""

    def fail_poisoned_accepted(
        self,
        task_id: TaskId,
        *,
        reason: str,
    ) -> AnalysisExecutionRef | None:
        """收敛成功时返回已提交终态的 execution，供调用方进入同一回调恢复链。"""

        ...


__all__ = (
    "AnalysisBatchAdmission",
    "AnalysisBatchAdmissionPort",
    "AnalysisBatchAdmissionOutcome",
    "AnalysisBatchCommand",
    "AnalysisBatchCommandPort",
    "AnalysisPoisonTaskCommandPort",
    "AnalysisTaskClaim",
    "AnalysisTaskClaimOutcome",
)
