"""文件分析结果映射与字段规范化纯规则。

本模块从旧 Analysis 服务迁移后只处理调用方传入的字典与文本，不写入日志、不触发翻译、
文件读取、回调或其他外部副作用。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

from .models import (
    ARCHITECTURE_FALLBACK_ID,
    DATA_STANDARD_FIELD_ALIASES,
    DATA_STANDARD_LEAF_NAMES,
    SOURCE_SCORE_VALUES,
    WEAPONRY_DETAIL_CATEGORY_SUFFIXES,
)
from .prompts import (
    ANALYSIS_ENUM_FIELD_MAX_ITEMS,
    ANALYSIS_ENUM_ITEM_MAX_CHARS,
    ANALYSIS_KEYWORD_MAX_CHARS,
    ANALYSIS_KEYWORD_MAX_COUNT,
    ANALYSIS_KEYWORD_MIN_COUNT,
    UNKNOWN_SOURCE_VALUE,
)
from .architecture_tree import ArchitectureTreeValidationError
from .ranges import (
    _ARCHITECTURE_TREE_INDEX_CACHE,
    build_effective_analysis_ranges,
)


# 保留旧模块内部别名，保证迁移期字段截断和关键词下限算法不变。
MIN_KEYWORD_COUNT = ANALYSIS_KEYWORD_MIN_COUNT
MAX_KEYWORD_COUNT = ANALYSIS_KEYWORD_MAX_COUNT
MAX_KEYWORD_LENGTH = ANALYSIS_KEYWORD_MAX_CHARS


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _as_business_original_file_name(value: Any) -> str:
    """读取业务原始文件名，并保留请求字符串的原始值。

    ``originalFileName`` 是面向甲方回调展示的业务字段，不是文件系统路径或内部键。
    因此只能用去首尾空白后的结果判断它是否为空，不能把 strip 后的文本回写为值；
    否则会违反“回调 source 严格返回请求 originalFileName 原值”的约定。
    """
    if value is None:
        return ""
    original_name = value if isinstance(value, str) else str(value)
    return original_name if original_name.strip() else ""


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_match_text(value: Any) -> str:
    text = _as_text(value)
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", "", normalized)


def _contains_gjb_standard_reference(*values: Any) -> bool:
    text = "\n".join(_as_text(value) for value in values if value not in (None, "", [], {}))
    if not text:
        return False
    normalized = unicodedata.normalize("NFKC", text)
    lowered = normalized.casefold()
    return (
        "国军标" in normalized
        or "国家军用标准" in normalized
        or re.search(r"(?<![a-z0-9])gjb(?=\s|[-_/]|\d|$)", lowered) is not None
    )


def _is_data_standard_candidate(item: Dict[str, Any]) -> bool:
    """判断候选节点是否属于数据标准分支。

    现有接口以节点 ``name`` / ``pathName`` 中是否包含“数据标准”标识该分支。这里延续
    既有口径，并在下游结合 ``parentId`` 扩展到名称中未重复写入“数据标准”的子孙节点。
    """
    names = (_as_text(item.get("name")), _as_text(item.get("pathName")))
    return any("数据标准" in name for name in names)


def _architecture_candidate_topology(
        architecture_list: Iterable[Dict[str, Any]],
) -> tuple[list[tuple[int, Dict[str, Any]]], set[int]]:
    """构建请求候选的有限树拓扑，并保留前端传入顺序。

    GJB 兜底虽然已经改为定向选择“通用要求”，但其他拓扑判断和审计仍需要保持调用方
    候选顺序。叶子关系仅根据本次请求中明确给出的 ``parentId`` 识别：若调用方没有提供
    某个子节点，服务端不会猜测完整树结构，也不会因此提前拒绝该请求。
    """
    nodes: list[tuple[int, Dict[str, Any]]] = []
    candidate_ids: set[int] = set()
    for item in architecture_list:
        if not isinstance(item, dict):
            continue
        item_id = _coerce_int(item.get("id"))
        if item_id is None or item_id < 1:
            continue
        nodes.append((item_id, item))
        candidate_ids.add(item_id)

    parent_ids = {
        parent_id
        for _item_id, item in nodes
        if (parent_id := _coerce_int(item.get("parentId"))) in candidate_ids
    }
    return nodes, parent_ids


def _data_standard_candidate_ids(
        nodes: Iterable[tuple[int, Dict[str, Any]]],
) -> set[int]:
    """返回数据标准节点及其在本次候选树中可追溯到的子孙节点 ID。"""
    ordered_nodes = list(nodes)
    standard_ids = {
        item_id
        for item_id, item in ordered_nodes
        if _is_data_standard_candidate(item)
    }

    # 子节点可能只填写自身名称、未重复填写“数据标准”。通过 parentId 向下扩展可避免把
    # 这类合法叶子遗漏；循环仅在集合新增节点时继续，能安全处理异常环状数据。
    has_new_node = True
    while has_new_node:
        has_new_node = False
        for item_id, item in ordered_nodes:
            parent_id = _coerce_int(item.get("parentId"))
            if item_id not in standard_ids and parent_id in standard_ids:
                standard_ids.add(item_id)
                has_new_node = True
    return standard_ids


def _ordered_data_standard_leaf_ids(
        architecture_list: Iterable[Dict[str, Any]],
) -> list[int]:
    """按请求顺序返回数据标准分支中的叶子节点 ID。

    普通领域仍允许返回父节点；此函数只服务于数据标准的特殊规则。返回顺序仅用于
    构造稳定候选，不能再作为无法判定时的语义兜底依据。
    """
    nodes, parent_ids = _architecture_candidate_topology(architecture_list)
    standard_ids = _data_standard_candidate_ids(nodes)
    leaf_ids: list[int] = []
    seen_ids: set[int] = set()
    for item_id, item in nodes:
        leaf_name = _as_text(item.get("name"))
        if leaf_name.endswith("标准"):
            leaf_name = leaf_name[:-2].strip()
        if (
                item_id in standard_ids
                and item_id not in parent_ids
                and item_id not in seen_ids
                and leaf_name in DATA_STANDARD_LEAF_NAMES
        ):
            leaf_ids.append(item_id)
            seen_ids.add(item_id)
    return leaf_ids


def _general_data_standard_leaf_id(
        architecture_list: Iterable[Dict[str, Any]],
) -> int | None:
    """返回数据标准分支中语义明确的“通用要求”叶子。

    同时兼容节点名“通用要求标准”；不得因请求顺序变化而落到其他专业叶子。
    """

    nodes, parent_ids = _architecture_candidate_topology(architecture_list)
    standard_ids = _data_standard_candidate_ids(nodes)
    for item_id, item in nodes:
        leaf_name = _as_text(item.get("name"))
        if leaf_name.endswith("标准"):
            leaf_name = leaf_name[:-2].strip()
        if (
                item_id in standard_ids
                and item_id not in parent_ids
                and leaf_name == "通用要求"
        ):
            return item_id
    return None


def _first_data_standard_leaf_id(
        architecture_list: Iterable[Dict[str, Any]],
) -> int | None:
    """兼容旧内部导入；现在定向返回“通用要求”，不再返回首叶。"""

    return _general_data_standard_leaf_id(architecture_list)


def _is_data_standard_parent_id(
        architecture_id: int,
        architecture_list: Iterable[Dict[str, Any]],
) -> bool:
    """判断指定候选是否为本次请求中数据标准分支的父节点。"""
    nodes, parent_ids = _architecture_candidate_topology(architecture_list)
    standard_ids = _data_standard_candidate_ids(nodes)
    return architecture_id in standard_ids and architecture_id in parent_ids


def _match_data_standard_architecture_id(
        architecture_list: Iterable[Dict[str, Any]],
        *context_values: Any,
) -> int | None:
    """命中 GJB 线索后，定向选择数据标准分支的“通用要求”叶子。"""
    if not _contains_gjb_standard_reference(*context_values):
        return None
    return _general_data_standard_leaf_id(architecture_list)


def _architecture_id_set(items: Iterable[Dict[str, Any]]) -> set[int]:
    ids = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = _coerce_int(item.get("id"))
        if item_id is not None:
            ids.add(item_id)
    return ids


def _path_ids(value: Any) -> set[int]:
    text = _as_text(value)
    if not text:
        return set()
    return {int(match) for match in re.findall(r"\d+", text)}


def _architecture_ancestor_ids(architecture_id: int, architecture_list: Iterable[Dict[str, Any]]) -> set[int]:
    items_by_id: dict[int, Dict[str, Any]] = {}
    for item in architecture_list:
        if not isinstance(item, dict):
            continue
        item_id = _coerce_int(item.get("id"))
        if item_id is not None:
            items_by_id[item_id] = item

    ancestors = set()
    current_id = architecture_id
    visited = {architecture_id}
    while True:
        item = items_by_id.get(current_id)
        if not item:
            break
        parent_id = _coerce_int(item.get("parentId"))
        if parent_id is None or parent_id in visited:
            break
        ancestors.add(parent_id)
        visited.add(parent_id)
        current_id = parent_id
    return ancestors


def resolve_storage_architecture_id(
        result_architecture_id: Any,
        architecture_list: Iterable[Dict[str, Any]],
) -> int | None:
    """将装备明细子分类解析为用于知识库存储的装备级分类 ID。"""
    resolved_id = _coerce_int(result_architecture_id)
    if resolved_id is None:
        return None

    candidate_items = [item for item in architecture_list if isinstance(item, dict)]
    items_by_id = {
        item_id: item
        for item in candidate_items
        if (item_id := _coerce_int(item.get("id"))) is not None
    }
    result_item = items_by_id.get(resolved_id)
    if not result_item:
        return resolved_id

    result_name = _as_text(result_item.get("name"))
    weaponry_name, separator, suffix = result_name.rpartition("-")
    weaponry_name = weaponry_name.strip()
    suffix = suffix.strip()
    if not separator or not weaponry_name or suffix not in WEAPONRY_DETAIL_CATEGORY_SUFFIXES:
        return resolved_id

    parent_id = _coerce_int(result_item.get("parentId"))
    if parent_id is None:
        return resolved_id

    # architectureList 可能只传当前子分类。此时 parentId 是唯一可用的装备级标识。
    if parent_id not in items_by_id:
        return parent_id

    current_id = parent_id
    visited = {resolved_id}
    while current_id not in visited:
        visited.add(current_id)
        current_item = items_by_id.get(current_id)
        if not current_item:
            break
        if _as_text(current_item.get("name")) == weaponry_name:
            return current_id
        next_parent_id = _coerce_int(current_item.get("parentId"))
        if next_parent_id is None:
            break
        current_id = next_parent_id

    # 父链字段不完整时，使用 path 中明确列出的祖先节点做一次保守补充匹配。
    path_ids = [_coerce_int(value) for value in re.findall(r"\d+", _as_text(result_item.get("path")))]
    matched_path_ids = [
        path_id
        for path_id in path_ids
        if path_id != resolved_id
        and path_id in items_by_id
        and _as_text(items_by_id[path_id].get("name")) == weaponry_name
    ]
    if len(matched_path_ids) == 1:
        return matched_path_ids[0]

    return resolved_id


def _architecture_name_by_id(architecture_id: Any, architecture_list: Iterable[Dict[str, Any]]) -> str:
    resolved_id = _coerce_int(architecture_id)
    if resolved_id is None:
        return ""
    for item in architecture_list:
        if isinstance(item, dict) and _coerce_int(item.get("id")) == resolved_id:
            return _as_text(item.get("name"))
    return ""


def _is_architecture_in_standard_range(
        architecture_id: Any,
        architecture_list: Iterable[Dict[str, Any]],
        architecture_standard_list: Iterable[Dict[str, Any]],
) -> bool:
    resolved_id = _coerce_int(architecture_id)
    if resolved_id is None:
        return False

    standard_ids = _architecture_id_set(architecture_standard_list)
    if not standard_ids:
        return False
    if resolved_id in standard_ids:
        return True

    candidate_items = [item for item in architecture_list if isinstance(item, dict)]
    target_item = None
    for item in candidate_items:
        if _coerce_int(item.get("id")) == resolved_id:
            target_item = item
            break

    if target_item:
        if standard_ids.intersection(_path_ids(target_item.get("path"))):
            return True
        if standard_ids.intersection(_architecture_ancestor_ids(resolved_id, candidate_items)):
            return True

    return False


def _normalize_source_score(value: Any) -> int:
    try:
        numeric_score = float(value)
    except (TypeError, ValueError):
        return 55
    if not numeric_score.is_integer():
        return 55
    score = int(numeric_score)
    if score not in SOURCE_SCORE_VALUES:
        return 55
    return score


def _match_option_value(value: Any, options: Iterable[Dict[str, Any]]) -> str:
    target = _scalar_text(value)
    if not target:
        return ""
    normalized_target = _normalize_match_text(target)
    for item in options:
        if not isinstance(item, dict):
            continue
        value_text = _as_text(item.get("value"))
        key_text = _as_text(item.get("key"))
        if target in {value_text, key_text}:
            return value_text
        if normalized_target and normalized_target in {
            _normalize_match_text(value_text),
            _normalize_match_text(key_text),
        }:
            return value_text
    return ""


def _default_security_value(options: Iterable[Dict[str, Any]]) -> str:
    matched = _match_option_value("公开", options)
    if matched:
        return matched
    for item in options:
        if not isinstance(item, dict):
            continue
        candidate = _as_text(item.get("value"))
        if candidate:
            return candidate
    return "公开"


def _match_architecture_id(parsed_result: Dict[str, Any], architecture_list: Iterable[Dict[str, Any]]) -> int:
    def _fallback(reason: str, detail: Any = None) -> int:
        return ARCHITECTURE_FALLBACK_ID

    candidate_items = [item for item in architecture_list if isinstance(item, dict)]
    candidate_ids = set()
    for item in candidate_items:
        try:
            raw_candidate_id = item.get("id")
            if raw_candidate_id in (None, ""):
                continue
            candidate_ids.add(int(raw_candidate_id))
        except (TypeError, ValueError):
            continue

    if len(candidate_ids) == 1:
        return next(iter(candidate_ids))

    raw_id = _first_non_empty_value(parsed_result, "architectureId", "领域体系 ID")
    if raw_id is not None:
        try:
            matched_id = int(raw_id)
            if matched_id in candidate_ids:
                return matched_id
            return _fallback(
                "raw_id_out_of_range",
                {"raw_id": matched_id, "candidate_count": len(candidate_ids)},
            )
        except (TypeError, ValueError):
            return _fallback("raw_id_invalid", raw_id)

    architecture_obj = _first_non_empty_value(parsed_result, "领域体系")
    if isinstance(architecture_obj, dict):
        raw_arch_id = architecture_obj.get("id")
        if raw_arch_id is not None:
            try:
                matched_id = int(raw_arch_id)
                if matched_id in candidate_ids:
                    return matched_id
                return _fallback(
                    "nested_id_out_of_range",
                    {"raw_id": matched_id, "candidate_count": len(candidate_ids)},
                )
            except (TypeError, ValueError):
                return _fallback("nested_id_invalid", raw_arch_id)

    name_candidates = []
    for value in (
            _first_non_empty_value(parsed_result, "architectureName", "architecture", "领域体系名称"),
            architecture_obj,
    ):
        if value in (None, "", [], {}):
            continue
        if isinstance(value, dict):
            for key in ("name", "pathName", "value", "text", "label", "content"):
                candidate = _as_text(value.get(key))
                if candidate:
                    name_candidates.append(candidate)
        else:
            candidate = _scalar_text(value)
            if candidate:
                name_candidates.append(candidate)

    if not name_candidates:
        return _fallback("no_candidate_name")

    for target_name in name_candidates:
        normalized_targets = {target_name}
        if "/" in target_name:
            normalized_targets.add(target_name.split("/")[-1].strip())

        for item in candidate_items:
            item_name = _as_text(item.get("name"))
            item_path_name = _as_text(item.get("pathName"))
            item_path_tail = item_path_name.split("/")[-1].strip() if item_path_name and "/" in item_path_name else ""
            if normalized_targets.intersection({item_name, item_path_name, item_path_tail}):
                try:
                    return int(item.get("id") or 0)
                except (TypeError, ValueError):
                    return _fallback("matched_item_id_invalid", item.get("id"))

    return _fallback(
        "name_not_matched",
        {"input_candidates": name_candidates[:3], "candidate_count": len(candidate_items)},
    )


def _first_non_empty_value(container: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key not in container:
            continue
        value = container.get(key)
        if value in (None, "", [], {}):
            continue
        return value
    return None


def _scalar_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("value", "name", "text", "label", "content"):
            candidate = _as_text(value.get(key))
            if candidate:
                return candidate
        for candidate in value.values():
            text = _as_text(candidate)
            if text:
                return text
        return ""
    return _as_text(value)


def _resolve_field(parsed_result: Dict[str, Any], file_item: Dict[str, Any], *aliases: str) -> str:
    nested = _first_non_empty_value(file_item, *aliases)
    if nested not in (None, "", [], {}):
        return _scalar_text(nested)
    top_level = _first_non_empty_value(parsed_result, *aliases)
    if top_level not in (None, "", [], {}):
        return _scalar_text(top_level)
    return ""


def _split_delimited_items(raw_value: Any) -> list[str]:
    """把模型可能返回的数组或多种分隔字符串拆成保持顺序的条目。"""
    if raw_value in (None, "", [], {}):
        return []
    if isinstance(raw_value, (list, tuple)):
        return [_scalar_text(item) for item in raw_value]
    if isinstance(raw_value, str):
        text = raw_value.strip()
        return re.split(r"[,，、;；|\n\r]+", text) if text else []
    return [_scalar_text(raw_value)]


def _sanitize_delimited_items(
        raw_value: Any,
        *,
        field_name: str,
        max_items: int,
        max_item_chars: int,
) -> list[str]:
    """规范化字符串枚举字段，不做任何业务语义补全。"""
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in _split_delimited_items(raw_value):
        item = re.sub(
            r"^\s*(?:[-*•]|\d+[.、])\s*",
            "",
            _as_text(part),
        ).strip().strip("\"'“”‘’").strip()
        if not item:
            continue
        if len(item) > max_item_chars:
            continue
        dedupe_key = unicodedata.normalize("NFKC", item).casefold()
        if not item or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        cleaned.append(item)
        if len(cleaned) >= max_items:
            break
    return cleaned


def _sanitize_keyword_items(raw_value: Any) -> list[str]:
    return _sanitize_delimited_items(
        raw_value,
        field_name="keyword",
        # 先保留有限数量的模型候选供分类去重和正文证据筛选，最终输出仍限制为 10 项。
        max_items=MAX_KEYWORD_COUNT * 3,
        max_item_chars=MAX_KEYWORD_LENGTH,
    )


def _sanitize_keywords(raw_value: Any) -> str:
    """保留给既有调用方的 keyword 字符串规范化入口。"""
    return ", ".join(_sanitize_keyword_items(raw_value)[:MAX_KEYWORD_COUNT])


def _architecture_path_keyword_names(
        tree_index: ArchitectureTreeIndex,
        architecture_id: int,
) -> list[str]:
    """沿已验证父链返回最多四个叶到根的节点原名。"""
    if tree_index.get(architecture_id) is None:
        return []
    node_ids = (
        architecture_id,
        *reversed(tree_index.ancestors_by_id.get(architecture_id, ())),
    )
    return [tree_index.require(node_id).name for node_id in node_ids[:4]]


def _bounded_unique_exact_items(
        items: Iterable[str],
        *,
        field_name: str,
        max_items: int,
        max_item_chars: int,
) -> list[str]:
    """保留服务端可信原值，仅执行空值、长度、去重和数量门禁。"""
    result: list[str] = []
    seen: set[str] = set()
    for raw_item in items:
        item = _as_text(raw_item)
        if not item:
            continue
        if len(item) > max_item_chars:
            continue
        dedupe_key = unicodedata.normalize("NFKC", item).casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        result.append(item)
        if len(result) >= max_items:
            break
    return result


def _normalize_evidence_text(value: Any) -> str:
    """生成仅用于证据匹配和去重的宽松文本，不改写正式回调值。"""
    normalized = unicodedata.normalize("NFKC", _as_text(value)).casefold()
    compact = "".join(
        char
        for char in normalized
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )
    return compact or normalized.strip()


def _has_normalized_text_evidence(value: Any, evidence_text: str) -> bool:
    normalized_value = _normalize_evidence_text(value)
    return bool(
        normalized_value
        and normalized_value in _normalize_evidence_text(evidence_text)
    )


def _classification_keyword_items(
        architecture_id: Any,
        candidates: Iterable[Dict[str, Any]],
) -> list[str]:
    """从已验证 parentId 祖先链提取最多四个由具体到一般的节点原名。"""
    normalized_id = _coerce_int(architecture_id)
    if normalized_id is None:
        return []
    candidate_list = [item for item in candidates if isinstance(item, dict)]
    try:
        tree_index = _ARCHITECTURE_TREE_INDEX_CACHE.get_or_build(candidate_list)
        node_names = _architecture_path_keyword_names(tree_index, normalized_id)
    except ArchitectureTreeValidationError:
        node_names = [
            _as_text(item.get("name"))
            for item in candidate_list
            if _coerce_int(item.get("id")) == normalized_id
        ][:1]
    return _bounded_unique_exact_items(
        node_names,
        field_name="keyword.classification",
        max_items=4,
        max_item_chars=MAX_KEYWORD_LENGTH,
    )


def _compose_analysis_keywords(
        raw_keyword_items: Sequence[str],
        *,
        summary: str,
        original_text: str,
        architecture_id: Any,
        candidates: Iterable[Dict[str, Any]],
) -> str:
    """按“分类路径在前、内容关键词在后”组成正式 keyword 字符串。"""
    classification_items = _classification_keyword_items(architecture_id, candidates)
    classification_keys = {
        _normalize_evidence_text(item)
        for item in classification_items
    }
    summary_items: list[str] = []
    source_fallback_items: list[str] = []
    content_seen: set[str] = set()
    summary_limit = min(7, max(0, MAX_KEYWORD_COUNT - len(classification_items)))
    for item in raw_keyword_items:
        dedupe_key = _normalize_evidence_text(item)
        if not dedupe_key or dedupe_key in classification_keys or dedupe_key in content_seen:
            continue
        if _has_normalized_text_evidence(item, summary):
            content_seen.add(dedupe_key)
            summary_items.append(item)
        elif _has_normalized_text_evidence(item, original_text):
            content_seen.add(dedupe_key)
            source_fallback_items.append(item)

    accepted_content_items = summary_items[:summary_limit]
    minimum_content_count = max(
        2,
        MIN_KEYWORD_COUNT - len(classification_items),
    )
    fallback_slots = max(
        0,
        min(
            summary_limit - len(accepted_content_items),
            minimum_content_count - len(accepted_content_items),
        ),
    )
    if fallback_slots:
        accepted_content_items.extend(source_fallback_items[:fallback_slots])
    return ", ".join(classification_items + accepted_content_items)


def _related_technology_evidence_map(raw_value: Any) -> dict[str, str]:
    if isinstance(raw_value, Mapping):
        raw_entries: Sequence[Any] = [raw_value]
    elif isinstance(raw_value, (list, tuple)):
        raw_entries = raw_value
    else:
        return {}

    evidence_by_name: dict[str, str] = {}
    for entry in raw_entries:
        if not isinstance(entry, Mapping):
            continue
        name = _as_text(
            entry.get("nameZh")
            or entry.get("name")
            or entry.get("中文名称")
        )
        source_term = _as_text(
            entry.get("sourceTerm")
            or entry.get("evidence")
            or entry.get("原文证据")
        )
        normalized_name = _normalize_evidence_text(name)
        if normalized_name and source_term and normalized_name not in evidence_by_name:
            evidence_by_name[normalized_name] = source_term
    return evidence_by_name


@dataclass(frozen=True)
class _RelatedTechnologySanitization:
    """所属技术规范化结果及其无副作用诊断事实。

    Domain 不能直接写日志，但 Service/Application 仍需要准确区分非中文、证据缺失和
    数量截断。将原因作为不可变值返回，可以避免兼容层根据最终数量反推并误报原因。
    """

    value: str
    chinese_count: int = 0
    accepted_before_limit: int = 0
    accepted_count: int = 0
    non_chinese_count: int = 0
    missing_evidence_count: int = 0
    overflow_count: int = 0
    evidence_text_present: bool = False


def _sanitize_related_technologies_with_diagnostics(
        raw_value: Any,
        *,
        raw_evidence: Any = None,
        original_text: str,
) -> _RelatedTechnologySanitization:
    """规范化所属技术，并返回供外层记录日志的精确诊断事实。"""

    raw_items = _split_delimited_items(raw_value)
    items = _sanitize_delimited_items(
        raw_items,
        field_name="relatedTechnology",
        # 先完成正文证据过滤，再执行最多 10 项的业务上限，避免前置幻觉项挤掉后续有效项。
        max_items=max(ANALYSIS_ENUM_FIELD_MAX_ITEMS, len(raw_items)),
        max_item_chars=ANALYSIS_ENUM_ITEM_MAX_CHARS,
    )
    if not items:
        return _RelatedTechnologySanitization(value="")

    chinese_items = [
        item for item in items if re.search(r"[\u3400-\u9fff]", item)
    ]
    non_chinese_count = len(items) - len(chinese_items)
    if not chinese_items:
        return _RelatedTechnologySanitization(
            value="",
            non_chinese_count=non_chinese_count,
        )

    evidence_text = _as_text(original_text)
    if not evidence_text:
        accepted = chinese_items[:ANALYSIS_ENUM_FIELD_MAX_ITEMS]
        return _RelatedTechnologySanitization(
            value=", ".join(accepted),
            chinese_count=len(chinese_items),
            accepted_before_limit=len(chinese_items),
            accepted_count=len(accepted),
            non_chinese_count=non_chinese_count,
            overflow_count=max(
                0,
                len(chinese_items) - ANALYSIS_ENUM_FIELD_MAX_ITEMS,
            ),
        )

    evidence_by_name = _related_technology_evidence_map(raw_evidence)
    evidence_items: list[str] = []
    for item in chinese_items:
        if _has_normalized_text_evidence(item, evidence_text):
            evidence_items.append(item)
            continue
        source_term = evidence_by_name.get(_normalize_evidence_text(item), "")
        if source_term and _has_normalized_text_evidence(
                source_term,
                evidence_text,
        ):
            evidence_items.append(item)

    accepted = evidence_items[:ANALYSIS_ENUM_FIELD_MAX_ITEMS]
    return _RelatedTechnologySanitization(
        value=", ".join(accepted),
        chinese_count=len(chinese_items),
        accepted_before_limit=len(evidence_items),
        accepted_count=len(accepted),
        non_chinese_count=non_chinese_count,
        missing_evidence_count=len(chinese_items) - len(evidence_items),
        overflow_count=max(
            0,
            len(evidence_items) - ANALYSIS_ENUM_FIELD_MAX_ITEMS,
        ),
        evidence_text_present=True,
    )


def _sanitize_related_technologies(
        raw_value: Any,
        *,
        raw_evidence: Any = None,
        original_text: str,
) -> str:
    return _sanitize_related_technologies_with_diagnostics(
        raw_value,
        raw_evidence=raw_evidence,
        original_text=original_text,
    ).value


def _extract_original_link(original_text: str) -> str:
    match = re.search(r"https?://\S+", original_text)
    return match.group(0) if match else ""


def _extract_date(original_text: str) -> str:
    match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", original_text)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    month_map = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    match = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", original_text)
    if not match:
        return ""
    day, month_name, year = match.groups()
    month = month_map.get(month_name.lower())
    if not month:
        return ""
    return f"{year}-{month:02d}-{int(day):02d}"


def _format_iso_date(year: Any, month: Any, day: Any) -> str:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except (TypeError, ValueError):
        return ""


def _normalize_date_field(value: Any) -> str:
    text = _scalar_text(value)
    if not text:
        return ""

    match = re.search(r"(\d{4})[-./年](\d{1,2})[-./月](\d{1,2})日?", text)
    if match:
        return _format_iso_date(*match.groups())

    match = re.search(r"\b(\d{4})(\d{2})(\d{2})\b", text)
    if match:
        return _format_iso_date(*match.groups())

    return _extract_date(text)


def _infer_language(original_text: str) -> str:
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", original_text))
    has_latin = bool(re.search(r"[A-Za-z]", original_text))
    if has_cjk and has_latin:
        return "中英双语"
    if has_cjk:
        return "中文"
    if has_latin:
        return "英文"
    return ""


def _match_option_value_from_text(options: Iterable[Dict[str, Any]], original_text: str) -> str:
    normalized_original_text = _normalize_match_text(original_text)
    if not normalized_original_text:
        return ""
    for item in options:
        if not isinstance(item, dict):
            continue
        candidate = _as_text(item.get("value"))
        if candidate and _normalize_match_text(candidate) in normalized_original_text:
            return _as_text(item.get("value"))
    return ""


def _opening_text(original_text: str, *, max_chars: int = 2000, max_lines: int = 80) -> str:
    if not original_text:
        return ""
    collected: list[str] = []
    total_chars = 0
    for raw_line in original_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        collected.append(line)
        total_chars += len(line)
        if total_chars >= max_chars or len(collected) >= max_lines:
            break
    if collected:
        return "\n".join(collected)[:max_chars]
    return original_text[:max_chars]


def _opening_identity_evidence_text(
    original_text: str,
    original_name: str,
    *,
    max_chars: int = 2000,
    max_lines: int = 80,
) -> str:
    """剔除转换器可能回显的原文件名，避免把同一信号当成双重证据。"""

    opening = _opening_text(
        original_text,
        max_chars=max_chars,
        max_lines=max_lines,
    )
    if not opening:
        return ""
    def normalize_echo(value: Any) -> str:
        return re.sub(
            r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+",
            "",
            unicodedata.normalize("NFKC", _as_text(value)).casefold(),
        )

    basename = Path(original_name.replace("\\", "/")).name
    echo_values = {
        normalized
        for value in (basename, Path(basename).stem)
        if (normalized := normalize_echo(value))
    }
    if not echo_values:
        return opening

    label_prefixes = (
        "filename",
        "originalfilename",
        "documentname",
        "title",
        "文件名",
        "原文件名",
        "文档名",
        "标题",
    )
    evidence_lines: list[str] = []
    for line in opening.splitlines():
        normalized_line = normalize_echo(line)
        labelled_echo = any(
            normalized_line == f"{prefix}{echo_value}"
            for prefix in label_prefixes
            for echo_value in echo_values
        )
        path_echo = (
            ("file://" in line.casefold() or line.lstrip().startswith(("/", "\\")))
            and any(echo_value in normalized_line for echo_value in echo_values)
        )
        if normalized_line in echo_values or labelled_echo or path_echo:
            continue
        evidence_lines.append(line)
    return "\n".join(evidence_lines)[:max_chars]


def _extract_security_from_opening_text(original_text: str, options: Iterable[Dict[str, Any]]) -> str:
    opening = _opening_text(original_text)
    if not opening:
        return ""
    lines = [line.strip() for line in opening.splitlines() if line.strip()]
    security_label_pattern = re.compile(r"(密级|密别|秘密等级|保密级别|文件密级|资料密级|密级程度|保密期限)")
    for line in lines:
        if not security_label_pattern.search(line):
            continue
        matched = _match_option_value_from_text(options, line)
        if matched:
            return matched
    for line in lines[:30]:
        if len(line) > 40:
            continue
        matched = _match_option_value_from_text(options, line)
        if matched:
            return matched
    return ""


def _extract_title(original_text: str) -> str:
    lines = [line.strip() for line in original_text.splitlines()]
    for index, line in enumerate(lines):
        if line == "标题":
            for candidate in lines[index + 1:]:
                if candidate:
                    return candidate
    for line in lines:
        if line and line not in {"内容", "原文链接", "原文"} and not line.startswith("http"):
            return line
    return ""


def _extract_source(original_text: str) -> str:
    match = re.search(r"【([^】]+?)\d{4}年\d{1,2}月\d{1,2}日", original_text)
    if match:
        return match.group(1).strip()
    return ""


def _extract_labeled_value(original_text: str, labels: Iterable[str]) -> str:
    if not original_text:
        return ""
    for label in labels:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", label):
            continue
        match = re.search(rf"{re.escape(label)}\s*[:：]\s*([^\r\n]+)", original_text)
        if match:
            return match.group(1).strip(" \t;；。")
    return ""


def _extract_gjb_number(original_text: str) -> str:
    match = re.search(r"\b(GJB\s*[0-9A-Za-z]+(?:[-—–][0-9A-Za-z]+)*)\b", original_text)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _extract_standard_name(original_text: str) -> str:
    labeled = _extract_labeled_value(original_text, DATA_STANDARD_FIELD_ALIASES["militaryName"])
    if labeled:
        return labeled
    for line in original_text.splitlines():
        candidate = line.strip()
        if candidate and _contains_gjb_standard_reference(candidate):
            return candidate
    return ""


def _extract_data_standard_fields(
        parsed_result: Dict[str, Any],
        file_item: Dict[str, Any],
        original_text: str,
) -> Dict[str, str]:
    fields = {}
    for field_name, aliases in DATA_STANDARD_FIELD_ALIASES.items():
        value = _resolve_field(parsed_result, file_item, *aliases)
        if field_name in {"startTime", "implTime"}:
            value = _normalize_date_field(value) or _normalize_date_field(_extract_labeled_value(original_text, aliases))
        elif not value and field_name == "militaryName":
            value = _extract_standard_name(original_text)
        elif not value and field_name == "num":
            value = _extract_labeled_value(original_text, aliases) or _extract_gjb_number(original_text)
        elif not value:
            value = _extract_labeled_value(original_text, aliases)
        fields[field_name] = value
    return fields


def map_analysis_result(
        parsed_result: Dict[str, Any],
        request_params: Dict[str, Any],
        original_text: str = "",
        resolved_architecture_id: Any = None,
) -> Dict[str, Any]:
    file_name = _as_text(request_params.get("fileName"))
    ranges = build_effective_analysis_ranges(request_params)
    file_item = parsed_result.get("fileDataItem")
    if not isinstance(file_item, dict):
        file_item = parsed_result.get("文件解析详细数据")
    if not isinstance(file_item, dict):
        file_item = {}

    raw_country = _first_non_empty_value(parsed_result, "country", "国家")
    raw_channel = _first_non_empty_value(parsed_result, "channel", "渠道")
    raw_maturity = _first_non_empty_value(parsed_result, "maturity", "成熟度")
    raw_security = _first_non_empty_value(parsed_result, "security", "密级", "密级程度")
    if raw_security in (None, "", [], {}):
        raw_security = _first_non_empty_value(file_item, "security", "密级", "密级程度")
    raw_format = _first_non_empty_value(parsed_result, "format", "格式")
    if raw_format in (None, "", [], {}):
        raw_format = _first_non_empty_value(file_item, "dataFormat", "资料格式")

    resolved_country = _match_option_value(raw_country, ranges["country"])
    resolved_channel = _match_option_value(raw_channel, ranges["channel"])
    resolved_maturity = _match_option_value(raw_maturity, ranges["maturity"])
    resolved_security = _match_option_value(raw_security, ranges["security"])
    resolved_format = _match_option_value(raw_format, ranges["format"])

    resolved_original_link = _resolve_field(parsed_result, file_item, "originalLink", "原文链接", "链接")
    resolved_date = _resolve_field(parsed_result, file_item, "dataTime", "资料年代", "日期", "时间")
    resolved_language = _resolve_field(parsed_result, file_item, "language", "语种")
    raw_score = _first_non_empty_value(file_item, "score", "评分")
    if raw_score is None:
        raw_score = _first_non_empty_value(parsed_result, "score", "评分")
    normalized_original_text = _as_text(
        original_text or _resolve_field(parsed_result, file_item, "originalText", "文件原文", "原文"))
    extracted_title = _extract_title(normalized_original_text)
    resolved_summary_from_model = _resolve_field(
        parsed_result,
        file_item,
        "summary",
        "摘要",
    )
    resolved_summary = resolved_summary_from_model or extracted_title
    raw_keyword_items = _sanitize_keyword_items(
        _first_non_empty_value(file_item, "keyword", "keywords", "关键词")
        or _first_non_empty_value(parsed_result, "keyword", "keywords", "关键词")
    )
    preliminary_keyword = ", ".join(raw_keyword_items)
    raw_related_technology = (
        _first_non_empty_value(file_item, "relatedTechnology", "所属技术")
        or _first_non_empty_value(parsed_result, "relatedTechnology", "所属技术")
    )
    raw_related_technology_evidence = (
        _first_non_empty_value(
            file_item,
            "relatedTechnologyEvidence",
            "所属技术证据",
        )
        or _first_non_empty_value(
            parsed_result,
            "relatedTechnologyEvidence",
            "所属技术证据",
        )
    )
    resolved_source = (
        _resolve_field(parsed_result, file_item, "source", "资料来源", "来源")
        or _extract_source(normalized_original_text)
        or UNKNOWN_SOURCE_VALUE
    )
    normalized_score = _normalize_source_score(raw_score)
    if resolved_source == UNKNOWN_SOURCE_VALUE:
        normalized_score = 55
    architecture_id = _coerce_int(resolved_architecture_id)
    if architecture_id is None:
        architecture_id = (
            _match_data_standard_architecture_id(
                ranges["architectureList"],
                normalized_original_text,
                request_params.get("originalFileName"),
                resolved_summary_from_model,
                preliminary_keyword,
                _resolve_field(parsed_result, file_item, "documentOverview", "文件概述", "概述"),
            )
            or _match_architecture_id(parsed_result, ranges["architectureList"])
        )
    resolved_keyword = _compose_analysis_keywords(
        raw_keyword_items,
        summary=resolved_summary,
        original_text=normalized_original_text,
        architecture_id=architecture_id,
        candidates=ranges["architectureList"],
    )

    file_data_item = {
        "fileName": file_name,
        "dataTime": resolved_date or _extract_date(normalized_original_text),
        "keyword": resolved_keyword,
        "summary": resolved_summary,
        "score": normalized_score,
        "fileNo": _resolve_field(parsed_result, file_item, "fileNo", "文件编号", "编号"),
        "source": resolved_source,
        "originalLink": resolved_original_link or _extract_original_link(normalized_original_text),
        "language": resolved_language or _infer_language(normalized_original_text),
        "dataFormat": resolved_format,
        "associatedEquipment": _resolve_field(parsed_result, file_item, "associatedEquipment", "所属装备"),
        "relatedTechnology": _sanitize_related_technologies(
            raw_related_technology,
            raw_evidence=raw_related_technology_evidence,
            original_text=normalized_original_text,
        ),
        "equipmentModel": _resolve_field(parsed_result, file_item, "equipmentModel", "装备型号"),
        "documentOverview": _resolve_field(parsed_result, file_item, "documentOverview", "文件概述", "概述")
                            or extracted_title,
        "originalText": normalized_original_text,
        "documentTranslationOne": "",
        "documentTranslationTwo": "",
    }

    if _is_architecture_in_standard_range(
            architecture_id,
            ranges["architectureList"],
            ranges["architectureStandardList"],
    ):
        file_data_item.update(_extract_data_standard_fields(parsed_result, file_item, normalized_original_text))

    return {
        "country": resolved_country or _match_option_value_from_text(ranges["country"], normalized_original_text),
        "channel": resolved_channel,
        "maturity": resolved_maturity,
        "security": (
            resolved_security
            or _extract_security_from_opening_text(normalized_original_text, ranges["security"])
            or _default_security_value(ranges["security"])
        ),
        "format": resolved_format,
        "architectureId": architecture_id,
        "fileDataItem": file_data_item,
    }

__all__ = (
    "ARCHITECTURE_FALLBACK_ID",
    "DATA_STANDARD_FIELD_ALIASES",
    "DATA_STANDARD_LEAF_NAMES",
    "SOURCE_SCORE_VALUES",
    "WEAPONRY_DETAIL_CATEGORY_SUFFIXES",
    "ANALYSIS_ENUM_FIELD_MAX_ITEMS",
    "ANALYSIS_ENUM_ITEM_MAX_CHARS",
    "ANALYSIS_KEYWORD_MAX_CHARS",
    "ANALYSIS_KEYWORD_MAX_COUNT",
    "ANALYSIS_KEYWORD_MIN_COUNT",
    "UNKNOWN_SOURCE_VALUE",
    "_ARCHITECTURE_TREE_INDEX_CACHE",
    "MIN_KEYWORD_COUNT",
    "MAX_KEYWORD_COUNT",
    "MAX_KEYWORD_LENGTH",
    "_as_text",
    "_as_business_original_file_name",
    "_coerce_int",
    "_normalize_match_text",
    "_contains_gjb_standard_reference",
    "_is_data_standard_candidate",
    "_architecture_candidate_topology",
    "_data_standard_candidate_ids",
    "_ordered_data_standard_leaf_ids",
    "_general_data_standard_leaf_id",
    "_first_data_standard_leaf_id",
    "_is_data_standard_parent_id",
    "_match_data_standard_architecture_id",
    "_architecture_id_set",
    "_path_ids",
    "_architecture_ancestor_ids",
    "resolve_storage_architecture_id",
    "_architecture_name_by_id",
    "_is_architecture_in_standard_range",
    "_normalize_source_score",
    "_match_option_value",
    "_default_security_value",
    "_match_architecture_id",
    "_first_non_empty_value",
    "_scalar_text",
    "_resolve_field",
    "_split_delimited_items",
    "_sanitize_delimited_items",
    "_sanitize_keyword_items",
    "_sanitize_keywords",
    "_architecture_path_keyword_names",
    "_bounded_unique_exact_items",
    "_normalize_evidence_text",
    "_has_normalized_text_evidence",
    "_classification_keyword_items",
    "_compose_analysis_keywords",
    "_related_technology_evidence_map",
    "_sanitize_related_technologies",
    "_extract_original_link",
    "_extract_date",
    "_format_iso_date",
    "_normalize_date_field",
    "_infer_language",
    "_match_option_value_from_text",
    "_opening_text",
    "_opening_identity_evidence_text",
    "_extract_security_from_opening_text",
    "_extract_title",
    "_extract_source",
    "_extract_labeled_value",
    "_extract_gjb_number",
    "_extract_standard_name",
    "_extract_data_standard_fields",
    "map_analysis_result",
)
