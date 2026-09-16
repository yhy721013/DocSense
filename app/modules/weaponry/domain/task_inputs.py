"""武器谱可靠受理所需的不可变文档范围、提交命令与 execution 输入。

公开 HTTP 请求投影和后台 Worker 输入具有不同职责：前者只供兼容查询与问题排查，后者
必须能够在进程重启、长时间排队和配置变化后独立重放。本模块把两者显式分开，避免 Worker
重新读取当前类别文档、旧选文表或运行时环境变量。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import WeaponryDomainValidationError
from .models import (
    MAX_ARCHITECTURE_ID,
    WEAPONRY_BUSINESS_TYPE,
    FrozenJsonObject,
    WEAPONRY_STATUS_SUCCEEDED,
    WeaponryDocumentSnapshot,
    WeaponryFieldSpecification,
    WeaponryResult,
)
from .prompts import EXTRACTION_PROMPT_VERSION
from .retrieval_quality import EvidenceSelectionPolicy
from .strategy import FILE_AGGREGATE_STRATEGY
from .architecture_ids import normalize_architecture_id_value


WEAPONRY_INPUT_SCHEMA_VERSION = 2
DOCUMENT_SCOPE_EXPLICIT = "explicit"
DOCUMENT_SCOPE_CATEGORY = "category"
AUXILIARY_GUIDANCE_NONE = "none"
AUXILIARY_GUIDANCE_TERMS_RULES_V1 = "terms-rules-v1"
AUXILIARY_GUIDANCE_TERMS_RULES_COLUMN_COMPACT_V2 = (
    "terms-rules-column-compact-v2"
)
EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1 = "provided_evidence_model_v1"
EXTRACTION_CONTEXT_EVIDENCE_ONLY_V1 = "evidence_only_context_v1"
TABLE_MERGE_POLICY_VERSION = "strong-identity-composite-v2"
_AUXILIARY_GUIDANCE_POLICIES = frozenset(
    {
        AUXILIARY_GUIDANCE_NONE,
        AUXILIARY_GUIDANCE_TERMS_RULES_V1,
        AUXILIARY_GUIDANCE_TERMS_RULES_COLUMN_COMPACT_V2,
    }
)
_EXTRACTION_CONTEXT_STRATEGIES = frozenset(
    {
        EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
        EXTRACTION_CONTEXT_EVIDENCE_ONLY_V1,
    }
)


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WeaponryDomainValidationError(f"{name} 必须是非空 str")
    return value.strip()


def _architecture_id(value: object, *, name: str) -> int:
    """校验已经规范化的内部 architecture ID，不做宽松字符串转换。"""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_ARCHITECTURE_ID
    ):
        raise WeaponryDomainValidationError(
            f"{name} 必须是 1 到 {MAX_ARCHITECTURE_ID} 的整数"
        )
    return value


def _public_architecture_id(value: object) -> int:
    """只用于核对公开投影和内部身份，语义与已批准 D02 保持一致。"""

    return normalize_architecture_id_value(value)


@dataclass(frozen=True)
class WeaponryDocumentScope:
    """受理时冻结的 explicit/category 文档范围。

    ``document_key`` 只需在当前 execution 内唯一；``sequence_no`` 则冻结调用方请求顺序或
    类别查询的稳定顺序。后续 Worker 只能消费这里的快照，禁止再按文件名或类别重新选择。
    """

    mode: str
    requested_file_names: tuple[str, ...]
    documents: tuple[WeaponryDocumentSnapshot, ...]

    def __post_init__(self) -> None:
        if self.mode not in {DOCUMENT_SCOPE_EXPLICIT, DOCUMENT_SCOPE_CATEGORY}:
            raise WeaponryDomainValidationError(
                "document scope mode 只能是 explicit 或 category"
            )
        if not isinstance(self.requested_file_names, (tuple, list)):
            raise WeaponryDomainValidationError(
                "requested_file_names 必须是有序文本序列"
            )
        requested_file_names = tuple(
            _required_text(item, name="requested_file_name")
            for item in self.requested_file_names
        )
        requested_keys = tuple(item.casefold() for item in requested_file_names)
        if len(set(requested_keys)) != len(requested_keys):
            raise WeaponryDomainValidationError(
                "requested_file_names 不能包含大小写不敏感重复项"
            )

        if not isinstance(self.documents, (tuple, list)) or any(
            not isinstance(item, WeaponryDocumentSnapshot)
            for item in self.documents
        ):
            raise WeaponryDomainValidationError(
                "documents 只能包含 WeaponryDocumentSnapshot"
            )
        documents = tuple(self.documents)
        expected_sequence = tuple(range(1, len(documents) + 1))
        if tuple(item.sequence_no for item in documents) != expected_sequence:
            raise WeaponryDomainValidationError(
                "documents.sequence_no 必须从 1 开始连续递增"
            )
        document_keys = tuple(item.document_key for item in documents)
        if len(set(document_keys)) != len(document_keys):
            raise WeaponryDomainValidationError(
                "documents 不能包含重复 document_key"
            )
        external_refs = tuple(item.external_document_ref for item in documents)
        if len(set(external_refs)) != len(external_refs):
            raise WeaponryDomainValidationError(
                "documents 不能包含重复 external_document_ref"
            )
        document_file_keys = tuple(item.file_name.casefold() for item in documents)
        if len(set(document_file_keys)) != len(document_file_keys):
            raise WeaponryDomainValidationError(
                "documents 不能包含大小写不敏感重复文件名"
            )

        if self.mode == DOCUMENT_SCOPE_EXPLICIT:
            if not requested_file_names:
                raise WeaponryDomainValidationError(
                    "explicit document scope 必须包含请求文件名"
                )
            if document_file_keys != requested_keys:
                raise WeaponryDomainValidationError(
                    "explicit document scope 文档必须与请求文件顺序逐项一致"
                )
        elif requested_file_names:
            raise WeaponryDomainValidationError(
                "category document scope 不能包含请求文件名"
            )

        object.__setattr__(self, "requested_file_names", requested_file_names)
        object.__setattr__(self, "documents", documents)


@dataclass(frozen=True)
class WeaponryExecutionPolicySnapshot:
    """决定 Worker 调用计划与结构化结果规则的不可变策略快照。

    这些值不能在任务重试时重新读取环境变量。模型的实际供应商配置可以继续由 Adapter
    管理，但 Adapter 必须证明其指纹与此快照一致后才能执行。
    """

    extraction_strategy: str
    extraction_prompt_version: str
    extraction_context_strategy: str
    extraction_model_fingerprint: str
    table_merge_policy_version: str
    max_table_rows: int

    def __post_init__(self) -> None:
        if self.extraction_strategy != FILE_AGGREGATE_STRATEGY:
            raise WeaponryDomainValidationError(
                "extraction_strategy 必须是 file_aggregate_v1"
            )
        if self.extraction_prompt_version != EXTRACTION_PROMPT_VERSION:
            raise WeaponryDomainValidationError(
                "当前 extraction_prompt_version 不受支持"
            )
        if self.extraction_context_strategy not in _EXTRACTION_CONTEXT_STRATEGIES:
            raise WeaponryDomainValidationError(
                "extraction_context_strategy 不受支持"
            )
        object.__setattr__(
            self,
            "extraction_model_fingerprint",
            _required_text(
                self.extraction_model_fingerprint,
                name="extraction_model_fingerprint",
            ),
        )
        if self.table_merge_policy_version != TABLE_MERGE_POLICY_VERSION:
            raise WeaponryDomainValidationError(
                "当前 table_merge_policy_version 不受支持"
            )
        if (
            isinstance(self.max_table_rows, bool)
            or not isinstance(self.max_table_rows, int)
            or self.max_table_rows < 1
        ):
            raise WeaponryDomainValidationError("max_table_rows 必须是正整数")


@dataclass(frozen=True)
class AuxiliaryGuidancePolicySnapshot:
    """可选术语规则辅助的完整、可重试策略事实。"""

    policy_id: str
    catalog_fingerprint: str
    top_n: int
    max_context_chars: int

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str):
            raise WeaponryDomainValidationError(
                "auxiliary guidance policy_id 必须是 str"
            )
        if self.policy_id not in _AUXILIARY_GUIDANCE_POLICIES:
            raise WeaponryDomainValidationError(
                "auxiliary guidance policy_id 不受支持"
            )
        if not isinstance(self.catalog_fingerprint, str):
            raise WeaponryDomainValidationError("catalog_fingerprint 必须是 str")
        for name in ("top_n", "max_context_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise WeaponryDomainValidationError(f"{name} 必须是整数")
        if self.policy_id == AUXILIARY_GUIDANCE_NONE:
            if (
                self.catalog_fingerprint
                or self.top_n != 0
                or self.max_context_chars != 0
            ):
                raise WeaponryDomainValidationError(
                    "none 辅助策略不能携带术语目录或上下文配额"
                )
            return

        object.__setattr__(
            self,
            "catalog_fingerprint",
            _required_text(
                self.catalog_fingerprint,
                name="catalog_fingerprint",
            ),
        )
        for name in ("top_n", "max_context_chars"):
            value = getattr(self, name)
            if value < 1:
                raise WeaponryDomainValidationError(f"{name} 必须是正整数")


@dataclass(frozen=True)
class WeaponrySubmission:
    """完成 Web 校验、文档冻结和内部策略注入后的原子受理命令。"""

    architecture_id: int
    request_projection: FrozenJsonObject
    fields: tuple[WeaponryFieldSpecification, ...]
    document_scope: WeaponryDocumentScope
    evidence_selection_policy: EvidenceSelectionPolicy
    execution_policy: WeaponryExecutionPolicySnapshot
    auxiliary_guidance_policy: AuxiliaryGuidancePolicySnapshot
    trace_id: str

    def __post_init__(self) -> None:
        architecture_id = _architecture_id(
            self.architecture_id,
            name="architecture_id",
        )
        if not isinstance(self.request_projection, FrozenJsonObject):
            raise WeaponryDomainValidationError(
                "request_projection 必须是 FrozenJsonObject"
            )
        if not isinstance(self.fields, (tuple, list)) or not self.fields or any(
            not isinstance(item, WeaponryFieldSpecification) for item in self.fields
        ):
            raise WeaponryDomainValidationError(
                "fields 必须包含至少一个 WeaponryFieldSpecification"
            )
        fields = tuple(self.fields)
        if not isinstance(self.document_scope, WeaponryDocumentScope):
            raise WeaponryDomainValidationError(
                "document_scope 必须是 WeaponryDocumentScope"
            )
        if not isinstance(self.evidence_selection_policy, EvidenceSelectionPolicy):
            raise WeaponryDomainValidationError(
                "evidence_selection_policy 必须是 EvidenceSelectionPolicy"
            )
        if not isinstance(self.execution_policy, WeaponryExecutionPolicySnapshot):
            raise WeaponryDomainValidationError(
                "execution_policy 必须是 WeaponryExecutionPolicySnapshot"
            )
        if not isinstance(
            self.auxiliary_guidance_policy,
            AuxiliaryGuidancePolicySnapshot,
        ):
            raise WeaponryDomainValidationError(
                "auxiliary_guidance_policy 必须是 AuxiliaryGuidancePolicySnapshot"
            )
        trace_id = _required_text(self.trace_id, name="trace_id")

        # 原始请求只作为兼容投影保存，但仍需在事务前证明它和内部规范身份/字段完全对应。
        # 这样数据库中不会出现“公开看起来是 A 请求，Worker 实际执行 B 输入”的双重事实。
        projection = self.request_projection.to_dict()
        if projection.get("businessType") != WEAPONRY_BUSINESS_TYPE:
            raise WeaponryDomainValidationError(
                "request_projection.businessType 必须是 weaponry"
            )
        params = projection.get("params")
        if not isinstance(params, dict):
            raise WeaponryDomainValidationError(
                "request_projection.params 必须是对象"
            )
        if _public_architecture_id(params.get("architectureId")) != architecture_id:
            raise WeaponryDomainValidationError(
                "request_projection 与内部 architecture_id 不一致"
            )
        if params.get("weaponryTemplateFieldList") != [
            item.template.to_dict() for item in fields
        ]:
            raise WeaponryDomainValidationError(
                "request_projection 与内部字段快照不一致"
            )
        raw_file_scope = params.get("filePathList")
        if self.document_scope.mode == DOCUMENT_SCOPE_EXPLICIT:
            if not isinstance(raw_file_scope, list) or not raw_file_scope:
                raise WeaponryDomainValidationError(
                    "explicit document scope 与公开请求不一致"
                )
        elif raw_file_scope not in (None, []):
            raise WeaponryDomainValidationError(
                "category document scope 与公开请求不一致"
            )

        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "trace_id", trace_id)

    @property
    def business_key(self) -> str:
        return str(self.architecture_id)

    @property
    def extraction_strategy(self) -> str:
        return self.execution_policy.extraction_strategy

    @property
    def auxiliary_guidance_policy_id(self) -> str:
        return self.auxiliary_guidance_policy.policy_id


@dataclass(frozen=True)
class WeaponryInputSnapshot:
    """从 ``llm_task_executions`` 恢复 Weaponry Worker 所需的全部输入。"""

    schema_version: int
    task_id: str
    architecture_id: int
    fields: tuple[WeaponryFieldSpecification, ...]
    document_scope: WeaponryDocumentScope
    evidence_selection_policy: EvidenceSelectionPolicy
    execution_policy: WeaponryExecutionPolicySnapshot
    auxiliary_guidance_policy: AuxiliaryGuidancePolicySnapshot
    accepted_at: str
    trace_id: str

    def __post_init__(self) -> None:
        # ``bool`` 是 Python 的 ``int`` 子类；持久任务解码必须同时校验类型和值，不能把
        # JSON ``true`` 当作版本号接受。
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != WEAPONRY_INPUT_SCHEMA_VERSION
        ):
            raise WeaponryDomainValidationError(
                "不支持的 weaponry input schema_version"
            )
        object.__setattr__(
            self,
            "task_id",
            _required_text(self.task_id, name="task_id"),
        )
        _architecture_id(self.architecture_id, name="architecture_id")
        if not isinstance(self.fields, (tuple, list)) or not self.fields or any(
            not isinstance(item, WeaponryFieldSpecification) for item in self.fields
        ):
            raise WeaponryDomainValidationError(
                "fields 必须包含至少一个 WeaponryFieldSpecification"
            )
        object.__setattr__(self, "fields", tuple(self.fields))
        if not isinstance(self.document_scope, WeaponryDocumentScope):
            raise WeaponryDomainValidationError(
                "document_scope 必须是 WeaponryDocumentScope"
            )
        if not isinstance(self.evidence_selection_policy, EvidenceSelectionPolicy):
            raise WeaponryDomainValidationError(
                "evidence_selection_policy 必须是 EvidenceSelectionPolicy"
            )
        if not isinstance(self.execution_policy, WeaponryExecutionPolicySnapshot):
            raise WeaponryDomainValidationError(
                "execution_policy 必须是 WeaponryExecutionPolicySnapshot"
            )
        if not isinstance(
            self.auxiliary_guidance_policy,
            AuxiliaryGuidancePolicySnapshot,
        ):
            raise WeaponryDomainValidationError(
                "auxiliary_guidance_policy 必须是 AuxiliaryGuidancePolicySnapshot"
            )
        object.__setattr__(
            self,
            "accepted_at",
            _required_text(self.accepted_at, name="accepted_at"),
        )
        object.__setattr__(
            self,
            "trace_id",
            _required_text(self.trace_id, name="trace_id"),
        )

    @property
    def business_key(self) -> str:
        return str(self.architecture_id)

    @property
    def extraction_strategy(self) -> str:
        return self.execution_policy.extraction_strategy

    @property
    def auxiliary_guidance_policy_id(self) -> str:
        return self.auxiliary_guidance_policy.policy_id

    @classmethod
    def from_submission(
        cls,
        submission: WeaponrySubmission,
        *,
        task_id: str,
        accepted_at: str,
        schema_version: int = WEAPONRY_INPUT_SCHEMA_VERSION,
    ) -> "WeaponryInputSnapshot":
        if not isinstance(submission, WeaponrySubmission):
            raise WeaponryDomainValidationError(
                "submission 必须是 WeaponrySubmission"
            )
        return cls(
            schema_version=schema_version,
            task_id=task_id,
            architecture_id=submission.architecture_id,
            fields=tuple(submission.fields),
            document_scope=submission.document_scope,
            evidence_selection_policy=submission.evidence_selection_policy,
            execution_policy=submission.execution_policy,
            auxiliary_guidance_policy=submission.auxiliary_guidance_policy,
            accepted_at=accepted_at,
            trace_id=submission.trace_id,
        )


def validate_weaponry_result_completeness(
    snapshot: WeaponryInputSnapshot,
    result: WeaponryResult,
) -> None:
    """在提交终态前验证结果完整对应不可变输入快照。

    字段没有检索到内容仍是合法成功结果，但字段对象本身不能缺失。TABLE 允许没有数据行；
    一旦存在数据行，每行必须逐列、同序使用快照中的完整列定义。该规则把“业务空结果”
    与“Worker 漏字段/错模板”的程序错误明确分开。
    """

    if not isinstance(snapshot, WeaponryInputSnapshot):
        raise WeaponryDomainValidationError(
            "snapshot 必须是 WeaponryInputSnapshot"
        )
    if not isinstance(result, WeaponryResult):
        raise WeaponryDomainValidationError("result 必须是 WeaponryResult")
    if (
        result.identity.task_id != snapshot.task_id
        or result.identity.architecture_id != snapshot.architecture_id
    ):
        raise WeaponryDomainValidationError(
            "WeaponryResult execution 身份与输入快照不一致"
        )
    if result.status != WEAPONRY_STATUS_SUCCEEDED:
        return

    if len(result.fields) != len(snapshot.fields):
        raise WeaponryDomainValidationError(
            "成功结果字段数量与输入快照不一致"
        )
    for field_index, (actual, expected) in enumerate(
        zip(result.fields, snapshot.fields, strict=True),
        start=1,
    ):
        if actual.specification != expected:
            raise WeaponryDomainValidationError(
                f"成功结果第{field_index}个字段定义或顺序与输入快照不一致"
            )
        if expected.field_type != "TABLE":
            continue
        for row_index, row in enumerate(actual.table_rows, start=1):
            actual_columns = tuple(cell.specification for cell in row)
            if actual_columns != expected.columns:
                raise WeaponryDomainValidationError(
                    f"成功结果第{field_index}个TABLE字段第{row_index}行列定义不完整或顺序不一致"
                )


__all__ = [
    "AUXILIARY_GUIDANCE_NONE",
    "AUXILIARY_GUIDANCE_TERMS_RULES_V1",
    "AUXILIARY_GUIDANCE_TERMS_RULES_COLUMN_COMPACT_V2",
    "AuxiliaryGuidancePolicySnapshot",
    "DOCUMENT_SCOPE_CATEGORY",
    "DOCUMENT_SCOPE_EXPLICIT",
    "EXTRACTION_CONTEXT_EVIDENCE_ONLY_V1",
    "EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1",
    "TABLE_MERGE_POLICY_VERSION",
    "WEAPONRY_INPUT_SCHEMA_VERSION",
    "WeaponryDocumentScope",
    "WeaponryExecutionPolicySnapshot",
    "WeaponryInputSnapshot",
    "WeaponrySubmission",
    "validate_weaponry_result_completeness",
]
