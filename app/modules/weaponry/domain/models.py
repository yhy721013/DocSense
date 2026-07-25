"""武器谱不可变身份、模板、文档、结果与回调 DTO。

本模块有意不依赖 ``dict`` 的可变嵌套引用。HTTP Adapter 收到的字段模板会先转换为
``FrozenJsonObject``，未知扩展键和原始顺序完整保留；后台任务只能读取不可变快照，
组装回调时再生成全新的可变对象。这样一个请求或 Worker 修改结果时，不会污染其他
并发任务、持久化输入或调用方传入对象。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, TypeAlias, Union

from .errors import WeaponryDomainValidationError


WEAPONRY_BUSINESS_TYPE = "weaponry"
WEAPONRY_STATUS_SUCCEEDED = "2"
WEAPONRY_STATUS_FAILED = "3"
WEAPONRY_SUCCESS_MESSAGE = "解析成功"
WEAPONRY_FAILURE_MESSAGE = "解析失败"
MAX_ARCHITECTURE_ID = 9_223_372_036_854_775_807


JsonScalar: TypeAlias = None | bool | int | float | str
FrozenJsonValue: TypeAlias = Union[
    JsonScalar,
    "FrozenJsonArray",
    "FrozenJsonObject",
]


def _required_text(value: object, *, name: str, preserve: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WeaponryDomainValidationError(f"{name} 必须是非空 str")
    return value if preserve else value.strip()


def _optional_text(value: object, *, name: str, preserve: bool = False) -> str:
    if not isinstance(value, str):
        raise WeaponryDomainValidationError(f"{name} 必须是 str")
    return value if preserve else value.strip()


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WeaponryDomainValidationError(f"{name} 必须是正整数")
    return value


def _freeze_json_value(value: object, *, path: str) -> FrozenJsonValue:
    """递归复制并冻结严格 JSON 值，同时在持久化前拒绝 NaN/Infinity。"""

    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WeaponryDomainValidationError(f"{path} 不能包含 NaN 或 Infinity")
        return value
    if isinstance(value, Mapping):
        items: list[tuple[str, FrozenJsonValue]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise WeaponryDomainValidationError(f"{path} 的对象键必须是 str")
            items.append((key, _freeze_json_value(item, path=f"{path}.{key}")))
        return FrozenJsonObject(tuple(items))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return FrozenJsonArray(
            tuple(
                _freeze_json_value(item, path=f"{path}[{index}]")
                for index, item in enumerate(value)
            )
        )
    raise WeaponryDomainValidationError(
        f"{path} 只能包含严格 JSON 类型，实际为 {type(value).__name__}"
    )


def _thaw_json_value(value: FrozenJsonValue) -> Any:
    if isinstance(value, FrozenJsonObject):
        return value.to_dict()
    if isinstance(value, FrozenJsonArray):
        return value.to_list()
    return value


def _validate_frozen_json_value(value: object, *, path: str) -> None:
    """防止调用方绕过 ``from_mapping`` 直接塞入可变或非 JSON 值。"""

    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WeaponryDomainValidationError(f"{path} 不能包含 NaN 或 Infinity")
        return
    if isinstance(value, FrozenJsonArray):
        return
    if isinstance(value, FrozenJsonObject):
        return
    raise WeaponryDomainValidationError(
        f"{path} 必须是已经冻结的严格 JSON 值"
    )


@dataclass(frozen=True)
class FrozenJsonArray:
    """保持数组顺序的递归不可变 JSON 值。"""

    values: tuple[FrozenJsonValue, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.values, (tuple, list)):
            raise WeaponryDomainValidationError("FrozenJsonArray.values 必须是有序序列")
        values = tuple(self.values)
        for index, value in enumerate(values):
            _validate_frozen_json_value(value, path=f"FrozenJsonArray[{index}]")
        object.__setattr__(self, "values", values)

    def to_list(self) -> list[Any]:
        """返回深复制的普通列表，供回调投影使用。"""

        return [_thaw_json_value(item) for item in self.values]


@dataclass(frozen=True)
class FrozenJsonObject:
    """保持对象插入顺序和未知扩展键的递归不可变 JSON 对象。"""

    items: tuple[tuple[str, FrozenJsonValue], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.items, (tuple, list)):
            raise WeaponryDomainValidationError("FrozenJsonObject.items 必须是有序键值序列")
        raw_items = tuple(self.items)
        items: list[tuple[str, FrozenJsonValue]] = []
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise WeaponryDomainValidationError(
                    "FrozenJsonObject.items 必须包含二元键值对"
                )
            key, _ = item
            if not isinstance(key, str):
                raise WeaponryDomainValidationError("FrozenJsonObject 键必须是 str")
            if key in seen:
                raise WeaponryDomainValidationError(
                    f"FrozenJsonObject 包含重复键: {key}"
                )
            seen.add(key)
            items.append((key, item[1]))
        for key, value in items:
            _validate_frozen_json_value(value, path=f"FrozenJsonObject.{key}")
        object.__setattr__(self, "items", tuple(items))

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        name: str = "json_object",
    ) -> "FrozenJsonObject":
        if not isinstance(value, Mapping):
            raise WeaponryDomainValidationError(f"{name} 必须是 Mapping")
        frozen = _freeze_json_value(value, path=name)
        if not isinstance(frozen, FrozenJsonObject):  # pragma: no cover - 类型防御
            raise WeaponryDomainValidationError(f"{name} 必须是 JSON 对象")
        return frozen

    def get(self, key: str, default: FrozenJsonValue | None = None) -> FrozenJsonValue | None:
        for item_key, value in self.items:
            if item_key == key:
                return value
        return default

    def to_dict(self) -> dict[str, Any]:
        """返回与冻结值无共享可变引用的普通字典。"""

        return {key: _thaw_json_value(value) for key, value in self.items}


@dataclass(frozen=True)
class WeaponryExecutionIdentity:
    """一次武器谱 execution 的稳定身份。"""

    task_id: str
    architecture_id: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_id",
            _required_text(self.task_id, name="task_id"),
        )
        architecture_id = _positive_int(
            self.architecture_id,
            name="architecture_id",
        )
        if architecture_id > MAX_ARCHITECTURE_ID:
            raise WeaponryDomainValidationError(
                f"architecture_id 不能大于 {MAX_ARCHITECTURE_ID}"
            )

    @property
    def business_key(self) -> str:
        return str(self.architecture_id)


@dataclass(frozen=True)
class WeaponryColumnSpecification:
    """TABLE 单列的不可变模板和当前算法语义。"""

    template: FrozenJsonObject
    field_name: str
    field_description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.template, FrozenJsonObject):
            raise WeaponryDomainValidationError("column template 必须是 FrozenJsonObject")
        object.__setattr__(
            self,
            "field_name",
            _required_text(self.field_name, name="column.field_name"),
        )
        object.__setattr__(
            self,
            "field_description",
            _optional_text(
                self.field_description,
                name="column.field_description",
                preserve=True,
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "WeaponryColumnSpecification":
        template = FrozenJsonObject.from_mapping(value, name="column_template")
        raw_field_name = value.get("fieldName")
        if not isinstance(raw_field_name, str):
            raise WeaponryDomainValidationError("column.fieldName 必须是 str")
        if value.get("fieldType") != "INPUT":
            raise WeaponryDomainValidationError("column.fieldType 必须是 INPUT")
        raw_description = value.get("fieldDescription")
        if raw_description is not None and not isinstance(raw_description, str):
            raise WeaponryDomainValidationError(
                "column.fieldDescription 必须是 str 或 None"
            )
        return cls(
            template=template,
            field_name=raw_field_name,
            field_description=raw_description or "",
        )


@dataclass(frozen=True)
class WeaponryFieldSpecification:
    """INPUT/TABLE 字段模板以及检索、抽取都必须使用的字段语义。"""

    template: FrozenJsonObject
    field_name: str
    field_type: str
    field_description: str = ""
    columns: tuple[WeaponryColumnSpecification, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.template, FrozenJsonObject):
            raise WeaponryDomainValidationError("field template 必须是 FrozenJsonObject")
        object.__setattr__(
            self,
            "field_name",
            _required_text(self.field_name, name="field_name"),
        )
        normalized_type = _required_text(
            self.field_type,
            name="field_type",
        ).upper()
        if normalized_type not in {"INPUT", "TABLE"}:
            raise WeaponryDomainValidationError("field_type 只能是 INPUT 或 TABLE")
        object.__setattr__(self, "field_type", normalized_type)
        object.__setattr__(
            self,
            "field_description",
            _optional_text(
                self.field_description,
                name="field_description",
                preserve=True,
            ),
        )
        if not isinstance(self.columns, (tuple, list)):
            raise WeaponryDomainValidationError("columns 必须是有序列定义")
        columns = tuple(self.columns)
        if any(not isinstance(item, WeaponryColumnSpecification) for item in columns):
            raise WeaponryDomainValidationError(
                "columns 只能包含 WeaponryColumnSpecification"
            )
        if normalized_type == "INPUT" and columns:
            raise WeaponryDomainValidationError("INPUT 不能包含 TABLE columns")
        if normalized_type == "TABLE" and not columns:
            raise WeaponryDomainValidationError("TABLE 必须包含至少一列")
        object.__setattr__(self, "columns", columns)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "WeaponryFieldSpecification":
        if not isinstance(value, Mapping):
            raise WeaponryDomainValidationError("field template 必须是 Mapping")
        raw_field_name = value.get("fieldName")
        if not isinstance(raw_field_name, str):
            raise WeaponryDomainValidationError("fieldName 必须是 str")
        raw_field_type = value.get("fieldType")
        if not isinstance(raw_field_type, str):
            raise WeaponryDomainValidationError("fieldType 必须是 str")
        if raw_field_type not in {"INPUT", "TABLE"}:
            raise WeaponryDomainValidationError("fieldType 只能是 INPUT 或 TABLE")
        field_type = raw_field_type
        raw_description = value.get("fieldDescription")
        if raw_description is not None and not isinstance(raw_description, str):
            raise WeaponryDomainValidationError(
                "fieldDescription 必须是 str 或 None"
            )
        columns: list[WeaponryColumnSpecification] = []
        if field_type == "TABLE":
            raw_rows = value.get("tableFieldList")
            if not isinstance(raw_rows, Sequence) or isinstance(
                raw_rows,
                (str, bytes, bytearray),
            ):
                raise WeaponryDomainValidationError("TABLE tableFieldList 必须是有序数组")
            seen_names: set[str] = set()
            for row in raw_rows:
                if not isinstance(row, Sequence) or isinstance(
                    row,
                    (str, bytes, bytearray),
                ):
                    raise WeaponryDomainValidationError(
                        "TABLE tableFieldList 行必须是有序数组"
                    )
                if not row:
                    raise WeaponryDomainValidationError(
                        "TABLE tableFieldList 行不能为空"
                    )
                for raw_cell in row:
                    if not isinstance(raw_cell, Mapping):
                        raise WeaponryDomainValidationError(
                            "TABLE tableFieldList 单元格必须是 Mapping"
                        )
                    column = WeaponryColumnSpecification.from_mapping(raw_cell)
                    if column.field_name in seen_names:
                        continue
                    seen_names.add(column.field_name)
                    columns.append(column)
        return cls(
            template=FrozenJsonObject.from_mapping(value, name="field_template"),
            field_name=raw_field_name,
            field_type=field_type,
            field_description=raw_description or "",
            columns=tuple(columns),
        )


@dataclass(frozen=True)
class WeaponryDocumentSnapshot:
    """受理时冻结、execution 内不可重新选择的文档身份。"""

    sequence_no: int
    document_key: str
    file_name: str
    original_name: str
    ingested_file_name: str
    source_architecture_id: int
    external_document_ref: str
    anything_document_id: str = ""

    def __post_init__(self) -> None:
        _positive_int(self.sequence_no, name="sequence_no")
        for name in (
            "document_key",
            "file_name",
            "ingested_file_name",
            "external_document_ref",
        ):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name=name),
            )
        object.__setattr__(
            self,
            "original_name",
            _required_text(
                self.original_name,
                name="original_name",
                preserve=True,
            ),
        )
        source_architecture_id = _positive_int(
            self.source_architecture_id,
            name="source_architecture_id",
        )
        if source_architecture_id > MAX_ARCHITECTURE_ID:
            raise WeaponryDomainValidationError(
                f"source_architecture_id 不能大于 {MAX_ARCHITECTURE_ID}"
            )
        object.__setattr__(
            self,
            "anything_document_id",
            _optional_text(
                self.anything_document_id,
                name="anything_document_id",
            ),
        )


@dataclass(frozen=True)
class AuxiliaryGuidance:
    """供应商无关的可选辅助语境；它不能成为公开 rows 或事实来源。"""

    guidance_id: str
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "guidance_id",
            _required_text(self.guidance_id, name="guidance_id"),
        )
        object.__setattr__(self, "text", _required_text(self.text, name="guidance.text"))


@dataclass(frozen=True)
class WeaponryAnalyseDataSource:
    """按文件聚合的既有公开 ``analyseDataSource`` DTO。"""

    content: str
    source: str
    occurred_at: str
    file_name: str
    rows: tuple[str, ...]
    translation: str

    def __post_init__(self) -> None:
        for name in ("content", "source", "occurred_at", "file_name", "translation"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise WeaponryDomainValidationError(f"{name} 必须是 str")
        if not isinstance(self.rows, (tuple, list)) or any(
            not isinstance(item, str) for item in self.rows
        ):
            raise WeaponryDomainValidationError("rows 必须是有序文本序列")
        object.__setattr__(self, "rows", tuple(self.rows))

    @classmethod
    def empty(cls) -> "WeaponryAnalyseDataSource":
        return cls("", "", "", "", (), "")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "source": self.source,
            "time": self.occurred_at,
            "fileName": self.file_name,
            "rows": list(self.rows),
            "translate": self.translation,
        }


@dataclass(frozen=True)
class WeaponryTableCellResult:
    """TABLE 输出中的一个单元格及其按文件来源。"""

    specification: WeaponryColumnSpecification
    analyse_data: str
    sources: tuple[WeaponryAnalyseDataSource, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.specification, WeaponryColumnSpecification):
            raise WeaponryDomainValidationError(
                "specification 必须是 WeaponryColumnSpecification"
            )
        if not isinstance(self.analyse_data, str):
            raise WeaponryDomainValidationError("analyse_data 必须是 str")
        if not isinstance(self.sources, (tuple, list)) or any(
            not isinstance(item, WeaponryAnalyseDataSource) for item in self.sources
        ):
            raise WeaponryDomainValidationError(
                "sources 只能包含 WeaponryAnalyseDataSource"
            )
        object.__setattr__(self, "sources", tuple(self.sources))

    def to_public_dict(self) -> dict[str, Any]:
        result = self.specification.template.to_dict()
        result["analyseData"] = self.analyse_data
        result["analyseDataSource"] = [item.to_public_dict() for item in self.sources]
        return result


@dataclass(frozen=True)
class WeaponryFieldResult:
    """一个 INPUT 或 TABLE 字段的领域结果。"""

    specification: WeaponryFieldSpecification
    analyse_data: str = ""
    sources: tuple[WeaponryAnalyseDataSource, ...] = ()
    table_rows: tuple[tuple[WeaponryTableCellResult, ...], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.specification, WeaponryFieldSpecification):
            raise WeaponryDomainValidationError(
                "specification 必须是 WeaponryFieldSpecification"
            )
        if not isinstance(self.analyse_data, str):
            raise WeaponryDomainValidationError("analyse_data 必须是 str")
        if not isinstance(self.sources, (tuple, list)) or any(
            not isinstance(item, WeaponryAnalyseDataSource) for item in self.sources
        ):
            raise WeaponryDomainValidationError(
                "sources 只能包含 WeaponryAnalyseDataSource"
            )
        rows = tuple(tuple(row) for row in self.table_rows)
        if any(
            not row or any(not isinstance(cell, WeaponryTableCellResult) for cell in row)
            for row in rows
        ):
            raise WeaponryDomainValidationError(
                "table_rows 只能包含非空 WeaponryTableCellResult 行"
            )
        if self.specification.field_type == "INPUT" and rows:
            raise WeaponryDomainValidationError("INPUT 结果不能包含 table_rows")
        if self.specification.field_type == "TABLE" and (
            self.analyse_data or self.sources
        ):
            raise WeaponryDomainValidationError(
                "TABLE 根字段不能写入 INPUT analyse_data/sources"
            )
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "table_rows", rows)

    def to_public_dict(self) -> dict[str, Any]:
        result = self.specification.template.to_dict()
        if self.specification.field_type == "INPUT":
            result["analyseData"] = self.analyse_data
            effective_sources = self.sources or (WeaponryAnalyseDataSource.empty(),)
            result["analyseDataSource"] = [
                item.to_public_dict() for item in effective_sources
            ]
        elif self.table_rows:
            result["tableFieldList"] = [
                [cell.to_public_dict() for cell in row] for row in self.table_rows
            ]
        return result


@dataclass(frozen=True)
class WeaponryCallbackPayload:
    """字段固定、可严格投影为既有 weaponry Callback 的终态 DTO。"""

    architecture_id: int
    status: str
    message: str
    fields: tuple[WeaponryFieldResult, ...] = ()

    def __post_init__(self) -> None:
        architecture_id = _positive_int(self.architecture_id, name="architecture_id")
        if architecture_id > MAX_ARCHITECTURE_ID:
            raise WeaponryDomainValidationError(
                f"architecture_id 不能大于 {MAX_ARCHITECTURE_ID}"
            )
        if self.status not in {WEAPONRY_STATUS_SUCCEEDED, WEAPONRY_STATUS_FAILED}:
            raise WeaponryDomainValidationError("status 只能是 2 或 3")
        if not isinstance(self.message, str) or not self.message:
            raise WeaponryDomainValidationError("message 必须是非空 str")
        if not isinstance(self.fields, (tuple, list)) or any(
            not isinstance(item, WeaponryFieldResult) for item in self.fields
        ):
            raise WeaponryDomainValidationError(
                "fields 只能包含 WeaponryFieldResult"
            )
        if self.status == WEAPONRY_STATUS_FAILED and self.fields:
            raise WeaponryDomainValidationError("失败回调不能携带字段结果")
        if self.status == WEAPONRY_STATUS_SUCCEEDED and not self.fields:
            raise WeaponryDomainValidationError("成功回调必须携带字段结果")
        object.__setattr__(self, "fields", tuple(self.fields))

    def to_public_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "status": self.status,
            "architectureId": self.architecture_id,
        }
        if self.status == WEAPONRY_STATUS_SUCCEEDED:
            data["weaponryTemplateFieldList"] = [
                item.to_public_dict() for item in self.fields
            ]
        return {
            "businessType": WEAPONRY_BUSINESS_TYPE,
            "data": data,
            "msg": self.message,
        }


@dataclass(frozen=True)
class WeaponryResult:
    """execution 终态结果；公开回调由该对象显式投影。"""

    identity: WeaponryExecutionIdentity
    status: str
    fields: tuple[WeaponryFieldResult, ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.identity, WeaponryExecutionIdentity):
            raise WeaponryDomainValidationError(
                "identity 必须是 WeaponryExecutionIdentity"
            )
        if self.status not in {WEAPONRY_STATUS_SUCCEEDED, WEAPONRY_STATUS_FAILED}:
            raise WeaponryDomainValidationError("status 只能是 2 或 3")
        if not isinstance(self.fields, (tuple, list)) or any(
            not isinstance(item, WeaponryFieldResult) for item in self.fields
        ):
            raise WeaponryDomainValidationError(
                "fields 只能包含 WeaponryFieldResult"
            )
        if not isinstance(self.message, str):
            raise WeaponryDomainValidationError("message 必须是 str")
        if self.status == WEAPONRY_STATUS_FAILED and self.fields:
            raise WeaponryDomainValidationError("失败结果不能携带字段结果")
        if self.status == WEAPONRY_STATUS_SUCCEEDED and not self.fields:
            raise WeaponryDomainValidationError("成功结果必须携带字段结果")
        object.__setattr__(self, "fields", tuple(self.fields))

    def to_callback(self) -> WeaponryCallbackPayload:
        default_message = (
            WEAPONRY_SUCCESS_MESSAGE
            if self.status == WEAPONRY_STATUS_SUCCEEDED
            else WEAPONRY_FAILURE_MESSAGE
        )
        return WeaponryCallbackPayload(
            architecture_id=self.identity.architecture_id,
            status=self.status,
            message=self.message or default_message,
            fields=self.fields if self.status == WEAPONRY_STATUS_SUCCEEDED else (),
        )


__all__ = [
    "MAX_ARCHITECTURE_ID",
    "WEAPONRY_BUSINESS_TYPE",
    "WEAPONRY_FAILURE_MESSAGE",
    "WEAPONRY_STATUS_FAILED",
    "WEAPONRY_STATUS_SUCCEEDED",
    "WEAPONRY_SUCCESS_MESSAGE",
    "AuxiliaryGuidance",
    "FrozenJsonArray",
    "FrozenJsonObject",
    "WeaponryAnalyseDataSource",
    "WeaponryCallbackPayload",
    "WeaponryColumnSpecification",
    "WeaponryDocumentSnapshot",
    "WeaponryExecutionIdentity",
    "WeaponryFieldResult",
    "WeaponryFieldSpecification",
    "WeaponryResult",
    "WeaponryTableCellResult",
]
