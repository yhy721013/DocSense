"""文件分析请求范围的默认化与领域树校验规则。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Mapping

from .architecture_tree import (
    ArchitectureTreeIndex,
    ArchitectureTreeIndexCache,
    ArchitectureTreeValidationError,
    build_architecture_tree_index,
)
from .models import (
    DEFAULT_ARCHITECTURE_OPTIONS,
    DEFAULT_COUNTRY_OPTIONS,
    DEFAULT_FORMAT_OPTIONS,
    DEFAULT_MATURITY_OPTIONS,
    DEFAULT_SECURITY_OPTIONS,
)


# 与旧链路保持同一容量，避免迁移时改变并发请求下的有限领域树索引复用语义。
_ARCHITECTURE_TREE_INDEX_CACHE = ArchitectureTreeIndexCache(capacity=4)


def _normalize_range_list(
    value: Any,
    default: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将请求范围规范为与输入、全局默认值完全隔离的快照。

    范围项包含嵌套字典和列表，浅复制只能隔离最外层列表。后续 Codec、Worker 或
    测试若修改嵌套字段，就可能污染同进程中的其他任务。因此无论使用调用方值还是
    默认值，都必须在此边界完成深复制，同时保持历史 ``list[dict]`` 返回形状不变。
    """

    if not isinstance(value, list):
        return deepcopy(default)
    items = [item for item in value if isinstance(item, dict) and item]
    return deepcopy(items if items else default)


def build_effective_analysis_ranges(
    request_params: Dict[str, Any],
) -> Dict[str, list[dict[str, Any]]]:
    """生成与历史 Analysis 链路逐字段等价的有效范围快照。"""

    return {
        "country": _normalize_range_list(
            request_params.get("country"),
            DEFAULT_COUNTRY_OPTIONS,
        ),
        # channel 必须完全由调用方提供，缺失或空范围不能回填服务端默认值。
        "channel": _normalize_range_list(request_params.get("channel"), []),
        "format": _normalize_range_list(
            request_params.get("format"),
            DEFAULT_FORMAT_OPTIONS,
        ),
        "maturity": _normalize_range_list(
            request_params.get("maturity"),
            DEFAULT_MATURITY_OPTIONS,
        ),
        "security": _normalize_range_list(
            request_params.get("security"),
            DEFAULT_SECURITY_OPTIONS,
        ),
        "architectureList": _normalize_range_list(
            request_params.get("architectureList"),
            DEFAULT_ARCHITECTURE_OPTIONS,
        ),
        "architectureStandardList": _normalize_range_list(
            request_params.get("architectureStandardList"),
            [],
        ),
    }


def validate_analysis_architecture_ranges(
    request_params: Mapping[str, Any],
) -> ArchitectureTreeIndex:
    """在任何任务或远端副作用前校验 analysis 的领域树输入。

    缺失、空值和空数组继续使用历史默认领域树，避免破坏既有调用方；只要调用方
    显式提供了非空范围，就必须完整通过结构、拓扑和资源边界校验，不能再静默过滤坏节点。
    architectureStandardList 是独立的有限树范围，不要求是主树的子集。
    """

    if not isinstance(request_params, Mapping):
        raise ArchitectureTreeValidationError("params 中的文件项必须是对象")

    raw_architecture_list = request_params.get("architectureList")
    if raw_architecture_list is None or raw_architecture_list == []:
        architecture_list: list[dict[str, Any]] = list(
            DEFAULT_ARCHITECTURE_OPTIONS
        )
    elif not isinstance(raw_architecture_list, list):
        raise ArchitectureTreeValidationError(
            "architectureList 必须是节点数组"
        )
    else:
        architecture_list = raw_architecture_list

    tree_index = _ARCHITECTURE_TREE_INDEX_CACHE.get_or_build(
        architecture_list
    )

    raw_standard_list = request_params.get("architectureStandardList")
    if raw_standard_list is None or raw_standard_list == []:
        return tree_index
    if not isinstance(raw_standard_list, list):
        raise ArchitectureTreeValidationError(
            "architectureStandardList 必须是节点数组"
        )
    try:
        # 标准范围通常很小，且不能挤占主领域树的全局 LRU 缓存。
        build_architecture_tree_index(raw_standard_list)
    except ArchitectureTreeValidationError as exc:
        message = str(exc).replace(
            "architectureList",
            "architectureStandardList",
        )
        raise ArchitectureTreeValidationError(message) from exc
    return tree_index


__all__ = (
    "_ARCHITECTURE_TREE_INDEX_CACHE",
    "_normalize_range_list",
    "build_effective_analysis_ranges",
    "validate_analysis_architecture_ranges",
)
