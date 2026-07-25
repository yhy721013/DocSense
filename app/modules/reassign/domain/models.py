"""分类节点变更的不可变领域对象。

本模块不会改写公开响应中的原始 ``ArchitectureId``。Web Adapter 负责保留原始值比较、
旧 ID 的首次 ``int(...)`` 查询时机和 HTTP 失败语义；领域层冻结 Adapter 已给出的原始值，
同时在任何 Operation 落库或远端写之前验证新 ID 是否能安全投影为跨数据库可迁移的有符号
64 位整数。这样既保留已经冻结的兼容输入，也不会让 SQLite 的宽松类型亲和性污染恢复现场。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence, TypeAlias, Union

from .errors import ReassignmentDomainValidationError


JsonScalar: TypeAlias = None | bool | int | float | str
FrozenRawJsonValue: TypeAlias = Union[
    JsonScalar,
    "FrozenRawJsonArray",
    "FrozenRawJsonObject",
]

# 这些上限只约束内部持久化与日志诊断字段，不改变任何公开接口参数。
# Adapter 在接收供应商响应时应先脱敏、再按上限截断；Domain 继续拒绝调用方直接构造
# 超长事实，避免完整响应、堆栈或敏感信息进入 Operation/Step 表。
REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH = 512
REASSIGNMENT_ERROR_CODE_MAX_LENGTH = 128
REASSIGNMENT_ERROR_SUMMARY_MAX_LENGTH = 1024
ARCHITECTURE_ID_MIN = -(2**63)
ARCHITECTURE_ID_MAX = 2**63 - 1


class ReassignmentOperationStatus(str, Enum):
    """一次分类节点变更 Saga 的内部生命周期状态。"""

    RESERVED = "reserved"
    RUNNING = "running"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"
    SUCCEEDED = "succeeded"


class ReassignmentStepName(str, Enum):
    """固定步骤名称；持久化层后续以 ``operation_id + step_name`` 保证唯一。"""

    RESERVE_DOCUMENT = "reserve_document"
    DETACH_SOURCE_DOCUMENT = "detach_source_document"
    PREPARE_TARGET_WORKSPACE = "prepare_target_workspace"
    ATTACH_TARGET_DOCUMENT = "attach_target_document"
    COMMIT_LOCAL_ARCHITECTURE = "commit_local_architecture"
    COMPENSATE_TARGET_DOCUMENT = "compensate_target_document"
    COMPENSATE_SOURCE_DOCUMENT = "compensate_source_document"
    FINALIZE_OPERATION = "finalize_operation"


class ReassignmentStepState(str, Enum):
    """单个前向或补偿步骤的写意图与执行结果。

    补偿外部写使用独立的 ``COMPENSATE_*`` Step；Operation 的
    ``COMPENSATING/COMPENSATED`` 表达总体补偿阶段，避免在前向 Step 上维护第二套补偿事实。
    """

    PENDING = "pending"
    MUTATION_STARTED = "mutation_started"
    SUCCEEDED = "succeeded"
    KNOWN_FAILED = "known_failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ReassignmentMutationOutcome(str, Enum):
    """已探测到的副作用事实，不能把未知结果伪装成明确失败。"""

    NOT_STARTED = "not_started"
    CONFIRMED_NO_EFFECT = "confirmed_no_effect"
    CONFIRMED_EFFECT = "confirmed_effect"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ReassignmentCompensationMode(str, Enum):
    """前向失败后的补偿决策分类。"""

    NO_COMPENSATION_NEEDED = "no_compensation_needed"
    COMPENSATE = "compensate"
    PRESERVE_CONFIRMED_LOCAL_COMMIT = "preserve_confirmed_local_commit"
    RECOVERY_REQUIRED = "recovery_required"


class ReassignmentCompensationAction(str, Enum):
    """补偿时严格固定的外部操作顺序。"""

    DETACH_TARGET_DOCUMENT = "detach_target_document"
    RESTORE_SOURCE_DOCUMENT = "restore_source_document"


class ReassignmentBindingState(str, Enum):
    """来源或目标 workspace 文档成员关系的已确认状态。"""

    NOT_APPLICABLE = "not_applicable"
    CONFIRMED_PRESENT = "confirmed_present"
    CONFIRMED_ABSENT = "confirmed_absent"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ReassignmentTerminalEvidenceKind(str, Enum):
    """进入会释放文档保护的终态前必须提供的强类型证据。"""

    FORWARD_SUCCESS_CONFIRMED = "forward_success_confirmed"
    NO_SIDE_EFFECT_FAILURE_CONFIRMED = "no_side_effect_failure_confirmed"
    COMPENSATION_CONFIRMED = "compensation_confirmed"


class ReassignmentResultCategory(str, Enum):
    """交给 Presenter 的业务结果分类；不包含 Operation、lease 或步骤详情。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATED = "compensated"
    RECOVERY_REQUIRED = "recovery_required"


class ReassignmentPublicMessage(str, Enum):
    """接口文档已确认的稳定公开文案；禁止使用供应商或数据库异常动态拼接。"""

    SUCCEEDED = "变更成功"
    DOCUMENT_NOT_FOUND = "文档记录不存在"
    ARCHITECTURE_MISMATCH = "分类不一致，变更失败"
    REMOTE_MIGRATION_FAILED = "知识库节点迁移失败"
    LOCAL_STATE_CONFLICT = "文档状态已变化，分类变更失败"
    CONCURRENT_OPERATION = "文档分类正在变更，请稍后重试"
    COMPENSATION_FAILED = "知识库关联恢复失败"
    RECOVERY_PENDING = "变更结果暂无法确认，请稍后重试"


def _required_text(value: object, *, name: str, strip: bool = True) -> str:
    """校验领域内部必填文本，拒绝隐式 ``str(...)`` 转换。"""

    if not isinstance(value, str):
        raise ReassignmentDomainValidationError(f"{name} 必须是 str")
    normalized = value.strip() if strip else value
    if not normalized.strip():
        raise ReassignmentDomainValidationError(f"{name} 不能为空")
    return normalized


def _optional_text(value: object, *, name: str) -> str | None:
    """校验可空不透明文本，并保留路径、标识等原始字符。"""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ReassignmentDomainValidationError(f"{name} 必须是 str 或 None")
    return value


def _optional_nonempty_text(
    value: object,
    *,
    name: str,
    max_length: int | None = None,
) -> str | None:
    """校验可空内部标识，并按字段职责执行确定的长度上限。"""

    if value is None:
        return None
    normalized = _required_text(value, name=name, strip=False)
    if max_length is not None and len(normalized) > max_length:
        raise ReassignmentDomainValidationError(
            f"{name} 长度不能超过 {max_length}"
        )
    return normalized


def _normalized_utc_timestamp(value: object, *, name: str) -> str:
    """把带时区 ISO-8601 时间规范化为固定 UTC 文本，保证跨实例可比较。

    Domain 不读取当前时钟，因此这里只验证格式与时区，不判断租约是否已经过期。
    Repository 后续必须使用注入时钟或数据库时间判断所有权。
    """

    normalized = _required_text(value, name=name)
    iso_value = (
        f"{normalized[:-1]}+00:00"
        if normalized.endswith(("Z", "z"))
        else normalized
    )
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError as exc:
        raise ReassignmentDomainValidationError(
            f"{name} 必须是带时区的 ISO-8601 时间"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReassignmentDomainValidationError(
            f"{name} 必须包含时区"
        )
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _strict_int(value: object, *, name: str) -> int:
    """只接受已由边界层完成转换的 Python ``int``，不在领域层再做 ID 解释。"""

    if type(value) is not int:
        raise ReassignmentDomainValidationError(f"{name} 必须是 int")
    return value


def _positive_int(value: object, *, name: str) -> int:
    """校验内部行号、fencing token 等必须为正整数的事实。"""

    normalized = _strict_int(value, name=name)
    if normalized < 1:
        raise ReassignmentDomainValidationError(f"{name} 必须是正整数")
    return normalized


def _freeze_raw_json_value(value: object, *, path: str) -> FrozenRawJsonValue:
    """递归复制并冻结原始 JSON 值，不把数值或字符串转换为另一种 ID 语义。"""

    if isinstance(value, (FrozenRawJsonArray, FrozenRawJsonObject)):
        return value
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReassignmentDomainValidationError(
                f"{path} 不能包含 NaN 或 Infinity"
            )
        return value
    if isinstance(value, Mapping):
        items: list[tuple[str, FrozenRawJsonValue]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReassignmentDomainValidationError(
                    f"{path} 的对象键必须是 str"
                )
            items.append(
                (key, _freeze_raw_json_value(item, path=f"{path}.{key}"))
            )
        return FrozenRawJsonObject(tuple(items))
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return FrozenRawJsonArray(
            tuple(
                _freeze_raw_json_value(item, path=f"{path}[{index}]")
                for index, item in enumerate(value)
            )
        )
    raise ReassignmentDomainValidationError(
        f"{path} 只能包含严格 JSON 类型，实际为 {type(value).__name__}"
    )


def _validate_frozen_raw_json_value(value: object, *, path: str) -> None:
    """拒绝通过 Frozen 容器构造函数混入可变或非 JSON 值。"""

    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReassignmentDomainValidationError(
                f"{path} 不能包含 NaN 或 Infinity"
            )
        return
    if isinstance(value, (FrozenRawJsonArray, FrozenRawJsonObject)):
        return
    raise ReassignmentDomainValidationError(
        f"{path} 必须是已冻结的严格 JSON 值"
    )


def _thaw_raw_json_value(value: FrozenRawJsonValue) -> Any:
    """生成不共享可变引用的新 JSON 值，供未来 Presenter 兼容投影使用。"""

    if isinstance(value, FrozenRawJsonArray):
        return value.to_list()
    if isinstance(value, FrozenRawJsonObject):
        return value.to_dict()
    return value


@dataclass(frozen=True)
class FrozenRawJsonArray:
    """保留数组顺序的递归不可变原始 JSON 值。"""

    values: tuple[FrozenRawJsonValue, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.values, (tuple, list)):
            raise ReassignmentDomainValidationError(
                "FrozenRawJsonArray.values 必须是有序序列"
            )
        values = tuple(self.values)
        for index, value in enumerate(values):
            _validate_frozen_raw_json_value(
                value,
                path=f"FrozenRawJsonArray[{index}]",
            )
        object.__setattr__(self, "values", values)

    def to_list(self) -> list[Any]:
        """返回全新列表，调用方修改它不会影响已冻结的请求原值。"""

        return [_thaw_raw_json_value(value) for value in self.values]


@dataclass(frozen=True)
class FrozenRawJsonObject:
    """保留对象插入顺序的递归不可变原始 JSON 对象。"""

    items: tuple[tuple[str, FrozenRawJsonValue], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.items, (tuple, list)):
            raise ReassignmentDomainValidationError(
                "FrozenRawJsonObject.items 必须是有序键值序列"
            )
        raw_items = tuple(self.items)
        items: list[tuple[str, FrozenRawJsonValue]] = []
        seen_keys: set[str] = set()
        for item in raw_items:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ReassignmentDomainValidationError(
                    "FrozenRawJsonObject.items 必须包含二元键值对"
                )
            key, value = item
            if not isinstance(key, str):
                raise ReassignmentDomainValidationError(
                    "FrozenRawJsonObject 的键必须是 str"
                )
            if key in seen_keys:
                raise ReassignmentDomainValidationError(
                    f"FrozenRawJsonObject 包含重复键: {key}"
                )
            seen_keys.add(key)
            _validate_frozen_raw_json_value(
                value,
                path=f"FrozenRawJsonObject.{key}",
            )
            items.append((key, value))
        object.__setattr__(self, "items", tuple(items))

    def to_dict(self) -> dict[str, Any]:
        """返回全新字典，保留冻结时的键顺序和嵌套值。"""

        return {key: _thaw_raw_json_value(value) for key, value in self.items}


@dataclass(frozen=True)
class ReassignmentRawValue:
    """未经语义规范化的公开 ArchitectureId 原始值。

    ``value`` 可以是严格 JSON 值。构造时会深冻结列表和对象；这使未来同步请求、补偿和
    恢复读到的是同一份原始事实，而不是调用方仍可改写的 ``dict`` 或 ``list``。
    """

    value: object

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            _freeze_raw_json_value(self.value, path="architecture_id_raw"),
        )

    @classmethod
    def from_external_value(cls, value: object) -> "ReassignmentRawValue":
        """从 Adapter 已接收的原始 JSON 值创建不可变包装，不执行 ID 类型转换。"""

        if isinstance(value, cls):
            return value
        return cls(value=value)

    def to_python(self) -> Any:
        """返回不共享嵌套引用的原始值副本。"""

        return _thaw_raw_json_value(self.value)  # type: ignore[arg-type]

    def canonical_json(self) -> str:
        """返回仅供内部幂等键使用的确定性 JSON 表示。

        该方法保留数值、布尔和字符串的原始 JSON 类型，例如 ``1`` 与 ``"1"`` 的编码
        不同；它不参与公开参数校验、数据库查询值转换或 HTTP 响应组装。
        """

        return json.dumps(
            self.to_python(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


def _as_raw_architecture_value(value: object) -> ReassignmentRawValue:
    """接受包装实例或原始 JSON 值，并统一冻结为领域值对象。"""

    return ReassignmentRawValue.from_external_value(value)


def _require_non_null_raw_value(
    value: ReassignmentRawValue,
    *,
    name: str,
) -> ReassignmentRawValue:
    """领域命令只接收已通过 Web 非空校验的 ArchitectureId 原始值。"""

    if value.value is None:
        raise ReassignmentDomainValidationError(f"{name} 不能为空")
    return value


def architecture_id_storage_value(
    raw: "ReassignmentRawValue",
    *,
    name: str,
) -> int:
    """把已冻结兼容输入投影为数据库权威整数。

    对外契约仍要求 Long。为避免突然把历史可用请求改成新的 HTTP 400，本阶段仅兼容：
    历史黄金资产已经冻结的 ``false``、整数，以及可按十进制解析的整数字符串。
    字符串前后空白与正负号延续 Python/SQLite 的既有宽松行为；小数字符串、科学计数法、
    非数字文本、容器和超出有符号 64 位范围的值会在创建 Operation 前明确失败。
    """

    if not isinstance(raw, ReassignmentRawValue):
        raise ReassignmentDomainValidationError(
            f"{name} 必须是 ReassignmentRawValue"
        )
    value = raw.to_python()
    if value is False:
        storage_value = 0
    elif type(value) is int:
        storage_value = value
    elif isinstance(value, str):
        try:
            storage_value = int(value.strip(), 10)
        except ValueError as exc:
            raise ReassignmentDomainValidationError(
                f"{name} 不能投影为有符号 64 位整数"
            ) from exc
    else:
        raise ReassignmentDomainValidationError(
            f"{name} 不能投影为有符号 64 位整数"
        )
    if not ARCHITECTURE_ID_MIN <= storage_value <= ARCHITECTURE_ID_MAX:
        raise ReassignmentDomainValidationError(
            f"{name} 超出有符号 64 位整数范围"
        )
    return storage_value


@dataclass(frozen=True)
class ReassignDocumentCommand:
    """Web Adapter 交给同步 Saga 的不可变分类节点变更命令。"""

    file_name: str
    old_architecture_id_raw: object
    new_architecture_id_raw: object
    old_architecture_id_query_value: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "file_name",
            _required_text(self.file_name, name="file_name"),
        )
        old_architecture_id_raw = _require_non_null_raw_value(
            _as_raw_architecture_value(self.old_architecture_id_raw),
            name="old_architecture_id_raw",
        )
        object.__setattr__(
            self,
            "old_architecture_id_raw",
            old_architecture_id_raw,
        )
        new_architecture_id_raw = _require_non_null_raw_value(
            _as_raw_architecture_value(self.new_architecture_id_raw),
            name="new_architecture_id_raw",
        )
        # 只做可持久化性验证，不覆盖原始值。Presenter 仍会回显请求中的原生 JSON 值，
        # workspace 命名与幂等键也继续使用被冻结的原始语义。
        architecture_id_storage_value(
            new_architecture_id_raw,
            name="new_architecture_id_raw",
        )
        object.__setattr__(
            self,
            "new_architecture_id_raw",
            new_architecture_id_raw,
        )
        old_architecture_id_query_value = _strict_int(
            self.old_architecture_id_query_value,
            name="old_architecture_id_query_value",
        )
        # Web Adapter 仍拥有公开 ``int(oldArchitectureId)`` 的首次转换时点和 HTTP
        # 失败语义。这里仅重复计算一次内部一致性断言，防止错误的 Adapter/Fake/恢复 Codec
        # 把原始值 ``"999"`` 与查询值 ``11`` 组合成不可审计的命令。
        try:
            verified_query_value = int(old_architecture_id_raw.to_python())
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReassignmentDomainValidationError(
                "old_architecture_id_raw 与已解析查询值不一致"
            ) from exc
        if verified_query_value != old_architecture_id_query_value:
            raise ReassignmentDomainValidationError(
                "old_architecture_id_raw 与 old_architecture_id_query_value 不一致"
            )
        object.__setattr__(
            self,
            "old_architecture_id_query_value",
            old_architecture_id_query_value,
        )


@dataclass(frozen=True)
class ReassignmentDocumentSnapshot:
    """受理后固定的本地文档身份和外部文档位置快照。"""

    document_row_id: int
    file_name: str
    source_architecture_id: int
    anything_doc_id: str | None = None
    doc_path: str | None = None
    original_file_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "document_row_id",
            _positive_int(self.document_row_id, name="document_row_id"),
        )
        object.__setattr__(
            self,
            "file_name",
            _required_text(self.file_name, name="file_name", strip=False),
        )
        object.__setattr__(
            self,
            "source_architecture_id",
            _strict_int(
                self.source_architecture_id,
                name="source_architecture_id",
            ),
        )
        object.__setattr__(
            self,
            "anything_doc_id",
            _optional_text(self.anything_doc_id, name="anything_doc_id"),
        )
        # 空 doc_path 是已冻结的兼容分支：后续 Application 必须跳过远端成员关系迁移，
        # 但仍执行本地条件更新，因此这里不能把空字符串改写为 None 或拒绝。
        object.__setattr__(
            self,
            "doc_path",
            _optional_text(self.doc_path, name="doc_path"),
        )
        object.__setattr__(
            self,
            "original_file_name",
            _optional_text(
                self.original_file_name,
                name="original_file_name",
            ),
        )

    @property
    def requires_remote_membership_change(self) -> bool:
        """仅在存在非空外部文档位置时需要 AnythingLLM 成员关系步骤。"""

        return bool(self.doc_path)


@dataclass(frozen=True)
class ReassignmentOperation:
    """一条持久化 Saga 的内部所有权、文档身份和当前状态快照。"""

    operation_id: str
    document: ReassignmentDocumentSnapshot
    source_architecture_id: int
    source_architecture_raw: object
    target_architecture_raw: object
    status: ReassignmentOperationStatus = ReassignmentOperationStatus.RESERVED
    current_step: ReassignmentStepName | None = None
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: str | None = None
    fencing_token: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _required_text(self.operation_id, name="operation_id"),
        )
        if not isinstance(self.document, ReassignmentDocumentSnapshot):
            raise ReassignmentDomainValidationError(
                "document 必须是 ReassignmentDocumentSnapshot"
            )
        source_architecture_id = _strict_int(
            self.source_architecture_id,
            name="source_architecture_id",
        )
        if source_architecture_id != self.document.source_architecture_id:
            raise ReassignmentDomainValidationError(
                "Operation 的 source_architecture_id 必须与文档快照一致"
            )
        object.__setattr__(
            self,
            "source_architecture_id",
            source_architecture_id,
        )
        object.__setattr__(
            self,
            "source_architecture_raw",
            _require_non_null_raw_value(
                _as_raw_architecture_value(self.source_architecture_raw),
                name="source_architecture_raw",
            ),
        )
        object.__setattr__(
            self,
            "target_architecture_raw",
            _require_non_null_raw_value(
                _as_raw_architecture_value(self.target_architecture_raw),
                name="target_architecture_raw",
            ),
        )
        if not isinstance(self.status, ReassignmentOperationStatus):
            raise ReassignmentDomainValidationError(
                "status 必须是 ReassignmentOperationStatus"
            )
        if self.current_step is not None and not isinstance(
            self.current_step,
            ReassignmentStepName,
        ):
            raise ReassignmentDomainValidationError(
                "current_step 必须是 ReassignmentStepName 或 None"
            )
        self._validate_lease_fact()

    def _validate_lease_fact(self) -> None:
        """lease 信息必须成组出现，防止把半份所有权事实传给后续 Repository。"""

        lease_owner = _optional_nonempty_text(
            self.lease_owner,
            name="lease_owner",
        )
        lease_token = _optional_nonempty_text(
            self.lease_token,
            name="lease_token",
        )
        lease_expires_at = _optional_nonempty_text(
            self.lease_expires_at,
            name="lease_expires_at",
        )
        fencing_token = self.fencing_token
        facts = (lease_owner, lease_token, lease_expires_at, fencing_token)
        if all(value is None for value in facts):
            return
        if any(value is None for value in facts):
            raise ReassignmentDomainValidationError(
                "lease_owner、lease_token、lease_expires_at 和 fencing_token 必须成组出现"
            )
        object.__setattr__(self, "lease_owner", lease_owner)
        object.__setattr__(self, "lease_token", lease_token)
        object.__setattr__(
            self,
            "lease_expires_at",
            _normalized_utc_timestamp(
                lease_expires_at,
                name="lease_expires_at",
            ),
        )
        object.__setattr__(
            self,
            "fencing_token",
            _positive_int(fencing_token, name="fencing_token"),
        )


@dataclass(frozen=True)
class ReassignmentStep:
    """一个固定 Saga 步骤的持久化写意图与结果快照。"""

    operation_id: str
    step_name: ReassignmentStepName
    idempotency_key: str
    state: ReassignmentStepState = ReassignmentStepState.PENDING
    write_intent_recorded: bool = False
    external_reference: str | None = None
    error_code: str | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _required_text(self.operation_id, name="operation_id"),
        )
        if not isinstance(self.step_name, ReassignmentStepName):
            raise ReassignmentDomainValidationError(
                "step_name 必须是 ReassignmentStepName"
            )
        object.__setattr__(
            self,
            "idempotency_key",
            _required_text(self.idempotency_key, name="idempotency_key"),
        )
        if not isinstance(self.state, ReassignmentStepState):
            raise ReassignmentDomainValidationError(
                "state 必须是 ReassignmentStepState"
            )
        if type(self.write_intent_recorded) is not bool:
            raise ReassignmentDomainValidationError(
                "write_intent_recorded 必须是 bool"
            )
        if (
            self.state is not ReassignmentStepState.PENDING
            and not self.write_intent_recorded
        ):
            raise ReassignmentDomainValidationError(
                "非 pending Step 必须先持久化 write_intent_recorded"
            )
        object.__setattr__(
            self,
            "external_reference",
            _optional_nonempty_text(
                self.external_reference,
                name="external_reference",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "error_code",
            _optional_nonempty_text(
                self.error_code,
                name="error_code",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "error_summary",
            _optional_nonempty_text(
                self.error_summary,
                name="error_summary",
                max_length=REASSIGNMENT_ERROR_SUMMARY_MAX_LENGTH,
            ),
        )


@dataclass(frozen=True)
class ReassignmentCompensationFacts:
    """补偿决策所需的三个已确认前向副作用事实。"""

    source_detach_outcome: ReassignmentMutationOutcome
    target_attach_outcome: ReassignmentMutationOutcome
    local_commit_outcome: ReassignmentMutationOutcome
    remote_membership_required: bool = True
    source_binding_state: ReassignmentBindingState = (
        ReassignmentBindingState.OUTCOME_UNKNOWN
    )
    target_binding_state: ReassignmentBindingState = (
        ReassignmentBindingState.OUTCOME_UNKNOWN
    )

    def __post_init__(self) -> None:
        for name in (
            "source_detach_outcome",
            "target_attach_outcome",
            "local_commit_outcome",
        ):
            if not isinstance(getattr(self, name), ReassignmentMutationOutcome):
                raise ReassignmentDomainValidationError(
                    f"{name} 必须是 ReassignmentMutationOutcome"
                )
        if type(self.remote_membership_required) is not bool:
            raise ReassignmentDomainValidationError(
                "remote_membership_required 必须是 bool"
            )
        if not isinstance(
            self.source_binding_state,
            ReassignmentBindingState,
        ) or not isinstance(
            self.target_binding_state,
            ReassignmentBindingState,
        ):
            raise ReassignmentDomainValidationError(
                "source_binding_state 和 target_binding_state "
                "必须是 ReassignmentBindingState"
            )
        if (
            self.remote_membership_required
            and self.target_binding_state
            is ReassignmentBindingState.NOT_APPLICABLE
        ):
            raise ReassignmentDomainValidationError(
                "需要远端迁移时 target_binding_state 不能是 not_applicable"
            )
        if not self.remote_membership_required and (
            self.source_binding_state
            is not ReassignmentBindingState.NOT_APPLICABLE
            or self.target_binding_state
            is not ReassignmentBindingState.NOT_APPLICABLE
        ):
            raise ReassignmentDomainValidationError(
                "本地-only路径的 source/target binding state 必须是 not_applicable"
            )


@dataclass(frozen=True)
class ReassignmentTerminalEvidence:
    """释放同文档保护前由 Application 显式提交的终态证据类型。

    本对象不替代 Repository 的 lease/fencing 校验。它用于阻止调用方仅凭一个目标枚举，
    就把仍可能存在副作用的 Operation 标记为 ``failed`` 或 ``succeeded``。
    """

    kind: ReassignmentTerminalEvidenceKind

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReassignmentTerminalEvidenceKind):
            raise ReassignmentDomainValidationError(
                "kind 必须是 ReassignmentTerminalEvidenceKind"
            )


@dataclass(frozen=True)
class ReassignmentCompensationDecision:
    """纯规则得出的补偿模式与固定动作顺序。"""

    mode: ReassignmentCompensationMode
    actions: tuple[ReassignmentCompensationAction, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ReassignmentCompensationMode):
            raise ReassignmentDomainValidationError(
                "mode 必须是 ReassignmentCompensationMode"
            )
        if not isinstance(self.actions, (tuple, list)):
            raise ReassignmentDomainValidationError("actions 必须是有序动作序列")
        actions = tuple(self.actions)
        if any(
            not isinstance(action, ReassignmentCompensationAction)
            for action in actions
        ):
            raise ReassignmentDomainValidationError(
                "actions 只能包含 ReassignmentCompensationAction"
            )
        if len(set(actions)) != len(actions):
            raise ReassignmentDomainValidationError("actions 不能包含重复补偿动作")
        valid_compensation_orders = {
            (ReassignmentCompensationAction.DETACH_TARGET_DOCUMENT,),
            (ReassignmentCompensationAction.RESTORE_SOURCE_DOCUMENT,),
            (
                ReassignmentCompensationAction.DETACH_TARGET_DOCUMENT,
                ReassignmentCompensationAction.RESTORE_SOURCE_DOCUMENT,
            ),
        }
        if self.mode is ReassignmentCompensationMode.COMPENSATE:
            if actions not in valid_compensation_orders:
                raise ReassignmentDomainValidationError(
                    "补偿动作必须按先删除目标、后恢复来源的既定顺序"
                )
        elif actions:
            raise ReassignmentDomainValidationError(
                "非补偿模式不能携带补偿动作"
            )
        object.__setattr__(self, "actions", actions)


@dataclass(frozen=True)
class ReassignmentResult:
    """Application 交给 Presenter 的最小业务结果，故意不包含内部执行细节。"""

    category: ReassignmentResultCategory
    public_message: ReassignmentPublicMessage

    def __post_init__(self) -> None:
        if not isinstance(self.category, ReassignmentResultCategory):
            raise ReassignmentDomainValidationError(
                "category 必须是 ReassignmentResultCategory"
            )
        if not isinstance(self.public_message, ReassignmentPublicMessage):
            raise ReassignmentDomainValidationError(
                "public_message 必须是 ReassignmentPublicMessage"
            )
        allowed_messages = {
            ReassignmentResultCategory.SUCCEEDED: {
                ReassignmentPublicMessage.SUCCEEDED,
            },
            ReassignmentResultCategory.FAILED: {
                ReassignmentPublicMessage.DOCUMENT_NOT_FOUND,
                ReassignmentPublicMessage.ARCHITECTURE_MISMATCH,
                ReassignmentPublicMessage.REMOTE_MIGRATION_FAILED,
                ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
                ReassignmentPublicMessage.CONCURRENT_OPERATION,
            },
            ReassignmentResultCategory.COMPENSATED: {
                ReassignmentPublicMessage.REMOTE_MIGRATION_FAILED,
                ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
            },
            ReassignmentResultCategory.RECOVERY_REQUIRED: {
                ReassignmentPublicMessage.COMPENSATION_FAILED,
                ReassignmentPublicMessage.RECOVERY_PENDING,
            },
        }
        if self.public_message not in allowed_messages[self.category]:
            raise ReassignmentDomainValidationError(
                "public_message 与 category 的公开语义不一致"
            )

    @property
    def success(self) -> bool:
        """只有完整成功才允许 Presenter 选择既有 HTTP 200 成功结构。"""

        return self.category is ReassignmentResultCategory.SUCCEEDED

    @property
    def public_message_text(self) -> str:
        """返回 Presenter 可直接使用的已批准公开文案。"""

        return self.public_message.value


__all__ = [
    "ARCHITECTURE_ID_MAX",
    "ARCHITECTURE_ID_MIN",
    "FrozenRawJsonArray",
    "FrozenRawJsonObject",
    "FrozenRawJsonValue",
    "JsonScalar",
    "ReassignDocumentCommand",
    "ReassignmentCompensationAction",
    "ReassignmentCompensationDecision",
    "ReassignmentCompensationFacts",
    "ReassignmentCompensationMode",
    "ReassignmentBindingState",
    "ReassignmentDocumentSnapshot",
    "ReassignmentMutationOutcome",
    "ReassignmentOperation",
    "ReassignmentOperationStatus",
    "ReassignmentPublicMessage",
    "ReassignmentRawValue",
    "ReassignmentResult",
    "ReassignmentResultCategory",
    "ReassignmentStep",
    "ReassignmentStepName",
    "ReassignmentStepState",
    "ReassignmentTerminalEvidence",
    "ReassignmentTerminalEvidenceKind",
    "REASSIGNMENT_ERROR_CODE_MAX_LENGTH",
    "REASSIGNMENT_ERROR_SUMMARY_MAX_LENGTH",
    "REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH",
    "architecture_id_storage_value",
]
