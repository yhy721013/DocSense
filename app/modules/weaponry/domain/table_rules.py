"""TABLE 模型回答清洗、行身份、合并与回调组装纯规则。"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .errors import WeaponryDomainValidationError
from .models import (
    WeaponryAnalyseDataSource,
    WeaponryFieldSpecification,
    WeaponryTableCellResult,
)


MAX_TABLE_ROWS = 100
_EMPTY_CELL_VALUES = {
    "null",
    "none",
    "nan",
    "未找到",
    "未检索到",
    "无明确依据",
    "未知",
    "不详",
}
_STRONG_ROW_IDENTITY_TOKENS = ("名称", "型号", "编号", "代号", "标识")
_WEAK_TYPE_TOKEN = "类型"


def normalize_table_cell_value(value: object) -> str:
    """把模型返回的任意 JSON 单元格收敛为稳定文本。"""

    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    if isinstance(value, (list, tuple)):
        values = [normalize_table_cell_value(item) for item in value]
        return ", ".join(item for item in values if item)
    if isinstance(value, Mapping):
        if not value:
            return ""
        try:
            return json.dumps(
                dict(value),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return ""
    text = str(value).strip()
    if text.lower() in _EMPTY_CELL_VALUES or text in _EMPTY_CELL_VALUES:
        return ""
    return text


def _normalize_json_key(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def _clean_table_json_response(text: str) -> str:
    if not isinstance(text, str):
        raise WeaponryDomainValidationError("table response 必须是 str")
    cleaned = text.strip()
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>")[-1]
    cleaned = cleaned.replace("<think>", "").strip()
    match = re.search(
        r"```(?:json)?\s*([\s\S]*?)\s*```",
        cleaned,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    match = re.search(
        r"```(?:json)?\s*([\s\S]*)",
        cleaned,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else cleaned


def _json_substring(text: str, start_char: str, end_char: str) -> str:
    start = text.find(start_char)
    end = text.rfind(end_char)
    if start == -1 or end == -1 or end <= start:
        return ""
    return text[start : end + 1]


def _load_table_json_response(text: str) -> object | None:
    cleaned = _clean_table_json_response(text)
    candidates = (
        cleaned,
        _json_substring(cleaned, "[", "]"),
        _json_substring(cleaned, "{", "}"),
    )
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, RecursionError):
            continue
    return None


def _lookup_table_value(row: Mapping[str, object], field_name: str) -> object | None:
    if field_name in row:
        return row[field_name]
    target_key = _normalize_json_key(field_name)
    for key, value in row.items():
        if _normalize_json_key(key) == target_key:
            return value
    return None


@dataclass(frozen=True)
class ParsedTableRow:
    """按列定义保序的模型 TABLE 行。"""

    row_key: str
    values: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.row_key, str):
            raise WeaponryDomainValidationError("row_key 必须是 str")
        if not isinstance(self.values, (tuple, list)):
            raise WeaponryDomainValidationError("values 必须是有序键值序列")
        raw_values = tuple(self.values)
        values: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in raw_values:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise WeaponryDomainValidationError("values 必须包含二元键值对")
            name, value = item
            if not isinstance(name, str) or not name:
                raise WeaponryDomainValidationError("TABLE 列名必须是非空 str")
            if name in seen:
                raise WeaponryDomainValidationError(f"TABLE 行包含重复列: {name}")
            if not isinstance(value, str):
                raise WeaponryDomainValidationError("TABLE 单元格必须是 str")
            seen.add(name)
            values.append((name, value))
        object.__setattr__(self, "values", tuple(values))

    def get(self, field_name: str) -> str:
        for name, value in self.values:
            if name == field_name:
                return value
        return ""

    def to_legacy_dict(self) -> dict[str, str]:
        return {"__rowKey": self.row_key, **dict(self.values)}


def parse_table_json_rows(
    text: str,
    specification: WeaponryFieldSpecification,
    *,
    max_rows: int = MAX_TABLE_ROWS,
) -> tuple[ParsedTableRow, ...]:
    """兼容常见包装形态，按冻结列定义解析至多 ``max_rows`` 行。"""

    if not isinstance(specification, WeaponryFieldSpecification):
        raise WeaponryDomainValidationError(
            "specification 必须是 WeaponryFieldSpecification"
        )
    if specification.field_type != "TABLE":
        raise WeaponryDomainValidationError("TABLE 解析只能使用 TABLE specification")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
        raise WeaponryDomainValidationError("max_rows 必须是正整数")
    parsed = _load_table_json_response(text)
    if parsed is None:
        return ()

    row_items: list[object]
    if isinstance(parsed, list):
        row_items = parsed
    elif isinstance(parsed, dict):
        row_items = []
        found_row_container = False
        for key in ("rows", "data", "items", "result", "tableRows", "tableFieldList"):
            value = parsed.get(key)
            if isinstance(value, list):
                row_items = value
                found_row_container = True
                break
        if not found_row_container:
            row_items = [parsed]
    else:
        return ()

    column_names = tuple(item.field_name for item in specification.columns)
    result: list[ParsedTableRow] = []
    for item in row_items:
        row_key = ""
        values: list[tuple[str, str]] = []
        if isinstance(item, list):
            for index, column_name in enumerate(column_names):
                raw_value = item[index] if index < len(item) else None
                values.append((column_name, normalize_table_cell_value(raw_value)))
        elif isinstance(item, dict):
            raw_key = (
                item.get("__rowKey")
                or item.get("rowKey")
                or item.get("row_key")
                or item.get("行标识")
                or item.get("主键")
            )
            row_key = normalize_table_cell_value(raw_key)
            for column_name in column_names:
                values.append(
                    (
                        column_name,
                        normalize_table_cell_value(
                            _lookup_table_value(item, column_name)
                        ),
                    )
                )
        else:
            continue
        if any(value for _, value in values):
            result.append(ParsedTableRow(row_key=row_key, values=tuple(values)))
        if len(result) >= max_rows:
            break
    return tuple(result)


def normalize_table_row_identity(value: str) -> str:
    text = normalize_table_cell_value(value)
    return re.sub(r"\s+", "", text).lower().strip("，,;；。.")


def table_row_identity(
    row: ParsedTableRow,
    specification: WeaponryFieldSpecification,
    *,
    fallback_index: int,
) -> str:
    """优先使用显式 row key，再按强身份列或复合字段确定合并身份。

    ``类型`` 只表示宽泛分类，不能单独证明两行是同一实体。没有名称、型号、编号、代号
    或标识时，使用除“类型”列外全部非空字段组成带列名的复合身份；若只剩“类型”等
    弱信息，则按输入序号保持独立，宁可保留两行也不能静默丢失不同装备。
    """

    if not isinstance(row, ParsedTableRow):
        raise WeaponryDomainValidationError("row 必须是 ParsedTableRow")
    if isinstance(fallback_index, bool) or not isinstance(fallback_index, int) or fallback_index < 0:
        raise WeaponryDomainValidationError("fallback_index 必须是非负整数")
    explicit = normalize_table_row_identity(row.row_key)
    if explicit:
        return f"explicit:{explicit}"
    for column in specification.columns:
        if any(
            token in column.field_name
            for token in _STRONG_ROW_IDENTITY_TOKENS
        ):
            identity = normalize_table_row_identity(row.get(column.field_name))
            if identity:
                # 列名属于身份的一部分，避免“名称=X”和“型号=X”发生跨列碰撞。
                return f"strong:{column.field_name}:{identity}"

    composite_parts: list[str] = []
    for column in specification.columns:
        if _WEAK_TYPE_TOKEN in column.field_name:
            continue
        identity = normalize_table_row_identity(row.get(column.field_name))
        if identity:
            composite_parts.append(f"{column.field_name}={identity}")
    if composite_parts:
        return "composite:" + "\x1f".join(composite_parts)
    return f"row-{fallback_index}"


@dataclass(frozen=True)
class TableRowResult:
    """单份文档抽取到的一行及已经由 Translation Port 提供的单元格翻译。"""

    row: ParsedTableRow
    source_name: str
    file_name: str
    evidence_rows: tuple[str, ...]
    occurred_at: str = ""
    translations: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.row, ParsedTableRow):
            raise WeaponryDomainValidationError("row 必须是 ParsedTableRow")
        for name in ("source_name", "file_name", "occurred_at"):
            if not isinstance(getattr(self, name), str):
                raise WeaponryDomainValidationError(f"{name} 必须是 str")
        if not isinstance(self.evidence_rows, (tuple, list)) or any(
            not isinstance(item, str) for item in self.evidence_rows
        ):
            raise WeaponryDomainValidationError("evidence_rows 必须是有序文本序列")
        object.__setattr__(self, "evidence_rows", tuple(self.evidence_rows))
        if not isinstance(self.translations, (tuple, list)):
            raise WeaponryDomainValidationError("translations 必须是有序键值序列")
        translation_map: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in self.translations:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise WeaponryDomainValidationError(
                    "translations 必须包含二元键值对"
                )
            name, value = item
            if not isinstance(name, str) or not isinstance(value, str):
                raise WeaponryDomainValidationError("translations 必须是 str 键值对")
            if name not in seen:
                seen.add(name)
                translation_map.append((name, value))
        object.__setattr__(self, "translations", tuple(translation_map))

    def translation_for(self, field_name: str) -> str:
        return dict(self.translations).get(field_name, "")


@dataclass(frozen=True)
class MergedTableRow:
    """同一行身份合并后的列值和逐列来源。"""

    values: tuple[tuple[str, str], ...]
    sources: tuple[tuple[str, tuple[WeaponryAnalyseDataSource, ...]], ...]

    def __post_init__(self) -> None:
        values = _normalize_named_text_pairs(self.values, name="values")
        if not isinstance(self.sources, (tuple, list)):
            raise WeaponryDomainValidationError("sources 必须是有序键值序列")
        sources: list[tuple[str, tuple[WeaponryAnalyseDataSource, ...]]] = []
        seen: set[str] = set()
        for item in self.sources:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise WeaponryDomainValidationError("sources 必须包含二元键值对")
            field_name, field_sources = item
            if not isinstance(field_name, str) or not field_name:
                raise WeaponryDomainValidationError("sources 列名必须是非空 str")
            if field_name in seen:
                raise WeaponryDomainValidationError(
                    f"sources 包含重复列: {field_name}"
                )
            if not isinstance(field_sources, (tuple, list)) or any(
                not isinstance(source, WeaponryAnalyseDataSource)
                for source in field_sources
            ):
                raise WeaponryDomainValidationError(
                    "sources 列值只能包含 WeaponryAnalyseDataSource"
                )
            seen.add(field_name)
            sources.append((field_name, tuple(field_sources)))
        if tuple(name for name, _ in values) != tuple(name for name, _ in sources):
            raise WeaponryDomainValidationError(
                "values 与 sources 必须使用完全相同的列名和顺序"
            )
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "sources", tuple(sources))

    def value_for(self, field_name: str) -> str:
        return dict(self.values).get(field_name, "")

    def sources_for(self, field_name: str) -> tuple[WeaponryAnalyseDataSource, ...]:
        return dict(self.sources).get(field_name, ())


def _normalize_named_text_pairs(
    value: object,
    *,
    name: str,
) -> tuple[tuple[str, str], ...]:
    """校验并冻结保序的列名/文本值，防止重复列在 ``dict`` 投影时被覆盖。"""

    if not isinstance(value, (tuple, list)):
        raise WeaponryDomainValidationError(f"{name} 必须是有序键值序列")
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise WeaponryDomainValidationError(f"{name} 必须包含二元键值对")
        field_name, text = item
        if not isinstance(field_name, str) or not field_name:
            raise WeaponryDomainValidationError(f"{name} 列名必须是非空 str")
        if field_name in seen:
            raise WeaponryDomainValidationError(
                f"{name} 包含重复列: {field_name}"
            )
        if not isinstance(text, str):
            raise WeaponryDomainValidationError(f"{name} 单元格必须是 str")
        seen.add(field_name)
        result.append((field_name, text))
    return tuple(result)


def _source_identity(source: WeaponryAnalyseDataSource) -> tuple[object, ...]:
    return (source.content, source.source, source.file_name, source.rows)


def merge_table_rows(
    row_results: Iterable[TableRowResult],
    specification: WeaponryFieldSpecification,
    *,
    max_rows: int = MAX_TABLE_ROWS,
) -> tuple[MergedTableRow, ...]:
    """按稳定行身份保序合并，单元格首个非空值获胜，来源有序去重。"""

    if specification.field_type != "TABLE":
        raise WeaponryDomainValidationError("TABLE 合并只能使用 TABLE specification")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
        raise WeaponryDomainValidationError("max_rows 必须是正整数")
    order: list[str] = []
    values_by_identity: dict[str, dict[str, str]] = {}
    sources_by_identity: dict[str, dict[str, list[WeaponryAnalyseDataSource]]] = {}

    for index, row_result in enumerate(tuple(row_results)):
        if not isinstance(row_result, TableRowResult):
            raise WeaponryDomainValidationError(
                "row_results 只能包含 TableRowResult"
            )
        identity = table_row_identity(
            row_result.row,
            specification,
            fallback_index=index,
        )
        if identity not in values_by_identity:
            order.append(identity)
            values_by_identity[identity] = {}
            sources_by_identity[identity] = {}
        values = values_by_identity[identity]
        sources = sources_by_identity[identity]
        for column in specification.columns:
            value = normalize_table_cell_value(
                row_result.row.get(column.field_name)
            )
            if not value:
                continue
            values.setdefault(column.field_name, value)
            source = WeaponryAnalyseDataSource(
                content=value,
                source=row_result.source_name,
                occurred_at=row_result.occurred_at,
                file_name=row_result.file_name,
                rows=row_result.evidence_rows,
                translation=row_result.translation_for(column.field_name),
            )
            bucket = sources.setdefault(column.field_name, [])
            identity_key = _source_identity(source)
            if all(_source_identity(existing) != identity_key for existing in bucket):
                bucket.append(source)

    merged: list[MergedTableRow] = []
    for identity in order[:max_rows]:
        values = tuple(
            (column.field_name, values_by_identity[identity].get(column.field_name, ""))
            for column in specification.columns
        )
        sources = tuple(
            (
                column.field_name,
                tuple(sources_by_identity[identity].get(column.field_name, ())),
            )
            for column in specification.columns
        )
        merged.append(MergedTableRow(values=values, sources=sources))
    return tuple(merged)


def assemble_table_rows(
    merged_rows: Iterable[MergedTableRow],
    specification: WeaponryFieldSpecification,
) -> tuple[tuple[WeaponryTableCellResult, ...], ...]:
    """把合并结果投影为保留未知列模板键的二维单元格 DTO。"""

    if specification.field_type != "TABLE":
        raise WeaponryDomainValidationError("TABLE 组装只能使用 TABLE specification")
    result: list[tuple[WeaponryTableCellResult, ...]] = []
    for merged in tuple(merged_rows):
        if not isinstance(merged, MergedTableRow):
            raise WeaponryDomainValidationError(
                "merged_rows 只能包含 MergedTableRow"
            )
        row = tuple(
            WeaponryTableCellResult(
                specification=column,
                analyse_data=merged.value_for(column.field_name),
                sources=(
                    merged.sources_for(column.field_name)
                    or (WeaponryAnalyseDataSource.empty(),)
                ),
            )
            for column in specification.columns
        )
        if any(cell.analyse_data.strip() for cell in row):
            result.append(row)
    return tuple(result)


__all__ = [
    "MAX_TABLE_ROWS",
    "MergedTableRow",
    "ParsedTableRow",
    "TableRowResult",
    "assemble_table_rows",
    "merge_table_rows",
    "normalize_table_cell_value",
    "normalize_table_row_identity",
    "parse_table_json_rows",
    "table_row_identity",
]
