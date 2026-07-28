"""文件分析 Application 的共享结果类型与单次执行临时状态。

本模块只承载不可变输入输出及每次调用独享的临时状态，不创建线程、缓存、客户端或
跨任务共享对象。这样协作器可以依赖同一份状态定义，而 ``run_analysis`` 外观仍保持原有
公开导入路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from app.modules.tasks.domain import TaskId

from app.modules.analysis.domain.architecture_tree import ArchitectureTreeIndex
from app.modules.analysis.domain.classification_rules import (
    _ArchitectureScopeResolution,
    _DataStandardClassificationProfile,
    _JaneClassificationProfile,
)
from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.analysis.ports import (
    AnalysisInteractionAttempt,
    AnalysisInteractionAuditReceipt,
    AnalysisRagLifecycleEvent,
    AnalysisRagOperation,
    AnalysisRagSessionRef,
    AnalysisRecallAuditReceipt,
)


class RunAnalysisOutcome(str, Enum):
    """Worker 内部收敛结果；不会成为公开接口中的新状态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"
    MISSING = "missing"
    NOT_CLAIMED = "not_claimed"


@dataclass(frozen=True)
class AnalysisTaskCompletion:
    """Task Adapter 在 expected TaskId 条件写中保存的最小终态事实。

    ``callback_payload`` 保持既有 file Callback 格式，但这里只作为持久投影，不在 1F-3
    发起 HTTP。实际 Guard、投递和同步恢复将在 1F-6 接入，避免新旧链路同时发送。
    """

    callback_payload: FrozenJsonObject
    succeeded: bool
    mapped_result: FrozenJsonObject | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.callback_payload, FrozenJsonObject):
            raise TypeError("callback_payload 必须是 FrozenJsonObject")
        if not isinstance(self.succeeded, bool):
            raise TypeError("succeeded 必须是 bool")
        if self.succeeded and not isinstance(self.mapped_result, FrozenJsonObject):
            raise TypeError("成功终态必须包含 mapped_result")
        if not self.succeeded and self.mapped_result is not None:
            raise ValueError("失败终态不得携带 mapped_result")


@dataclass(frozen=True)
class RunAnalysisResult:
    """一次调用的内部结果，不携带 Prompt、正文、模型回答或外部资源引用。"""

    task_id: TaskId
    outcome: RunAnalysisOutcome
    error_code: str = ""
    stage: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.outcome, RunAnalysisOutcome):
            raise TypeError("outcome 必须是 RunAnalysisOutcome")
        for field_name in ("error_code", "stage"):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"{field_name} 必须是 str")


class AnalysisApplicationContractError(RuntimeError):
    """Port 返回错误类型、跨任务身份或不可重放数据时抛出的内部合同异常。"""


class AnalysisTaskPersistenceError(RuntimeError):
    """任务事实条件写发生异常或确认丢失；禁止再写相反终态。"""


class _AnalysisKnownFailure(RuntimeError):
    """带稳定阶段/错误码的业务失败，避免把异常正文投影到公开状态。"""

    def __init__(self, stage: str, error_code: str) -> None:
        super().__init__(error_code)
        self.stage = stage
        self.error_code = error_code


@dataclass(frozen=True)
class _AnalysisWorkflowPlan:
    """纯规则计算后的 RAG 执行计划；不保存可变 Adapter 或文件系统对象。"""

    params: dict[str, Any]
    ranges: dict[str, list[dict[str, Any]]]
    tree_index: ArchitectureTreeIndex
    visible_candidates: tuple[dict[str, Any], ...]
    visible_ids: frozenset[int]
    initial_prompt: str
    direct_architecture_id: int | None
    recall_payload: FrozenJsonObject
    original_name: str
    original_text: str
    data_standard_profile: _DataStandardClassificationProfile
    data_standard_scope_guard_active: bool
    data_standard_scope_ids: tuple[int, ...]
    data_standard_remark_overrides: dict[int, str]
    jane_profile: _JaneClassificationProfile
    scope_resolution: _ArchitectureScopeResolution


@dataclass
class _RagWorkflowState:
    """单次 Application 调用内的临时审计聚合，不跨线程或任务共享。"""

    session: AnalysisRagSessionRef | None = None
    opened: bool = False
    lifecycle_events: list[AnalysisRagLifecycleEvent] = field(default_factory=list)
    attempts: list[AnalysisInteractionAttempt] = field(default_factory=list)
    attempt_counts: dict[AnalysisRagOperation, int] = field(default_factory=dict)
    last_prompt: str = ""
    recall_receipt: AnalysisRecallAuditReceipt | None = None
    recall_finalized: bool = False
    interaction_receipt: AnalysisInteractionAuditReceipt | None = None
    interaction_audit_attempted: bool = False
    preserve_scene: bool = False
    retain_document: bool = False
    # 由 RunAnalysisTask 在注入 Resource Port 时设置；每个 state 仅属于一次 execute，
    # 不会把另一个任务的资源事实、线程或可变回调带入当前任务。
    resource_checkpoint: Callable[["_RagWorkflowState"], None] | None = field(
        default=None,
        repr=False,
    )

    def checkpoint_resource_facts(self) -> None:
        """在取得新的 RAG 外部引用后立即落库，失败时 fail closed 保留现场。"""

        if self.resource_checkpoint is None:
            return
        try:
            self.resource_checkpoint(self)
        except Exception:
            # 资源引用已产生但无法可靠持久化时，后续不能继续模型、知识库或 close/delete。
            # 具体错误仍会由 Application 的失败收敛记录；这里不输出任何外部引用或正文。
            self.preserve_scene = True
            raise


__all__ = (
    "AnalysisApplicationContractError",
    "AnalysisTaskCompletion",
    "AnalysisTaskPersistenceError",
    "RunAnalysisOutcome",
    "RunAnalysisResult",
)
