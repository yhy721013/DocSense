"""甲方保留字段的确定性空值策略。

这些字段属于甲方维护的分类/编号事实，``/llm/weaponry`` 不得通过术语辅助、目标
检索、模型抽取或翻译生成它们。策略只按去除首尾空白后的完整字段名匹配，禁止使用
包含、前缀或模糊匹配，以免误伤正常业务字段。
"""

from __future__ import annotations

from typing import Mapping, Sequence

from .errors import WeaponryDomainValidationError
from .models import (
    WeaponryAnalyseDataSource,
    WeaponryFieldResult,
    WeaponryFieldSpecification,
    WeaponryTableCellResult,
)


FORCED_EMPTY_FIELD_NAMES = frozenset(
    {
        "装备编号",
        "一级分类",
        "二级分类",
        "三级分类",
        "四级分类",
    }
)


def is_forced_empty_field_name(value: object) -> bool:
    """判断字段名是否命中甲方保留字段；非字符串一律不匹配。"""

    return isinstance(value, str) and value.strip() in FORCED_EMPTY_FIELD_NAMES


def external_processing_specification(
    specification: WeaponryFieldSpecification,
) -> WeaponryFieldSpecification | None:
    """返回允许进入外部处理链的字段规格。

    ``None`` 表示整个字段都必须确定性置空。INPUT 只检查顶层字段名；TABLE 只过滤列
    名，并同步过滤内部模板，确保 Guidance、Query、Prompt、Extraction Request 和解析
    使用同一组普通列。公开结果仍必须使用调用方持有的原始 ``specification`` 组装。
    """

    if not isinstance(specification, WeaponryFieldSpecification):
        raise WeaponryDomainValidationError(
            "specification 必须是 WeaponryFieldSpecification"
        )
    if specification.field_type == "INPUT":
        return (
            None
            if is_forced_empty_field_name(specification.field_name)
            else specification
        )

    allowed_columns = tuple(
        column
        for column in specification.columns
        if not is_forced_empty_field_name(column.field_name)
    )
    if len(allowed_columns) == len(specification.columns):
        return specification
    if not allowed_columns:
        return None

    template = specification.template.to_dict()
    raw_rows = template.get("tableFieldList")
    if not isinstance(raw_rows, Sequence) or isinstance(
        raw_rows,
        (str, bytes, bytearray),
    ):
        raise WeaponryDomainValidationError(
            "TABLE field template 缺少有效 tableFieldList"
        )
    filtered_rows: list[list[object]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Sequence) or isinstance(
            raw_row,
            (str, bytes, bytearray),
        ):
            raise WeaponryDomainValidationError(
                "TABLE field template 行必须是有序数组"
            )
        filtered_row = [
            raw_cell
            for raw_cell in raw_row
            if isinstance(raw_cell, Mapping)
            and not is_forced_empty_field_name(raw_cell.get("fieldName"))
        ]
        if filtered_row:
            filtered_rows.append(filtered_row)
    template["tableFieldList"] = filtered_rows
    narrowed = WeaponryFieldSpecification.from_mapping(template)
    if narrowed.columns != allowed_columns:
        raise WeaponryDomainValidationError(
            "TABLE 外部处理规格与原始普通列不一致"
        )
    return narrowed


def build_forced_empty_result(
    specification: WeaponryFieldSpecification,
) -> WeaponryFieldResult:
    """为完全受限的 INPUT/TABLE 构造标准空值公开结果。"""

    if not isinstance(specification, WeaponryFieldSpecification):
        raise WeaponryDomainValidationError(
            "specification 必须是 WeaponryFieldSpecification"
        )
    if external_processing_specification(specification) is not None:
        raise WeaponryDomainValidationError("字段并非完全受限，不能强制构造空结果")
    if specification.field_type == "INPUT":
        return WeaponryFieldResult(specification=specification)
    return build_table_empty_fallback_result(specification)


def build_table_empty_fallback_result(
    specification: WeaponryFieldSpecification,
) -> WeaponryFieldResult:
    """为含保留列且没有有效抽取行的 TABLE 构造一行完整空结果。

    保留列使用标准空来源占位；普通列保持既有 TABLE 空单元格语义，即空值和空来源
    数组。该函数只创建当前 execution 的领域结果，不参与历史 Callback 解码。
    """

    if not isinstance(specification, WeaponryFieldSpecification):
        raise WeaponryDomainValidationError(
            "specification 必须是 WeaponryFieldSpecification"
        )
    if specification.field_type != "TABLE":
        raise WeaponryDomainValidationError("TABLE 空结果只能使用 TABLE specification")
    if not any(
        is_forced_empty_field_name(column.field_name)
        for column in specification.columns
    ):
        raise WeaponryDomainValidationError("TABLE 不含保留列，不能构造保留字段空结果")
    row = tuple(
        WeaponryTableCellResult(
            specification=column,
            analyse_data="",
            sources=(
                (WeaponryAnalyseDataSource.empty(),)
                if is_forced_empty_field_name(column.field_name)
                else ()
            ),
        )
        for column in specification.columns
    )
    return WeaponryFieldResult(
        specification=specification,
        table_rows=(row,),
    )


__all__ = [
    "FORCED_EMPTY_FIELD_NAMES",
    "build_forced_empty_result",
    "build_table_empty_fallback_result",
    "external_processing_specification",
    "is_forced_empty_field_name",
]
