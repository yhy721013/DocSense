"""武器谱公开请求的文档冻结、策略注入与可靠受理编排。"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from app.modules.weaponry.domain import (
    AuxiliaryGuidancePolicySnapshot,
    EvidenceSelectionPolicy,
    FrozenJsonObject,
    WeaponryExecutionPolicySnapshot,
    WeaponryFieldSpecification,
    WeaponrySubmission,
)
from app.modules.weaponry.ports import WeaponryDocumentScopePort

from .submit_weaponry import SubmitWeaponryResult, SubmitWeaponryTask


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmitWeaponryRequestCommand:
    """Web Adapter 完整校验后的不可变武器谱受理命令。"""

    request_projection: FrozenJsonObject
    architecture_id: int
    selected_file_names: tuple[str, ...]
    fields: tuple[WeaponryFieldSpecification, ...]
    trace_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.request_projection, FrozenJsonObject):
            raise TypeError("request_projection 必须是 FrozenJsonObject")
        if isinstance(self.architecture_id, bool) or not isinstance(
            self.architecture_id, int
        ):
            raise TypeError("architecture_id 必须是 int")
        if self.architecture_id <= 0:
            raise ValueError("architecture_id 必须为正整数")
        file_names = tuple(self.selected_file_names)
        if any(not isinstance(item, str) or not item for item in file_names):
            raise ValueError("selected_file_names 只能包含非空 str")
        fields = tuple(self.fields)
        if not fields or any(
            not isinstance(item, WeaponryFieldSpecification) for item in fields
        ):
            raise ValueError("fields 必须包含 WeaponryFieldSpecification")
        if not isinstance(self.trace_id, str) or not self.trace_id.strip():
            raise ValueError("trace_id 必须是非空 str")
        object.__setattr__(self, "selected_file_names", file_names)
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "trace_id", self.trace_id.strip())


@dataclass(frozen=True)
class SubmitWeaponryRequestResult:
    """公开请求编排结果；仅供日志和 Presenter 选择既有成功分支。"""

    admission: SubmitWeaponryResult
    architecture_id: int
    document_count: int
    field_count: int


class SubmitWeaponryRequest:
    """编排“冻结文档范围 → 注入策略 → 提交任务事实”。

    文档解析和受理都可能访问基础设施，但本用例只依赖显式 Port/Application，因而
    Flask 路由不再直接协调数据库适配器、策略快照和任务提交顺序。
    """

    def __init__(
        self,
        *,
        document_scope: WeaponryDocumentScopePort,
        evidence_selection_policy: EvidenceSelectionPolicy,
        execution_policy: WeaponryExecutionPolicySnapshot,
        auxiliary_guidance_policy: AuxiliaryGuidancePolicySnapshot,
        submit: SubmitWeaponryTask,
    ) -> None:
        self._document_scope = document_scope
        self._evidence_selection_policy = evidence_selection_policy
        self._execution_policy = execution_policy
        self._auxiliary_guidance_policy = auxiliary_guidance_policy
        self._submit = submit

    @property
    def document_scope(self) -> WeaponryDocumentScopePort:
        """暴露只读依赖身份，供组合根证明使用同一 Document Scope Port。"""

        return self._document_scope

    @property
    def submit(self) -> SubmitWeaponryTask:
        """暴露只读依赖身份，供组合根证明使用同一可靠受理用例。"""

        return self._submit

    def execute(
        self,
        command: SubmitWeaponryRequestCommand,
    ) -> SubmitWeaponryRequestResult:
        if not isinstance(command, SubmitWeaponryRequestCommand):
            raise TypeError("command 必须是 SubmitWeaponryRequestCommand")

        document_scope = self._document_scope.resolve(
            architecture_id=command.architecture_id,
            requested_file_names=command.selected_file_names,
        )
        submission = WeaponrySubmission(
            architecture_id=command.architecture_id,
            request_projection=command.request_projection,
            fields=command.fields,
            document_scope=document_scope,
            evidence_selection_policy=self._evidence_selection_policy,
            execution_policy=self._execution_policy,
            auxiliary_guidance_policy=self._auxiliary_guidance_policy,
            trace_id=command.trace_id,
        )
        admission = self._submit.execute(submission)
        result = SubmitWeaponryRequestResult(
            admission=admission,
            architecture_id=command.architecture_id,
            document_count=len(document_scope.documents),
            field_count=len(command.fields),
        )
        logger.info(
            "武器谱公开请求编排完成: document_count=%d field_count=%d "
            "has_request_trace=%s",
            result.document_count,
            result.field_count,
            bool(command.trace_id),
        )
        return result


__all__ = [
    "SubmitWeaponryRequest",
    "SubmitWeaponryRequestCommand",
    "SubmitWeaponryRequestResult",
]
