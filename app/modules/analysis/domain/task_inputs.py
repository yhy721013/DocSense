"""文件分析受理快照与 Worker 输入的不可变领域模型。

公开 ``params`` 对象允许携带调用方扩展字段，且这些字段在后台执行期间不能与 Flask 请求、
其他任务或调用方持有的可变字典共享引用。本模块因此把 JSON 值深冻结，再把执行策略、
有效范围和任务身份固定为 ``AnalysisTaskInputV1``。它不读取环境变量、不生成 TaskId，
也不访问数据库或文件系统。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, Sequence, TypeAlias, Union

from .errors import AnalysisContractError
from .architecture_tree import ArchitectureTreeValidationError
from .models import (
    ANALYSIS_CLASSIFICATION_MODES,
    ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
    ANALYSIS_DATA_STANDARD_MODES,
    ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    ANALYSIS_FILENAME_CONSTRAINT_MODES,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
    ANALYSIS_IDENTITY_RESELECT_MODES,
    ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
    MAX_ANALYSIS_MODEL_CALLS,
    MAX_ANALYSIS_PARAMS_PER_REQUEST,
    MAX_ANALYSIS_PHASE_CALLS,
    MAX_ANALYSIS_PROMPT_CHARS,
)
from .ranges import (
    build_effective_analysis_ranges,
    validate_analysis_architecture_ranges,
)
from .rag_naming import AnalysisRagNamingSnapshot


ANALYSIS_BUSINESS_TYPE = "file"
ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V1 = 1
ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V2 = 2
ANALYSIS_TASK_INPUT_SCHEMA_VERSION = 3
ANALYSIS_PROCESSING_PROFILE_LEGACY_OFFICE_V1 = "legacy-office-v1"
ANALYSIS_XLSX_SHEET_POLICY_SINGLE_V1 = "single-sheet-v1"
ANALYSIS_LEGACY_OFFICE_DEFAULT_VERSION_SERIES = "26.2"
_ANALYSIS_LEGACY_OFFICE_SUFFIXES = frozenset({".doc", ".ppt", ".xls"})
ANALYSIS_EFFECTIVE_RANGE_KEYS = (
    "country",
    "channel",
    "format",
    "maturity",
    "security",
    "architectureList",
    "architectureStandardList",
)

# 这些范围在旧链路中会回填服务端默认值，因此持久化快照不得为空；channel 与标准树
# 则允许调用方明确不选择，必须继续保留空数组语义。
_ANALYSIS_REQUIRED_EFFECTIVE_RANGE_KEYS = frozenset(
    {"country", "format", "maturity", "security", "architectureList"}
)

JsonScalar: TypeAlias = None | bool | int | float | str
FrozenJsonValue: TypeAlias = Union[
    JsonScalar,
    "FrozenJsonArray",
    "FrozenJsonObject",
]


def _required_text(value: object, *, name: str, preserve: bool = False) -> str:
    """校验非空文本；公开字段可选择保留原始首尾空白。"""

    if not isinstance(value, str) or not value.strip():
        raise AnalysisContractError(f"{name} 必须是非空 str")
    return value if preserve else value.strip()


def _positive_int(value: object, *, name: str, maximum: int | None = None) -> int:
    """严格校验内部计数，显式拒绝 Python ``bool`` 伪装的整数。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AnalysisContractError(f"{name} 必须是正整数")
    if maximum is not None and value > maximum:
        raise AnalysisContractError(f"{name} 不能超过 {maximum}")
    return value


def _validate_batch_id(value: object) -> str:
    """校验内部 128 位批次身份，避免把业务字段误当作批次键。"""

    if not isinstance(value, str) or len(value) != 32:
        raise AnalysisContractError("batch_id 必须是 32 位小写十六进制字符串")
    allowed = set("0123456789abcdef")
    if any(character not in allowed for character in value):
        raise AnalysisContractError("batch_id 必须是 32 位小写十六进制字符串")
    return value


def _freeze_json_value(value: object, *, path: str) -> FrozenJsonValue:
    """递归深复制严格 JSON 值，拒绝 NaN、Infinity 和 Python 专有对象。"""

    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AnalysisContractError(f"{path} 不能包含 NaN 或 Infinity")
        return value
    if isinstance(value, Mapping):
        items: list[tuple[str, FrozenJsonValue]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise AnalysisContractError(f"{path} 的对象键必须是 str")
            items.append((key, _freeze_json_value(item, path=f"{path}.{key}")))
        return FrozenJsonObject(tuple(items))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return FrozenJsonArray(
            tuple(
                _freeze_json_value(item, path=f"{path}[{index}]")
                for index, item in enumerate(value)
            )
        )
    raise AnalysisContractError(
        f"{path} 只能包含严格 JSON 类型，实际为 {type(value).__name__}"
    )


def _thaw_json_value(value: FrozenJsonValue) -> Any:
    """生成无共享引用的普通 JSON 对象，仅供 Codec 或兼容投影使用。"""

    if isinstance(value, FrozenJsonObject):
        return value.to_dict()
    if isinstance(value, FrozenJsonArray):
        return value.to_list()
    return value


def _validate_frozen_json_value(value: object, *, path: str) -> None:
    """防止调用方绕过工厂方法，直接向快照塞入可变对象。"""

    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AnalysisContractError(f"{path} 不能包含 NaN 或 Infinity")
        return
    if isinstance(value, (FrozenJsonArray, FrozenJsonObject)):
        return
    raise AnalysisContractError(f"{path} 必须是已经冻结的严格 JSON 值")


@dataclass(frozen=True)
class FrozenJsonArray:
    """保持数组原始顺序的递归不可变 JSON 值。"""

    values: tuple[FrozenJsonValue, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.values, (tuple, list)):
            raise AnalysisContractError("FrozenJsonArray.values 必须是有序序列")
        values = tuple(self.values)
        for index, value in enumerate(values):
            _validate_frozen_json_value(value, path=f"FrozenJsonArray[{index}]")
        object.__setattr__(self, "values", values)

    def to_list(self) -> list[Any]:
        """返回普通列表，调用方修改返回值不会污染冻结快照。"""

        return [_thaw_json_value(value) for value in self.values]


@dataclass(frozen=True)
class FrozenJsonObject:
    """保持对象插入顺序、未知扩展键和空值语义的不可变 JSON 对象。"""

    items: tuple[tuple[str, FrozenJsonValue], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.items, (tuple, list)):
            raise AnalysisContractError("FrozenJsonObject.items 必须是有序键值序列")
        items: list[tuple[str, FrozenJsonValue]] = []
        seen: set[str] = set()
        for item in tuple(self.items):
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise AnalysisContractError(
                    "FrozenJsonObject.items 必须包含二元键值对"
                )
            key, value = item
            if not isinstance(key, str):
                raise AnalysisContractError("FrozenJsonObject 键必须是 str")
            if key in seen:
                raise AnalysisContractError(
                    f"FrozenJsonObject 包含重复键: {key}"
                )
            seen.add(key)
            _validate_frozen_json_value(value, path=f"FrozenJsonObject.{key}")
            items.append((key, value))
        object.__setattr__(self, "items", tuple(items))

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        name: str = "json_object",
    ) -> "FrozenJsonObject":
        """冻结请求或持久化 JSON 对象，拒绝不可序列化值。"""

        if not isinstance(value, Mapping):
            raise AnalysisContractError(f"{name} 必须是 Mapping")
        frozen = _freeze_json_value(value, path=name)
        if not isinstance(frozen, FrozenJsonObject):  # pragma: no cover - 防御分支
            raise AnalysisContractError(f"{name} 必须是 JSON 对象")
        return frozen

    def get(
        self,
        key: str,
        default: FrozenJsonValue | None = None,
    ) -> FrozenJsonValue | None:
        """按键读取冻结值，不把内部元组暴露给调用方。"""

        for item_key, value in self.items:
            if item_key == key:
                return value
        return default

    def contains(self, key: str) -> bool:
        """区分字段缺失与显式 ``null``，保留旧 Analysis 的可选字段语义。"""

        return any(item_key == key for item_key, _ in self.items)

    def to_dict(self) -> dict[str, Any]:
        """返回深复制普通字典，供旧兼容调用和 JSON Codec 使用。"""

        return {key: _thaw_json_value(value) for key, value in self.items}


@dataclass(frozen=True)
class AnalysisPolicySnapshot:
    """执行时必须固定的分类和模型调用预算，禁止 Worker 重读环境变量。"""

    classification_mode: str
    filename_constraint_mode: str
    data_standard_mode: str
    identity_reselect_mode: str
    model_candidate_limit: int
    classification_prompt_char_limit: int
    base_leaf_limit: int
    parent_candidate_limit: int
    max_phase_calls: int = MAX_ANALYSIS_PHASE_CALLS
    max_model_calls: int = MAX_ANALYSIS_MODEL_CALLS

    def __post_init__(self) -> None:
        if self.classification_mode not in ANALYSIS_CLASSIFICATION_MODES:
            raise AnalysisContractError("classification_mode 不受支持")
        if self.filename_constraint_mode not in ANALYSIS_FILENAME_CONSTRAINT_MODES:
            raise AnalysisContractError("filename_constraint_mode 不受支持")
        if self.data_standard_mode not in ANALYSIS_DATA_STANDARD_MODES:
            raise AnalysisContractError("data_standard_mode 不受支持")
        if self.identity_reselect_mode not in ANALYSIS_IDENTITY_RESELECT_MODES:
            raise AnalysisContractError("identity_reselect_mode 不受支持")
        model_candidate_limit = _positive_int(
            self.model_candidate_limit,
            name="model_candidate_limit",
            maximum=128,
        )
        classification_prompt_char_limit = _positive_int(
            self.classification_prompt_char_limit,
            name="classification_prompt_char_limit",
            maximum=MAX_ANALYSIS_PROMPT_CHARS,
        )
        base_leaf_limit = _positive_int(
            self.base_leaf_limit,
            name="base_leaf_limit",
            maximum=64,
        )
        parent_candidate_limit = _positive_int(
            self.parent_candidate_limit,
            name="parent_candidate_limit",
            maximum=16,
        )
        if base_leaf_limit + parent_candidate_limit > model_candidate_limit:
            raise AnalysisContractError(
                "base_leaf_limit 与 parent_candidate_limit 之和不能超过 model_candidate_limit"
            )
        if self.max_phase_calls != MAX_ANALYSIS_PHASE_CALLS:
            raise AnalysisContractError("max_phase_calls 必须匹配当前固定合同")
        if self.max_model_calls != MAX_ANALYSIS_MODEL_CALLS:
            raise AnalysisContractError("max_model_calls 必须匹配当前固定合同")
        object.__setattr__(self, "model_candidate_limit", model_candidate_limit)
        object.__setattr__(
            self,
            "classification_prompt_char_limit",
            classification_prompt_char_limit,
        )
        object.__setattr__(self, "base_leaf_limit", base_leaf_limit)
        object.__setattr__(self, "parent_candidate_limit", parent_candidate_limit)

    @classmethod
    def default(cls) -> "AnalysisPolicySnapshot":
        """返回与当前无环境变量配置完全一致的默认策略快照。"""

        return cls(
            classification_mode=ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
            filename_constraint_mode=ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
            data_standard_mode=ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
            identity_reselect_mode=ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
            model_candidate_limit=128,
            classification_prompt_char_limit=MAX_ANALYSIS_PROMPT_CHARS,
            base_leaf_limit=64,
            parent_candidate_limit=16,
        )

    def to_dict(self) -> dict[str, Any]:
        """生成稳定、仅含 JSON 原始类型的 Codec 投影。"""

        return {
            "classification_mode": self.classification_mode,
            "filename_constraint_mode": self.filename_constraint_mode,
            "data_standard_mode": self.data_standard_mode,
            "identity_reselect_mode": self.identity_reselect_mode,
            "model_candidate_limit": self.model_candidate_limit,
            "classification_prompt_char_limit": self.classification_prompt_char_limit,
            "base_leaf_limit": self.base_leaf_limit,
            "parent_candidate_limit": self.parent_candidate_limit,
            "max_phase_calls": self.max_phase_calls,
            "max_model_calls": self.max_model_calls,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        name: str = "policy_snapshot",
    ) -> "AnalysisPolicySnapshot":
        """从持久化 Codec 投影严格恢复策略快照。

        策略字段决定 Worker 的模型调用边界，缺失字段会导致恢复时重新读取运行时配置，
        未知字段则可能掩盖错误版本的 payload。因此这里使用精确键集合校验，遇到不认识
        的输入一律拒绝，而不是静默忽略。
        """

        if not isinstance(value, Mapping):
            raise AnalysisContractError(f"{name} 必须是 Mapping")
        if any(not isinstance(key, str) for key in value):
            raise AnalysisContractError(f"{name} 的键必须全部是 str")
        expected_keys = frozenset(cls.default().to_dict())
        actual_keys = frozenset(value)
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            unknown = sorted(actual_keys - expected_keys)
            raise AnalysisContractError(
                f"{name} 键集合不匹配: missing={missing} unknown={unknown}"
            )
        return cls(
            classification_mode=value["classification_mode"],
            filename_constraint_mode=value["filename_constraint_mode"],
            data_standard_mode=value["data_standard_mode"],
            identity_reselect_mode=value["identity_reselect_mode"],
            model_candidate_limit=value["model_candidate_limit"],
            classification_prompt_char_limit=value[
                "classification_prompt_char_limit"
            ],
            base_leaf_limit=value["base_leaf_limit"],
            parent_candidate_limit=value["parent_candidate_limit"],
            max_phase_calls=value["max_phase_calls"],
            max_model_calls=value["max_model_calls"],
        )


@dataclass(frozen=True)
class AnalysisDocumentProcessingPolicySnapshot:
    """冻结文件预处理承诺，避免 accepted 任务在重启后读取漂移配置。

    快照只保存非敏感、可审计的策略标识，不保存 LibreOffice 可执行文件路径、临时目录或
    密钥。``legacy_office_required`` 描述当前输入是否必须先转换，不表示当前主机一定具备
    转换能力；Worker 仍须按快照验证实际运行能力，无法满足时必须失败关闭。
    """

    legacy_office_required: bool
    processing_profile_id: str
    legacy_office_allowed_version_series: str
    xlsx_sheet_policy: str
    processing_policy_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.legacy_office_required, bool):
            raise AnalysisContractError("legacy_office_required 必须是 bool")
        for field_name in (
            "processing_profile_id",
            "legacy_office_allowed_version_series",
            "xlsx_sheet_policy",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), name=field_name),
            )
        if self.processing_profile_id != ANALYSIS_PROCESSING_PROFILE_LEGACY_OFFICE_V1:
            raise AnalysisContractError("processing_profile_id 不受支持")
        if self.xlsx_sheet_policy != ANALYSIS_XLSX_SHEET_POLICY_SINGLE_V1:
            raise AnalysisContractError("xlsx_sheet_policy 不受支持")
        series_parts = self.legacy_office_allowed_version_series.split(".")
        if len(series_parts) < 2 or any(not part.isdigit() for part in series_parts):
            raise AnalysisContractError(
                "legacy_office_allowed_version_series 必须是数字版本系列"
            )
        normalized_series = ".".join(str(int(part)) for part in series_parts)
        object.__setattr__(
            self,
            "legacy_office_allowed_version_series",
            normalized_series,
        )
        fingerprint = str(self.processing_policy_fingerprint or "").strip().lower()
        if (
            len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise AnalysisContractError("processing_policy_fingerprint 必须是 SHA-256")
        expected = self._calculate_fingerprint(
            legacy_office_required=self.legacy_office_required,
            processing_profile_id=self.processing_profile_id,
            legacy_office_allowed_version_series=normalized_series,
            xlsx_sheet_policy=self.xlsx_sheet_policy,
        )
        if fingerprint != expected:
            raise AnalysisContractError("processing_policy_fingerprint 与策略字段不一致")
        object.__setattr__(self, "processing_policy_fingerprint", fingerprint)

    @classmethod
    def for_source(
        cls,
        source_url: str,
        *,
        business_file_name: str = "",
        allowed_version_series: str = ANALYSIS_LEGACY_OFFICE_DEFAULT_VERSION_SERIES,
    ) -> "AnalysisDocumentProcessingPolicySnapshot":
        """根据受理时已冻结 URL 生成确定策略；不访问文件系统或环境变量。"""

        if not isinstance(source_url, str) or not source_url.strip():
            raise AnalysisContractError("source_url 必须是非空 str")
        # Domain 不依赖 URL/文件系统工具；只需在去掉 query/fragment 后识别三种固定后缀。
        # 同时处理常见的 percent-encoded 点和路径分隔符，结果只用于内部处理策略判定。
        def suffix_of(value: str, *, strip_url_parts: bool) -> str:
            normalized_path = str(value or "").strip()
            if strip_url_parts:
                normalized_path = normalized_path.split("#", 1)[0].split("?", 1)[0]
            normalized_path = (
                normalized_path.replace("%2E", ".")
                .replace("%2e", ".")
                .replace("%2F", "/")
                .replace("%2f", "/")
                .replace("\\", "/")
            )
            basename = normalized_path.rsplit("/", 1)[-1]
            return (
                f".{basename.rsplit('.', 1)[-1].lower()}"
                if "." in basename
                else ""
            )

        source_suffix = suffix_of(source_url, strip_url_parts=True)
        business_suffix = suffix_of(business_file_name, strip_url_parts=False)
        required = bool(
            {source_suffix, business_suffix} & _ANALYSIS_LEGACY_OFFICE_SUFFIXES
        )
        profile = ANALYSIS_PROCESSING_PROFILE_LEGACY_OFFICE_V1
        sheet_policy = ANALYSIS_XLSX_SHEET_POLICY_SINGLE_V1
        normalized_series = str(allowed_version_series or "").strip()
        return cls(
            legacy_office_required=required,
            processing_profile_id=profile,
            legacy_office_allowed_version_series=normalized_series,
            xlsx_sheet_policy=sheet_policy,
            processing_policy_fingerprint=cls._calculate_fingerprint(
                legacy_office_required=required,
                processing_profile_id=profile,
                legacy_office_allowed_version_series=normalized_series,
                xlsx_sheet_policy=sheet_policy,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """生成稳定 JSON 投影，供 V2 Codec 持久化。"""

        return {
            "legacy_office_required": self.legacy_office_required,
            "processing_profile_id": self.processing_profile_id,
            "legacy_office_allowed_version_series": (
                self.legacy_office_allowed_version_series
            ),
            "xlsx_sheet_policy": self.xlsx_sheet_policy,
            "processing_policy_fingerprint": self.processing_policy_fingerprint,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "AnalysisDocumentProcessingPolicySnapshot":
        """严格恢复 V2 处理策略，拒绝缺项、扩展项和被篡改指纹。"""

        if not isinstance(value, Mapping):
            raise AnalysisContractError("document_processing_policy 必须是 Mapping")
        expected_keys = frozenset(
            {
                "legacy_office_required",
                "processing_profile_id",
                "legacy_office_allowed_version_series",
                "xlsx_sheet_policy",
                "processing_policy_fingerprint",
            }
        )
        actual_keys = frozenset(value)
        if actual_keys != expected_keys or any(not isinstance(key, str) for key in value):
            missing = sorted(expected_keys - actual_keys)
            unknown = sorted(str(key) for key in actual_keys - expected_keys)
            raise AnalysisContractError(
                "document_processing_policy 键集合不匹配: "
                f"missing={missing} unknown={unknown}"
            )
        return cls(
            legacy_office_required=value["legacy_office_required"],  # type: ignore[arg-type]
            processing_profile_id=value["processing_profile_id"],  # type: ignore[arg-type]
            legacy_office_allowed_version_series=value[
                "legacy_office_allowed_version_series"
            ],  # type: ignore[arg-type]
            xlsx_sheet_policy=value["xlsx_sheet_policy"],  # type: ignore[arg-type]
            processing_policy_fingerprint=value[
                "processing_policy_fingerprint"
            ],  # type: ignore[arg-type]
        )

    @staticmethod
    def _calculate_fingerprint(
        *,
        legacy_office_required: bool,
        processing_profile_id: str,
        legacy_office_allowed_version_series: str,
        xlsx_sheet_policy: str,
    ) -> str:
        payload = {
            "legacy_office_allowed_version_series": str(
                legacy_office_allowed_version_series
            ).strip(),
            "legacy_office_required": legacy_office_required,
            "processing_profile_id": str(processing_profile_id).strip(),
            "xlsx_sheet_policy": str(xlsx_sheet_policy).strip(),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _business_original_file_name(raw_params: FrozenJsonObject) -> tuple[bool, str]:
    """复现旧链路的 ``originalFileName`` 空值与原值保留规则。"""

    present = raw_params.contains("originalFileName")
    # 这里只读取单个字段，不能为了取得文件名而深复制完整 architectureList。
    # 对非常规 JSON 值仍先解冻再执行旧链路的 ``str(value)``，保持历史兼容语义。
    raw_value = _thaw_json_value(raw_params.get("originalFileName"))
    if raw_value is None:
        return present, ""
    original_name = raw_value if isinstance(raw_value, str) else str(raw_value)
    return present, original_name if original_name.strip() else ""


def _validate_effective_ranges_snapshot(
    effective_ranges: FrozenJsonObject,
) -> None:
    """校验受理/持久化快照的固定范围 Schema。"""

    actual_keys = tuple(key for key, _ in effective_ranges.items)
    if frozenset(actual_keys) != frozenset(ANALYSIS_EFFECTIVE_RANGE_KEYS):
        missing = sorted(set(ANALYSIS_EFFECTIVE_RANGE_KEYS) - set(actual_keys))
        unknown = sorted(set(actual_keys) - set(ANALYSIS_EFFECTIVE_RANGE_KEYS))
        raise AnalysisContractError(
            f"effective_ranges 键集合不匹配: missing={missing} unknown={unknown}"
        )
    for key in ANALYSIS_EFFECTIVE_RANGE_KEYS:
        value = effective_ranges.get(key)
        if not isinstance(value, FrozenJsonArray):
            raise AnalysisContractError(f"effective_ranges.{key} 必须是数组")
        if key in _ANALYSIS_REQUIRED_EFFECTIVE_RANGE_KEYS and not value.values:
            raise AnalysisContractError(f"effective_ranges.{key} 不能为空")
        if any(
            not isinstance(item, FrozenJsonObject) or not item.items
            for item in value.values
        ):
            raise AnalysisContractError(f"effective_ranges.{key} 只能包含非空对象")
    try:
        validate_analysis_architecture_ranges(effective_ranges.to_dict())
    except ArchitectureTreeValidationError as exc:
        # 统一收口为领域合同错误；原始异常只作为 cause 保留，不进入公开响应。
        raise AnalysisContractError("effective_ranges 领域树无效") from exc


@dataclass(frozen=True)
class AnalysisSubmissionSnapshot:
    """单个公开 ``params`` 项在受理前冻结的纯领域输入。"""

    raw_params: FrozenJsonObject
    effective_ranges: FrozenJsonObject
    policy_snapshot: AnalysisPolicySnapshot
    document_processing_policy: AnalysisDocumentProcessingPolicySnapshot
    rag_naming: AnalysisRagNamingSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.raw_params, FrozenJsonObject):
            raise AnalysisContractError("raw_params 必须是 FrozenJsonObject")
        if not isinstance(self.effective_ranges, FrozenJsonObject):
            raise AnalysisContractError("effective_ranges 必须是 FrozenJsonObject")
        if not isinstance(self.policy_snapshot, AnalysisPolicySnapshot):
            raise AnalysisContractError("policy_snapshot 必须是 AnalysisPolicySnapshot")
        if not isinstance(
            self.document_processing_policy,
            AnalysisDocumentProcessingPolicySnapshot,
        ):
            raise AnalysisContractError(
                "document_processing_policy 必须是 AnalysisDocumentProcessingPolicySnapshot"
            )
        if not isinstance(self.rag_naming, AnalysisRagNamingSnapshot):
            raise AnalysisContractError("rag_naming 必须是 AnalysisRagNamingSnapshot")
        # 受理前的 Web Adapter 已校验公开字段；这里再次守住存储边界，避免未来调用方绕过
        # Parser 后写入无法重放的 execution 输入。
        _required_text(self.raw_params.get("fileName"), name="raw_params.fileName")
        _required_text(self.raw_params.get("filePath"), name="raw_params.filePath")
        expected_rag_naming = AnalysisRagNamingSnapshot.from_public_names(
            original_file_name=self.raw_params.get("originalFileName"),
            file_name=self.raw_params.get("fileName"),
        )
        if self.rag_naming != expected_rag_naming:
            raise AnalysisContractError("rag_naming 与 raw_params 命名字段不一致")
        self._validate_effective_ranges()

    def _validate_effective_ranges(self) -> None:
        """校验受理快照的固定范围 Schema，拒绝无法独立重放的毒记录。"""

        _validate_effective_ranges_snapshot(self.effective_ranges)

    @classmethod
    def from_request_params(
        cls,
        raw_params: Mapping[str, object],
        *,
        policy_snapshot: AnalysisPolicySnapshot,
        document_processing_policy: AnalysisDocumentProcessingPolicySnapshot | None = None,
    ) -> "AnalysisSubmissionSnapshot":
        """深冻结请求项，并在受理时一次性计算有效范围默认值。"""

        frozen_params = FrozenJsonObject.from_mapping(
            raw_params,
            name="analysis_params",
        )
        return cls.from_frozen_params(
            frozen_params,
            policy_snapshot=policy_snapshot,
            document_processing_policy=document_processing_policy,
        )

    @classmethod
    def from_frozen_params(
        cls,
        raw_params: FrozenJsonObject,
        *,
        policy_snapshot: AnalysisPolicySnapshot,
        document_processing_policy: AnalysisDocumentProcessingPolicySnapshot | None = None,
    ) -> "AnalysisSubmissionSnapshot":
        """复用 Parser 已冻结参数，避免在受理链中重复复制大型嵌套容器。"""

        if not isinstance(raw_params, FrozenJsonObject):
            raise TypeError("raw_params 必须是 FrozenJsonObject")
        effective_ranges = FrozenJsonObject.from_mapping(
            build_effective_analysis_ranges(raw_params.to_dict()),
            name="effective_ranges",
        )
        resolved_processing_policy = document_processing_policy
        if resolved_processing_policy is None:
            resolved_processing_policy = AnalysisDocumentProcessingPolicySnapshot.for_source(
                _required_text(raw_params.get("filePath"), name="raw_params.filePath"),
                business_file_name=_required_text(
                    raw_params.get("fileName"),
                    name="raw_params.fileName",
                ),
            )
        return cls(
            raw_params=raw_params,
            effective_ranges=effective_ranges,
            policy_snapshot=policy_snapshot,
            document_processing_policy=resolved_processing_policy,
            rag_naming=AnalysisRagNamingSnapshot.from_public_names(
                original_file_name=raw_params.get("originalFileName"),
                file_name=raw_params.get("fileName"),
            ),
        )

    @property
    def file_name(self) -> str:
        """返回用于 Task 最新投影的规范业务键，不回写原始请求。"""

        return _required_text(self.raw_params.get("fileName"), name="raw_params.fileName")

    @property
    def file_path(self) -> str:
        """返回旧链路实际下载前使用的去首尾空白路径。"""

        return _required_text(self.raw_params.get("filePath"), name="raw_params.filePath")

    @property
    def original_file_name(self) -> str:
        """返回旧回调源展示所需的原始业务文件名语义。"""

        return _business_original_file_name(self.raw_params)[1]

    @property
    def original_file_name_present(self) -> bool:
        """保留字段缺失与显式空值的差异，供后续审计或兼容判断使用。"""

        return _business_original_file_name(self.raw_params)[0]


@dataclass(frozen=True)
class AnalysisTaskInputV1:
    """从 ``llm_task_executions`` 解码后可独立重放的完整文件分析输入。"""

    schema_version: int
    task_id: str
    batch_id: str
    batch_sequence: int
    file_name: str
    original_file_name: str
    original_file_name_present: bool
    file_path: str
    raw_params: FrozenJsonObject
    effective_ranges: FrozenJsonObject
    policy_snapshot: AnalysisPolicySnapshot
    accepted_at: str
    trace_id: str

    EXPECTED_SCHEMA_VERSION: ClassVar[int] = ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != self.EXPECTED_SCHEMA_VERSION
        ):
            raise AnalysisContractError(
                f"不支持的 {type(self).__name__} schema_version"
            )
        object.__setattr__(self, "task_id", _required_text(self.task_id, name="task_id"))
        object.__setattr__(self, "batch_id", _validate_batch_id(self.batch_id))
        object.__setattr__(
            self,
            "batch_sequence",
            _positive_int(
                self.batch_sequence,
                name="batch_sequence",
                maximum=MAX_ANALYSIS_PARAMS_PER_REQUEST,
            ),
        )
        if not isinstance(self.raw_params, FrozenJsonObject):
            raise AnalysisContractError("raw_params 必须是 FrozenJsonObject")
        if not isinstance(self.effective_ranges, FrozenJsonObject):
            raise AnalysisContractError("effective_ranges 必须是 FrozenJsonObject")
        if not isinstance(self.policy_snapshot, AnalysisPolicySnapshot):
            raise AnalysisContractError("policy_snapshot 必须是 AnalysisPolicySnapshot")
        # 历史 V1/V2 可能包含 P1 上线前曾被接受、但不符合新 multipart 命名规则的名称。
        # 解码历史快照时只能复核既有范围合同，不能把新入站规则追溯施加到旧任务。
        _validate_effective_ranges_snapshot(self.effective_ranges)
        file_name = _required_text(self.file_name, name="file_name")
        file_path = _required_text(self.file_path, name="file_path")
        raw_file_name = _required_text(
            self.raw_params.get("fileName"),
            name="raw_params.fileName",
        )
        raw_file_path = _required_text(
            self.raw_params.get("filePath"),
            name="raw_params.filePath",
        )
        if file_name != raw_file_name or file_path != raw_file_path:
            raise AnalysisContractError("任务身份与 raw_params.fileName/filePath 不一致")
        if not isinstance(self.original_file_name, str):
            raise AnalysisContractError("original_file_name 必须是 str")
        if not isinstance(self.original_file_name_present, bool):
            raise AnalysisContractError("original_file_name_present 必须是 bool")
        expected_present, expected_original_name = _business_original_file_name(
            self.raw_params
        )
        if (
            self.original_file_name_present != expected_present
            or self.original_file_name != expected_original_name
        ):
            raise AnalysisContractError("original_file_name 与 raw_params 不一致")
        object.__setattr__(self, "file_name", file_name)
        object.__setattr__(self, "file_path", file_path)
        object.__setattr__(
            self,
            "accepted_at",
            _required_text(self.accepted_at, name="accepted_at", preserve=True),
        )
        object.__setattr__(
            self,
            "trace_id",
            _required_text(self.trace_id, name="trace_id", preserve=True),
        )

    @classmethod
    def from_submission(
        cls,
        submission: AnalysisSubmissionSnapshot,
        *,
        task_id: str,
        batch_id: str,
        batch_sequence: int,
        accepted_at: str,
        trace_id: str,
    ) -> "AnalysisTaskInputV1":
        """把受理快照与数据库分配的内部身份合成 Worker 唯一输入。"""

        if not isinstance(submission, AnalysisSubmissionSnapshot):
            raise TypeError("submission 必须是 AnalysisSubmissionSnapshot")
        return cls(
            schema_version=cls.EXPECTED_SCHEMA_VERSION,
            task_id=task_id,
            batch_id=batch_id,
            batch_sequence=batch_sequence,
            file_name=submission.file_name,
            original_file_name=submission.original_file_name,
            original_file_name_present=submission.original_file_name_present,
            file_path=submission.file_path,
            raw_params=submission.raw_params,
            effective_ranges=submission.effective_ranges,
            policy_snapshot=submission.policy_snapshot,
            accepted_at=accepted_at,
            trace_id=trace_id,
        )


@dataclass(frozen=True)
class AnalysisTaskInputV2(AnalysisTaskInputV1):
    """历史 V2 Worker 输入；在 V1 业务字段之上冻结文档处理策略。"""

    document_processing_policy: AnalysisDocumentProcessingPolicySnapshot

    EXPECTED_SCHEMA_VERSION: ClassVar[int] = ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V2

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(
            self.document_processing_policy,
            AnalysisDocumentProcessingPolicySnapshot,
        ):
            raise AnalysisContractError(
                "document_processing_policy 必须是 AnalysisDocumentProcessingPolicySnapshot"
            )
        expected_required = (
            AnalysisDocumentProcessingPolicySnapshot.for_source(
                self.file_path,
                business_file_name=self.file_name,
            )
            .legacy_office_required
        )
        if self.document_processing_policy.legacy_office_required != expected_required:
            raise AnalysisContractError(
                "legacy_office_required 与冻结 file_path 类型不一致"
            )

    @classmethod
    def from_submission(
        cls,
        submission: AnalysisSubmissionSnapshot,
        *,
        task_id: str,
        batch_id: str,
        batch_sequence: int,
        accepted_at: str,
        trace_id: str,
    ) -> "AnalysisTaskInputV2":
        """把受理快照合成为可跨重启重放的 V2 输入。"""

        if not isinstance(submission, AnalysisSubmissionSnapshot):
            raise TypeError("submission 必须是 AnalysisSubmissionSnapshot")
        return cls(
            schema_version=cls.EXPECTED_SCHEMA_VERSION,
            task_id=task_id,
            batch_id=batch_id,
            batch_sequence=batch_sequence,
            file_name=submission.file_name,
            original_file_name=submission.original_file_name,
            original_file_name_present=submission.original_file_name_present,
            file_path=submission.file_path,
            raw_params=submission.raw_params,
            effective_ranges=submission.effective_ranges,
            policy_snapshot=submission.policy_snapshot,
            accepted_at=accepted_at,
            trace_id=trace_id,
            document_processing_policy=submission.document_processing_policy,
        )


@dataclass(frozen=True)
class AnalysisTaskInputV3(AnalysisTaskInputV2):
    """当前 Worker 输入；增加可跨实例重放的 RAG 命名快照。"""

    rag_naming: AnalysisRagNamingSnapshot

    EXPECTED_SCHEMA_VERSION: ClassVar[int] = ANALYSIS_TASK_INPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.rag_naming, AnalysisRagNamingSnapshot):
            raise AnalysisContractError("rag_naming 必须是 AnalysisRagNamingSnapshot")
        expected = AnalysisRagNamingSnapshot.from_public_names(
            original_file_name=self.raw_params.get("originalFileName"),
            file_name=self.raw_params.get("fileName"),
        )
        if self.rag_naming != expected:
            raise AnalysisContractError("rag_naming 与 raw_params 命名字段不一致")

    @classmethod
    def from_submission(
        cls,
        submission: AnalysisSubmissionSnapshot,
        *,
        task_id: str,
        batch_id: str,
        batch_sequence: int,
        accepted_at: str,
        trace_id: str,
    ) -> "AnalysisTaskInputV3":
        """把受理快照合成为包含处理策略和命名事实的当前 V3 输入。"""

        if not isinstance(submission, AnalysisSubmissionSnapshot):
            raise TypeError("submission 必须是 AnalysisSubmissionSnapshot")
        return cls(
            schema_version=cls.EXPECTED_SCHEMA_VERSION,
            task_id=task_id,
            batch_id=batch_id,
            batch_sequence=batch_sequence,
            file_name=submission.file_name,
            original_file_name=submission.original_file_name,
            original_file_name_present=submission.original_file_name_present,
            file_path=submission.file_path,
            raw_params=submission.raw_params,
            effective_ranges=submission.effective_ranges,
            policy_snapshot=submission.policy_snapshot,
            accepted_at=accepted_at,
            trace_id=trace_id,
            document_processing_policy=submission.document_processing_policy,
            rag_naming=submission.rag_naming,
        )


__all__ = (
    "ANALYSIS_BUSINESS_TYPE",
    "ANALYSIS_EFFECTIVE_RANGE_KEYS",
    "ANALYSIS_LEGACY_OFFICE_DEFAULT_VERSION_SERIES",
    "ANALYSIS_PROCESSING_PROFILE_LEGACY_OFFICE_V1",
    "ANALYSIS_TASK_INPUT_SCHEMA_VERSION",
    "ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V1",
    "ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V2",
    "ANALYSIS_XLSX_SHEET_POLICY_SINGLE_V1",
    "AnalysisDocumentProcessingPolicySnapshot",
    "AnalysisPolicySnapshot",
    "AnalysisSubmissionSnapshot",
    "AnalysisTaskInputV1",
    "AnalysisTaskInputV2",
    "AnalysisTaskInputV3",
    "FrozenJsonArray",
    "FrozenJsonObject",
    "FrozenJsonValue",
    "JsonScalar",
)
