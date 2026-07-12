from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable

import fitz

from app.ports import (
    CollectionSpec,
    DocumentRagFactory,
    DocumentRagSession,
    KnowledgeDocumentMetadata,
    KnowledgeIndexDocumentReleasedError,
    KnowledgeIndexError,
    KnowledgeIndexFactory,
    KnowledgeIndexRetentionRequiredError,
    KnowledgeOperationContext,
    PreparedDocumentRef,
    RagExecutionTrace,
    RagLifecycleEvent,
    RagOperationError,
    RagPromptKind,
    build_document_idempotency_key,
    normalize_rag_prompt,
)
from app.services.core.config import load_ocr_config
from app.services.utils.ocr_preprocessor import prepare_analysis_file_for_upload

from app.services.utils.callback_client import post_callback_payload
from app.services.utils.file_downloader import download_to_temp_file
from app.services.utils.mhtml_normalizer import extract_text_from_mhtml, is_mhtml_file, normalize_file_for_llm
from app.services.core.progress_hub import LLMProgressHub
from app.services.core.prompts import (
    build_architecture_repair_prompt,
    build_file_analysis_prompt,
    build_json_repair_prompt,
)
from app.services.llm_service.task_service import LLMTaskService
from app.services.llm_service.translation_service import get_translation_service


logger = logging.getLogger(__name__)

DEFAULT_COUNTRY_OPTIONS = [
    {"key": "02", "value": "美国"},
    {"key": "03", "value": "俄罗斯"},
    {"key": "04", "value": "日本"},
    {"key": "05", "value": "英国"},
    {"key": "06", "value": "法国"},
]

DEFAULT_FORMAT_OPTIONS = [
    {"key": "01", "value": "音频类"},
    {"key": "03", "value": "文档类"},
    {"key": "04", "value": "图片类"},
]

DEFAULT_MATURITY_OPTIONS = [
    {"key": "01", "value": "概念研究"},
    {"key": "02", "value": "阶段成果"},
    {"key": "03", "value": "定型成果"},
]

DEFAULT_SECURITY_OPTIONS = [
    {"key": "02", "value": "公开"},
]

DEFAULT_ARCHITECTURE_OPTIONS = [
    {"id": 101, "name": "军事基地", "parentId": None, "path": "101", "pathName": "军事基地", "remark": "军事设施、基地建设、基地布局、港口码头、机场跑道、后勤保障设施等。"},
    {"id": 102, "name": "体系运用", "parentId": None, "path": "102", "pathName": "体系运用", "remark": "作战体系、系统集成、联合作战、协同配合、多域作战、体系对抗等。"},
    {"id": 103, "name": "装备型号", "parentId": None, "path": "103", "pathName": "装备型号", "remark": "武器装备、装备参数、技术指标、装备性能及型号资料。"},
    {"id": 10301, "name": "空中装备", "parentId": 103, "path": "103/10301", "pathName": "装备型号/空中装备", "remark": "飞机、无人机、航空平台及相关空中装备。"},
    {"id": 10302, "name": "水面装备", "parentId": 103, "path": "103/10302", "pathName": "装备型号/水面装备", "remark": "水面舰艇、船舶平台及相关水面装备。"},
    {"id": 10303, "name": "水下装备", "parentId": 103, "path": "103/10303", "pathName": "装备型号/水下装备", "remark": "潜艇、水下无人平台、鱼雷及相关水下装备。"},
    {"id": 104, "name": "作战环境", "parentId": None, "path": "104", "pathName": "作战环境", "remark": "战场环境、地理条件、气象水文、电磁环境、海洋环境等。"},
    {"id": 105, "name": "作战指挥", "parentId": None, "path": "105", "pathName": "作战指挥", "remark": "指挥控制、决策流程、作战计划、战术战法等。"},
    {"id": 10501, "name": "条令条例", "parentId": 105, "path": "105/10501", "pathName": "作战指挥/条令条例", "remark": "发布机构、编号、版本、规范、条令、条例、制度等。"},
    {"id": 10502, "name": "组织机构", "parentId": 105, "path": "105/10502", "pathName": "作战指挥/组织机构", "remark": "机构编制、隶属关系、职责分工、司令部、部门设置、岗位任命等。"},
    {"id": 106, "name": "数据标准", "parentId": None, "path": "106", "pathName": "数据标准", "remark": "GJB、国军标、国家军用标准、技术标准、数据规范和标准化资料。"},
]

ARCHITECTURE_FALLBACK_ID = 1
WEAPONRY_DETAIL_CATEGORY_SUFFIXES = frozenset({
    "基础数据",
    "战技指标",
    "运用数据",
    "效能数据",
})
SOURCE_SCORE_VALUES = {95, 85, 75, 65, 55}
MAX_KEYWORD_COUNT = 10
MAX_KEYWORD_LENGTH = 30
DATA_STANDARD_FIELD_ALIASES = {
    "militaryName": ("militaryName", "国军标名称", "标准名称"),
    "num": ("num", "编号", "标准编号", "国军标编号", "fileNo", "文件编号"),
    "startTime": ("startTime", "发布时间", "发布日期", "发布日"),
    "implTime": ("implTime", "实施时间", "实施日期", "实施日"),
    "approvalDept": ("approvalDept", "批准部门", "批准单位", "批准机关", "批准机构", "发布部门"),
}


def _normalize_range_list(value: Any, default: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return list(default)
    items = [item for item in value if isinstance(item, dict) and item]
    return items if items else list(default)


def build_effective_analysis_ranges(request_params: Dict[str, Any]) -> Dict[str, list[dict[str, Any]]]:
    return {
        "country": _normalize_range_list(request_params.get("country"), DEFAULT_COUNTRY_OPTIONS),
        # channel 必须完全由调用方提供，缺失或空范围不能回填服务端默认值。
        "channel": _normalize_range_list(request_params.get("channel"), []),
        "format": _normalize_range_list(request_params.get("format"), DEFAULT_FORMAT_OPTIONS),
        "maturity": _normalize_range_list(request_params.get("maturity"), DEFAULT_MATURITY_OPTIONS),
        "security": _normalize_range_list(request_params.get("security"), DEFAULT_SECURITY_OPTIONS),
        "architectureList": _normalize_range_list(request_params.get("architectureList"), DEFAULT_ARCHITECTURE_OPTIONS),
        "architectureStandardList": _normalize_range_list(request_params.get("architectureStandardList"), []),
    }


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

    GJB 兜底的业务规则要求“按候选顺序”选择数据标准叶子，因此不能把节点先放入无序集合。
    叶子关系仅根据本次请求中明确给出的 ``parentId`` 识别：若调用方没有提供某个子节点，
    服务端不会猜测完整树结构，也不会因此提前拒绝该请求。
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

    普通领域仍允许返回父节点；此函数只服务于数据标准的特殊规则。若多个数据标准叶子
    均可选或无法区分，调用方按列表顺序取第一个，而不在服务端引入证据唯一性判断。
    """
    nodes, parent_ids = _architecture_candidate_topology(architecture_list)
    standard_ids = _data_standard_candidate_ids(nodes)
    leaf_ids: list[int] = []
    seen_ids: set[int] = set()
    for item_id, _item in nodes:
        if (
                item_id in standard_ids
                and item_id not in parent_ids
                and item_id not in seen_ids
        ):
            leaf_ids.append(item_id)
            seen_ids.add(item_id)
    return leaf_ids


def _first_data_standard_leaf_id(
        architecture_list: Iterable[Dict[str, Any]],
) -> int | None:
    """返回按请求顺序可命中的第一个数据标准叶子节点。"""
    leaf_ids = _ordered_data_standard_leaf_ids(architecture_list)
    return leaf_ids[0] if leaf_ids else None


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
    """命中 GJB 线索后，按候选顺序选择数据标准分支的第一个叶子节点。"""
    if not _contains_gjb_standard_reference(*context_values):
        return None
    return _first_data_standard_leaf_id(architecture_list)


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
        logger.warning(
            "装备明细分类缺少父节点，继续按原分类存储: architecture_id=%s architecture_name=%s",
            resolved_id,
            result_name,
        )
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

    logger.warning(
        "装备明细分类未找到匹配的装备级节点，继续按原分类存储: "
        "architecture_id=%s architecture_name=%s expected_weaponry_name=%s parent_id=%s",
        resolved_id,
        result_name,
        weaponry_name,
        parent_id,
    )
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
        logger.info(
            "领域分类匹配失败，使用默认分类: reason=%s "
            "fallback_architecture_id=%s has_detail=%s detail_type=%s",
            reason,
            ARCHITECTURE_FALLBACK_ID,
            detail is not None,
            type(detail).__name__ if detail is not None else "",
        )
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


def _sanitize_keywords(raw_value: Any) -> str:
    """对 LLM 返回的 keyword 字段做后处理：拆分、截断单条过长关键词、限制总数量。

    小模型（如 4B）有时不遵守 prompt 约束，可能返回极长的单个关键词或过多关键词，
    此函数在输出前统一做校验与截断，确保 keyword 字段始终为不超过 MAX_KEYWORD_COUNT
    个、每个不超过 MAX_KEYWORD_LENGTH 字符的短词，以英文逗号+空格分隔。
    """
    if raw_value in (None, "", [], {}):
        return ""

    # 模型可能返回列表形式的关键词
    if isinstance(raw_value, list):
        parts = [_as_text(item) for item in raw_value]
    elif isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return ""
        # 按常见分隔符拆分（中英文逗号、顿号、分号、竖线、换行）
        parts = re.split(r"[,，、;；|\n\r]+", text)
    else:
        parts = [str(raw_value)]

    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        kw = part.strip().strip("\"'“”‘’").strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        # 单个关键词过长时截断
        if len(kw) > MAX_KEYWORD_LENGTH:
            logger.warning(
                "关键词长度超过上限，已截断: original_chars=%d limit=%d",
                len(kw),
                MAX_KEYWORD_LENGTH,
            )
            kw = kw[:MAX_KEYWORD_LENGTH]
        cleaned.append(kw)
        if len(cleaned) >= MAX_KEYWORD_COUNT:
            break

    return ", ".join(cleaned)


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

    for field_name, raw_value, resolved_value in (
        ("country", raw_country, resolved_country),
        ("channel", raw_channel, resolved_channel),
        ("maturity", raw_maturity, resolved_maturity),
        ("security", raw_security, resolved_security),
        ("format", raw_format, resolved_format),
    ):
        if raw_value not in (None, "", [], {}) and not resolved_value:
            logger.info(
                "字段候选值未匹配到预设范围: field=%s raw_value_chars=%d",
                field_name,
                len(_scalar_text(raw_value)),
            )

    resolved_original_link = _resolve_field(parsed_result, file_item, "originalLink", "原文链接", "链接")
    resolved_date = _resolve_field(parsed_result, file_item, "dataTime", "资料年代", "日期", "时间")
    resolved_language = _resolve_field(parsed_result, file_item, "language", "语种")
    raw_score = _first_non_empty_value(file_item, "score", "评分")
    if raw_score is None:
        raw_score = _first_non_empty_value(parsed_result, "score", "评分")
    normalized_original_text = _as_text(
        original_text or _resolve_field(parsed_result, file_item, "originalText", "文件原文", "原文"))
    extracted_title = _extract_title(normalized_original_text)
    resolved_keyword = _sanitize_keywords(
        _first_non_empty_value(file_item, "keyword", "keywords", "关键词")
        or _first_non_empty_value(parsed_result, "keyword", "keywords", "关键词")
    )
    architecture_id = _coerce_int(resolved_architecture_id)
    if architecture_id is None:
        architecture_id = (
            _match_data_standard_architecture_id(
                ranges["architectureList"],
                normalized_original_text,
                request_params.get("originalFileName"),
                _resolve_field(parsed_result, file_item, "summary", "摘要"),
                resolved_keyword,
                _resolve_field(parsed_result, file_item, "documentOverview", "文件概述", "概述"),
            )
            or _match_architecture_id(parsed_result, ranges["architectureList"])
        )

    file_data_item = {
        "fileName": file_name,
        "dataTime": resolved_date or _extract_date(normalized_original_text),
        "keyword": resolved_keyword,
        "summary": _resolve_field(parsed_result, file_item, "summary", "摘要") or extracted_title,
        "score": _normalize_source_score(raw_score),
        "fileNo": _resolve_field(parsed_result, file_item, "fileNo", "文件编号", "编号"),
        "source": _resolve_field(parsed_result, file_item, "source", "资料来源", "来源") or _extract_source(
            normalized_original_text),
        "originalLink": resolved_original_link or _extract_original_link(normalized_original_text),
        "language": resolved_language or _infer_language(normalized_original_text),
        "dataFormat": resolved_format,
        "associatedEquipment": _resolve_field(parsed_result, file_item, "associatedEquipment", "所属装备"),
        "relatedTechnology": _resolve_field(parsed_result, file_item, "relatedTechnology", "所属技术"),
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


def enrich_with_translations(
        mapped_result: Dict[str, Any],
        file_path: str,
        enable_full_translation: bool = False,
) -> Dict[str, Any]:
    """
    为映射结果添加翻译内容

    :param mapped_result: map_analysis_result 返回的映射结果
    :param file_path: 原始文件路径
    :param enable_full_translation: 是否启用全文翻译（否则只翻译摘要）
    :return: 更新后的映射结果
    """
    try:
        translation_service = get_translation_service()

        # 检查是否需要翻译
        file_item = mapped_result.get("fileDataItem", {})
        original_text = file_item.get("originalText", "")
        summary = file_item.get("summary", "")

        if not original_text and not summary:
            return mapped_result

        if enable_full_translation:
            # 全文翻译模式：翻译整个文档
            logger.info(
                "开始全文翻译文档: file_name=%s",
                Path(file_path).name,
            )

            # 【新增】定义进度回调函数，将翻译进度反馈到任务状态
            def translation_progress_callback(progress: float, message: str):
                # 计算总体进度（翻译占 0.35~0.95 区间，共 0.6 权重）
                overall_progress = 0.35 + (progress * 0.6)
                logger.debug(
                    "全文翻译进度已更新: progress_percent=%d",
                    round(overall_progress * 100),
                )

            # 设置进度回调
            translation_service.set_progress_callback(translation_progress_callback)

            bilingual_html_content, monolingual_html_content = translation_service.translate_document(
                file_path=file_path,
                target_lang="Chinese",
                translate_all=0,
                use_minerU= True,
            )

            mapped_result["fileDataItem"]["documentTranslationOne"] = monolingual_html_content
            mapped_result["fileDataItem"]["documentTranslationTwo"] = bilingual_html_content

        else:
            # 快速模式：只翻译摘要
            if summary:
                logger.info("开始翻译文档摘要: summary_chars=%d", len(summary))
                translated_summary = translation_service.translate_text_only(summary)
                mapped_result["fileDataItem"]["documentTranslationOne"] = translated_summary
                mapped_result["fileDataItem"]["documentTranslationTwo"] = summary+"\n"+translated_summary

        return mapped_result

    except Exception as e:
        logger.warning(
            "文档翻译失败，返回未翻译结果: error_type=%s",
            type(e).__name__,
        )
        return mapped_result


def build_file_callback_payload(file_name: str, mapped_result: Dict[str, Any], status: str) -> Dict[str, Any]:
    data = {"fileName": file_name, "status": status}
    data.update(mapped_result)
    return {
        "businessType": "file",
        "data": data,
        "msg": "解析成功" if status == "2" else "解析失败",
    }


def _publish_progress(progress_hub: LLMProgressHub, file_name: str, progress: float) -> None:
    progress_hub.publish(
        "file",
        file_name,
        {"businessType": "file", "data": {"fileName": file_name, "progress": progress}},
    )


def _read_original_text(file_path: str) -> str:
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".json", ".csv"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".pdf":
        with fitz.open(path) as doc:
            return "\n".join(page.get_text() for page in doc)
    if is_mhtml_file(str(path)):
        return extract_text_from_mhtml(str(path))
    return ""


def _prepare_analysis_upload_file(file_path: str) -> str:
    """返回单文件 RAG 实际使用的原文件或 OCR 增强文件路径。

    阶段 9 的 Document RAG Session 严格处理一份目标文档，因此这里不再沿用旧 Pipeline 的
    文件列表语义。路径不存在时保持原值交给 Gateway 统一产生可审计的上传阶段错误。
    """
    path = Path(file_path)
    if not path.exists():
        return str(path)

    upload_path = prepare_analysis_file_for_upload(str(path), load_ocr_config())
    upload_path_obj = Path(upload_path)
    if not upload_path_obj.exists():
        return str(path)

    return str(upload_path_obj)


class AnalysisContractError(ValueError):
    """模型回答违反文件分析业务契约。"""


class ArchitectureContractError(AnalysisContractError):
    """architectureId 缺失、类型错误或超出请求候选范围。"""


class DataStandardParentContractError(ArchitectureContractError):
    """数据标准分支的父节点不能作为最终成功分类。"""


def _reject_nonstandard_json_constant(value: str) -> None:
    """拒绝 Python JSON 解码器默认接受的 NaN 与 Infinity 扩展值。"""
    raise ValueError(f"非法 JSON 常量: {value}")


def _parse_strict_json_object(raw_result: Any) -> Dict[str, Any] | None:
    """只接受原生对象或严格 JSON 对象，不执行猜括号等有损修补。

    旧实现会从任意文本中截取最外层花括号并反复补 ``}``。这种做法可能把截断回答误判
    为有效业务数据。阶段 9 改为显式发起一次 ``JSON_REPAIR`` 模型调用，因此本地解析器
    必须保持确定性：语法不合法就返回 ``None``，由编排层决定是否修复；合法的空对象
    则继续进入宽松字段映射和独立的 architectureId 契约处理，不能伪装成语法问题。
    """
    if isinstance(raw_result, dict):
        try:
            serialized = json.dumps(
                raw_result,
                ensure_ascii=False,
                allow_nan=False,
            )
            parsed_object = json.loads(serialized)
        except (TypeError, ValueError):
            return None
        return parsed_object if isinstance(parsed_object, dict) else None
    if not isinstance(raw_result, str) or not raw_result.strip():
        return None
    try:
        parsed = json.loads(
            raw_result.strip(),
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning(
            "文件分析模型结果不是严格 JSON 对象: response_chars=%d",
            len(raw_result),
        )
        return None
    return parsed if isinstance(parsed, dict) else None


def _architecture_candidates(
        request_params: Dict[str, Any],
) -> tuple[list[Dict[str, Any]], set[int]]:
    """返回请求中的有效候选及其全部 ID。

    当请求只携带一个节点时，无论它在完整体系中是否还有子节点，都按阶段 9 契约直接
    使用该唯一 ID。多候选时允许模型选择请求中任意候选节点，不再由服务端强制要求叶子。
    """
    items: list[Dict[str, Any]] = []
    ids: set[int] = set()
    for item in build_effective_analysis_ranges(request_params)["architectureList"]:
        if not isinstance(item, dict):
            continue
        item_id = _coerce_int(item.get("id"))
        if item_id is None or item_id < 1:
            continue
        if item_id in ids:
            raise AnalysisContractError(f"architectureList 包含重复 id: {item_id}")
        ids.add(item_id)
        items.append(item)
    if not items:
        raise AnalysisContractError("architectureList 不包含有效候选")
    return items, ids


def _validate_data_standard_leaf_requirement(
        architecture_id: int,
        candidates: Iterable[Dict[str, Any]],
) -> None:
    """阻止数据标准父节点进入成功回调与永久知识库。

    普通分类父节点仍是合法业务结果，不能复用旧版“所有候选必须叶子”的全局校验。只有
    数据标准分支中、且在本次候选树内明确拥有子节点的父节点会触发此特殊规则。
    """
    if _is_data_standard_parent_id(architecture_id, candidates):
        raise DataStandardParentContractError(
            "architectureId 是数据标准父节点，必须兜底到其下叶子节点"
        )


def _resolve_analysis_architecture_id(
        parsed_result: Dict[str, Any],
        request_params: Dict[str, Any],
) -> int:
    """按单候选直返、多候选显式 ID 的规则解析 architectureId。"""
    candidates, allowed_ids = _architecture_candidates(request_params)
    if len(allowed_ids) == 1:
        architecture_id = next(iter(allowed_ids))
    else:
        if "architectureId" not in parsed_result or parsed_result.get("architectureId") in (None, ""):
            raise ArchitectureContractError("architectureId 缺失")
        raw_id = parsed_result.get("architectureId")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            raise ArchitectureContractError("architectureId 必须是数字 ID")
        if raw_id not in allowed_ids:
            raise ArchitectureContractError("architectureId 不属于请求 architectureList 候选")
        architecture_id = raw_id

    _validate_data_standard_leaf_requirement(architecture_id, candidates)
    return architecture_id


def _match_gjb_architecture_candidate(
        parsed_result: Dict[str, Any],
        request_params: Dict[str, Any],
        original_text: str,
        candidates: Iterable[Dict[str, Any]],
) -> int | None:
    """首次分类不合规时，按 GJB 线索匹配请求中的数据标准叶子节点。"""
    file_item = parsed_result.get("fileDataItem")
    if not isinstance(file_item, dict):
        file_item = {}
    return _match_data_standard_architecture_id(
        candidates,
        original_text,
        request_params.get("originalFileName"),
        _resolve_field(parsed_result, file_item, "summary", "摘要"),
        _first_non_empty_value(file_item, "keyword", "keywords", "关键词"),
        _resolve_field(parsed_result, file_item, "documentOverview", "文件概述", "概述"),
    )


def _validate_architecture_repair_result(
        raw_result: Any,
        request_params: Dict[str, Any],
) -> int:
    """要求分类修复只能返回一个 architectureId 键并再次执行候选校验。"""
    repaired = _parse_strict_json_object(raw_result)
    if repaired is None:
        raise ArchitectureContractError("architecture 修复结果不是严格 JSON 对象")
    if set(repaired) != {"architectureId"}:
        raise ArchitectureContractError("architecture 修复结果只能包含 architectureId")
    return _resolve_analysis_architecture_id(repaired, request_params)


def _safe_task_error(error: BaseException, *, fallback: str) -> str:
    """生成有界任务错误，避免把 Prompt、正文或外部响应写入普通日志和回调状态。"""
    if isinstance(
        error,
        (AnalysisContractError, KnowledgeIndexError, RagOperationError, ValueError),
    ):
        message = str(error)
    else:
        message = f"{fallback}（{type(error).__name__}）"
    return " ".join((message or fallback).split())[:500]


def _record_lease_resources(
        task_service: LLMTaskService,
        execution_id: str,
        trace: RagExecutionTrace,
        prepared_document: PreparedDocumentRef | None = None,
) -> None:
    """把 Trace 中已经可定位的资源立即写入跨进程租约。

    上传阶段失败时可能还没有完整 ``PreparedDocumentRef``，但生命周期中的上传位置仍可
    用于人工巡检。空字段不会覆盖租约中此前已经记录的更完整引用。
    """
    external_location = ""
    if prepared_document is not None:
        external_location = prepared_document.external_location
    else:
        for event in reversed(trace.lifecycle_events):
            if event.operation == "document_upload" and event.external_ref:
                external_location = event.external_ref
                break
    task_service.rag_resource_leases.record_resources(
        execution_id=execution_id,
        context_ref=trace.context_ref or "",
        conversation_ref=trace.conversation_ref or "",
        document_ref=(prepared_document.document_ref if prepared_document else ""),
        external_location=external_location,
    )


def _submit_callback(
        *,
        task_service: LLMTaskService,
        file_name: str,
        original_name: str,
        callback_url: str,
        callback_timeout: float,
        callback_payload: Dict[str, Any],
) -> None:
    """在业务终态落库后执行可选回调，并精确推进回调状态机。"""
    if not callback_url:
        try:
            task_service.mark_callback_skipped("file", file_name)
        except Exception:
            logger.critical(
                "未配置回调地址，但无法将任务标记为无需回调: file_name=%s",
                file_name,
                exc_info=True,
            )
        return
    callback_context = {
        "businessType": "file",
        "fileName": file_name,
        "originalFileName": original_name,
    }
    try:
        succeeded = post_callback_payload(
            callback_url,
            callback_payload,
            timeout=callback_timeout,
            callback_context=callback_context,
        )
    except Exception as exc:  # 回调异常不能改写已经确定的业务成功或失败结果。
        callback_error = _safe_task_error(exc, fallback="callback failed")
        try:
            task_service.mark_callback_failed("file", file_name, callback_error)
        except Exception:
            logger.critical(
                "文件分析回调发生异常后，无法将任务标记为回调失败: file_name=%s",
                file_name,
                exc_info=True,
            )
        logger.exception(
            "文件分析回调发生异常: file_name=%s error_type=%s",
            file_name,
            type(exc).__name__,
        )
        return
    try:
        if succeeded:
            task_service.mark_callback_success("file", file_name)
            logger.info("文件分析回调提交成功: file_name=%s", file_name)
        else:
            task_service.mark_callback_failed("file", file_name, "callback failed")
            logger.warning("文件分析回调提交失败: file_name=%s", file_name)
    except Exception:
        logger.critical(
            "文件分析回调已执行但结果状态无法提交: file_name=%s callback_succeeded=%s",
            file_name,
            succeeded,
            exc_info=True,
        )


def _finalize_file_failure(
        *,
        task_service: LLMTaskService,
        progress_hub: LLMProgressHub,
        file_name: str,
        original_name: str,
        stage: str,
        error_message: str,
        callback_url: str,
        callback_timeout: float,
) -> None:
    """以失败语义终结任务；任务库不可写时禁止绕过状态落库发送外部回调。"""
    callback_payload = build_file_callback_payload(file_name, {}, status="3")
    try:
        task_service.mark_business_result(
            "file",
            file_name,
            callback_payload,
            status="3",
            message=f"解析失败（{stage}）：{error_message}",
        )
        _publish_progress(progress_hub, file_name, 1.0)
    except Exception:  # SQLite 整体不可写时，回调也不能对外宣称已有可追踪的业务终态。
        logger.critical(
            "文件分析失败状态无法持久化，停止回调: file_name=%s stage=%s",
            file_name,
            stage,
            exc_info=True,
        )
        return
    try:
        _submit_callback(
            task_service=task_service,
            file_name=file_name,
            original_name=original_name,
            callback_url=callback_url,
            callback_timeout=callback_timeout,
            callback_payload=callback_payload,
        )
    except Exception:
        # 回调状态落库失败不能阻止审计成功后的资源补偿。调用方会继续执行 close，运维
        # 可以根据任务终态和 critical 日志重放回调状态修复。
        logger.critical(
            "文件分析失败回调状态无法提交: file_name=%s stage=%s",
            file_name,
            stage,
            exc_info=True,
        )


def _close_audited_session(
        *,
        task_service: LLMTaskService,
        session: DocumentRagSession,
        interaction_id: int,
        execution_id: str,
        audited_trace: RagExecutionTrace,
        retain_document: bool,
) -> None:
    """关闭已审计 Session，并原子追加关闭事件与 cleanup 结果。

    外部关闭已经发生但追加审计失败时，业务结果不回滚；资源租约保持 ``audited``，使巡检
    能发现“外部可能已关闭、关闭证据尚未提交”的异常，而不是错误标记为完全 closed。
    """
    try:
        cleanup = session.close(retain_document=retain_document)
    except Exception:
        logger.critical(
            "RAG 会话关闭调用发生异常: interaction_id=%s execution_id=%s "
            "retain_document=%s",
            interaction_id,
            execution_id,
            retain_document,
            exc_info=True,
        )
        return
    closed_trace = session.trace
    initial_event_count = len(audited_trace.lifecycle_events)
    close_events = closed_trace.lifecycle_events[initial_event_count:]
    cleanup_status = "deleted" if cleanup.success else "failed"
    cleanup_error = "" if cleanup.success else cleanup.error_message
    try:
        if not close_events:
            raise RuntimeError("RAG Session 关闭后未生成生命周期事件")
        task_service.append_llm_interaction_lifecycle_events(
            interaction_id,
            close_events,
            cleanup_status=cleanup_status,
            cleanup_error=cleanup_error,
        )
    except Exception:
        logger.critical(
            "RAG 会话已关闭，但无法追加关闭审计: interaction_id=%s execution_id=%s",
            interaction_id,
            execution_id,
            exc_info=True,
        )
        return
    try:
        if cleanup.success:
            task_service.rag_resource_leases.mark_closed(
                execution_id=execution_id,
            )
        else:
            task_service.rag_resource_leases.record_cleanup_failure(
                execution_id=execution_id,
                error_message=cleanup_error,
            )
    except Exception:
        logger.critical(
            "RAG 会话关闭后，无法结束资源租约: interaction_id=%s execution_id=%s",
            interaction_id,
            execution_id,
            exc_info=True,
        )


def _store_prepared_analysis_document(
        *,
        knowledge_index_factory: KnowledgeIndexFactory,
        execution_id: str,
        file_name: str,
        original_name: str,
        mapped_result: Dict[str, Any],
        architecture_list: Iterable[Dict[str, Any]],
        prepared_document: PreparedDocumentRef,
) -> None:
    """把 RAG 已上传的同一文档转交永久知识库，不读取源文件也不二次上传。"""
    logger.info(
        "开始将已上传文档转交永久知识库: file_name=%s execution_id=%s",
        file_name,
        execution_id,
    )
    result_architecture_id = int(mapped_result["architectureId"])
    logger.info(
        "文件分析结果已确定分类: execution_id=%s result_architecture_id=%s",
        execution_id,
        result_architecture_id,
    )
    storage_architecture_id = resolve_storage_architecture_id(
        result_architecture_id,
        architecture_list,
    )
    logger.info(
        "永久知识库存储分类已确定: execution_id=%s storage_architecture_id=%s",
        execution_id,
        storage_architecture_id,
    )
    if storage_architecture_id is None or storage_architecture_id < 1:
        raise AnalysisContractError("无法确定永久知识库存储分类")
    if storage_architecture_id != result_architecture_id:
        logger.info(
            "文件知识库存储分类归并: file_name=%s result_architecture_id=%s "
            "result_architecture_name=%s storage_architecture_id=%s storage_architecture_name=%s",
            file_name,
            result_architecture_id,
            _architecture_name_by_id(result_architecture_id, architecture_list),
            storage_architecture_id,
            _architecture_name_by_id(storage_architecture_id, architecture_list),
        )
    attributes = {
        key: mapped_result.get(key, "")
        for key in ("country", "channel", "maturity", "security", "format")
    }
    metadata = KnowledgeDocumentMetadata(
        file_name=file_name,
        original_name=original_name,
        # 此名称来自 RAG Gateway 实际提交给 AnythingLLM 的不可变上传副本，而不是
        # 下载文件或业务哈希名。MHTML/PDF、OCR/Markdown 等预处理后的来源映射必须以
        # 它为准，才能在 weaponry 回调中稳定回填业务原始名。
        ingested_file_name=prepared_document.ingested_file_name,
        attributes=attributes,
    )
    logger.info(
        "永久知识库文档元数据已构建: file_name=%s has_ingested_file_name=%s "
        "attribute_key_count=%d",
        file_name,
        bool(metadata.ingested_file_name),
        len(attributes),
    )
    operation_context = KnowledgeOperationContext(
        execution_id=execution_id,
        business_type="file",
        business_key=file_name,
    )
    idempotency_key = build_document_idempotency_key(
        file_name=file_name,
        architecture_id=storage_architecture_id,
        content_sha256=prepared_document.content_sha256,
    )
    logger.info(
        "永久知识库写入幂等键已生成: execution_id=%s key_length=%d",
        execution_id,
        len(idempotency_key),
    )
    try:
        logger.debug("开始创建永久知识库任务对象: execution_id=%s", execution_id)
        with knowledge_index_factory.create() as knowledge_index:
            logger.debug("永久知识库任务对象创建完成: execution_id=%s", execution_id)
            collection = knowledge_index.ensure_collection(
                CollectionSpec(
                    architecture_id=storage_architecture_id,
                    name=f"architectureId-{storage_architecture_id}",
                )
            )
            logger.info(
                "永久知识集合已确认: execution_id=%s architecture_id=%s",
                execution_id,
                collection.architecture_id,
            )
            logger.debug("开始写入永久知识库文档: execution_id=%s", execution_id)
            knowledge_index.store_prepared_document(
                collection,
                prepared_document,
                metadata,
                operation_context=operation_context,
                idempotency_key=idempotency_key,
            )
            logger.debug("永久知识库文档写入调用完成: execution_id=%s", execution_id)
        logger.debug("永久知识库任务对象已正常关闭: execution_id=%s", execution_id)
    except Exception as exc:
        logger.exception(
            "写入永久知识库时发生异常: file_name=%s execution_id=%s error_type=%s",
            file_name,
            execution_id,
            type(exc).__name__,
        )
        raise
    logger.info(
        "文件分析文档所有权已转交永久知识库: file_name=%s execution_id=%s "
        "architecture_id=%s storage_architecture_id=%s",
        file_name,
        execution_id,
        result_architecture_id,
        storage_architecture_id,
    )


def _execute_file_analysis_task(
        *,
        task_service: LLMTaskService,
        progress_hub: LLMProgressHub,
        request_payload: Dict[str, Any],
        download_root: str,
        callback_url: str,
        callback_timeout: float,
        document_rag_factory: DocumentRagFactory,
        knowledge_index_factory: KnowledgeIndexFactory,
) -> None:
    """按审计硬前置契约执行单文件分析和永久知识库转交。

    关键顺序固定为：准备文件 → 隔离 RAG → 领域契约 → 原子审计 → 永久知识库 → 翻译与
    业务结果 → 回调 → 审计化关闭。任何审计失败都保留外部现场且绝不调用 ``close``；审计
    成功后的失败则按永久知识库是否已经接管文档决定删除或保留全局实体。
    """
    if not isinstance(document_rag_factory, DocumentRagFactory):
        raise TypeError("document_rag_factory 必须实现 DocumentRagFactory")
    if not isinstance(knowledge_index_factory, KnowledgeIndexFactory):
        raise TypeError("knowledge_index_factory 必须实现 KnowledgeIndexFactory")
    params = request_payload["params"][0]
    file_name = _as_text(params.get("fileName"))
    requested_original_name = _as_business_original_file_name(
        params.get("originalFileName"),
    )
    original_name = requested_original_name or file_name
    if not requested_original_name:
        # 不改变既有接口的可选参数约束，但明确记录此类请求无法提供业务原始名；后续
        # weaponry 只能稳定回填哈希名，绝不能把预处理生成的文件名伪装成业务原始名。
        logger.warning(
            "文件分析请求缺少originalFileName，来源展示将回退为业务哈希名: file_name=%s",
            file_name,
        )
    file_path = _as_text(params.get("filePath"))
    task = task_service.get_task("file", file_name)
    if task is None:
        raise ValueError(f"文件分析任务不存在: {file_name}")
    execution_id = _as_text(task.get("execution_id"))
    analysis_prompt = ""

    logger.info(
        "开始执行文件分析任务: file_name=%s execution_id=%s",
        file_name,
        execution_id,
    )
    try:
        task_service.update_task_progress(
            "file", file_name, progress=0.15, message="正在下载文件", status="1"
        )
        _publish_progress(progress_hub, file_name, 0.15)
        downloaded_path = download_to_temp_file(
            file_path,
            file_name,
            download_root,
            timeout=60,
        )
        task_service.update_task_progress(
            "file", file_name, progress=0.35, message="正在执行文档解析"
        )
        _publish_progress(progress_hub, file_name, 0.35)

        llm_file_path = downloaded_path
        try:
            llm_file_path = normalize_file_for_llm(downloaded_path)
        except Exception as exc:  # 归一化是增强能力，原文件仍是合法的降级输入。
            logger.warning(
                "MHTML 归一化失败，降级使用原文件: file_name=%s error_type=%s",
                file_name,
                type(exc).__name__,
            )
        llm_file_path = _prepare_analysis_upload_file(llm_file_path)
        analysis_prompt = normalize_rag_prompt(build_file_analysis_prompt(params))
    except Exception as exc:
        error_message = _safe_task_error(exc, fallback="文件预处理失败")
        logger.exception(
            "文件分析预处理失败: file_name=%s execution_id=%s",
            file_name,
            execution_id,
        )
        _finalize_file_failure(
            task_service=task_service,
            progress_hub=progress_hub,
            file_name=file_name,
            original_name=original_name,
            stage="preparation",
            error_message=error_message,
            callback_url=callback_url,
            callback_timeout=callback_timeout,
        )
        return

    with document_rag_factory.create() as document_rag:
        try:
            task_service.rag_resource_leases.begin(
                execution_id=execution_id,
                business_type="file",
                business_key=file_name,
            )
        except Exception as exc:
            # Factory 进入只创建本地 HTTP 对象图，不创建远端资源。租约登记仍严格发生在
            # open_isolated_session 之前；登记失败时立即退出租约，不会产生无法追踪的资源。
            lease_error = _safe_task_error(exc, fallback="RAG 资源租约登记失败")
            logger.exception(
                "RAG 资源租约登记失败，未创建外部资源: file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                original_name=original_name,
                stage="resource_lease",
                error_message=lease_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
            return
        session: DocumentRagSession | None = None
        prepared_document: PreparedDocumentRef | None = None
        final_prompt = analysis_prompt
        try:
            session = document_rag.open_isolated_session(
                context_name=f"llm-file-{execution_id}",
                conversation_name=f"analysis-{Path(file_name).stem}",
            )
            _record_lease_resources(
                task_service,
                execution_id,
                session.trace,
            )
            rag_result = session.analyse(
                llm_file_path,
                analysis_prompt,
                require_sources=True,
                max_attempts=2,
            )
            prepared_document = rag_result.prepared_document
            _record_lease_resources(
                task_service,
                execution_id,
                rag_result.trace,
                prepared_document,
            )

            parsed_result = _parse_strict_json_object(rag_result.text)
            if parsed_result is None:
                final_prompt = normalize_rag_prompt(
                    build_json_repair_prompt(rag_result.text)
                )
                repaired_result = session.ask(
                    final_prompt,
                    prompt_kind=RagPromptKind.JSON_REPAIR,
                    require_sources=True,
                    max_attempts=1,
                )
                parsed_result = _parse_strict_json_object(repaired_result.text)
                if parsed_result is None:
                    raise AnalysisContractError("JSON 修复后仍不是严格 JSON 对象")

            original_text = _read_original_text(llm_file_path)
            try:
                architecture_id = _resolve_analysis_architecture_id(
                    parsed_result,
                    params,
                )
            except ArchitectureContractError as contract_error:
                candidates, allowed_ids = _architecture_candidates(params)
                if isinstance(contract_error, DataStandardParentContractError):
                    # 模型已返回合法候选 ID，但该 ID 是数据标准父节点。该场景不依赖
                    # GJB 关键词，按前端原始候选顺序直接兜底到数据标准叶子节点。
                    architecture_id = _first_data_standard_leaf_id(candidates)
                    fallback_reason = "data_standard_parent"
                else:
                    # 保留既有 GJB 兜底：首次结果缺失、类型错误或超出候选范围时，若正文
                    # 存在 GJB 线索，则按候选顺序选择数据标准分支中的第一个叶子节点。
                    architecture_id = _match_gjb_architecture_candidate(
                        parsed_result,
                        params,
                        original_text,
                        candidates,
                    )
                    fallback_reason = "gjb_reference"
                if architecture_id is not None:
                    logger.info(
                        "文件分析分类已按候选顺序兜底到数据标准叶子: "
                        "file_name=%s fallback_reason=%s architecture_id=%s",
                        file_name,
                        fallback_reason,
                        architecture_id,
                    )
                else:
                    final_prompt = normalize_rag_prompt(
                        build_architecture_repair_prompt(
                            parsed_result,
                            [
                                item
                                for item in candidates
                                if _coerce_int(item.get("id")) in allowed_ids
                            ],
                            str(contract_error),
                        )
                    )
                    repaired_result = session.ask(
                        final_prompt,
                        prompt_kind=RagPromptKind.ARCHITECTURE_REPAIR,
                        require_sources=True,
                        max_attempts=1,
                    )
                    architecture_id = _validate_architecture_repair_result(
                        repaired_result.text,
                        params,
                    )
            mapped_result = map_analysis_result(
                parsed_result,
                params,
                original_text=original_text,
                resolved_architecture_id=architecture_id,
            )
        except Exception as exc:
            trace = exc.trace if isinstance(exc, RagOperationError) else (
                session.trace if session is not None else None
            )
            if trace is None:
                raise
            try:
                _record_lease_resources(
                    task_service,
                    execution_id,
                    trace,
                    prepared_document,
                )
            except Exception:
                logger.critical(
                    "文件分析失败后无法更新资源租约: file_name=%s execution_id=%s",
                    file_name,
                    execution_id,
                    exc_info=True,
                )
            error_message = _safe_task_error(exc, fallback="RAG 或业务契约失败")
            failure_stage = trace.failure_stage or "business_contract"
            try:
                audit_result = task_service.create_llm_interaction_with_trace(
                    business_type="file",
                    business_key=file_name,
                    execution_id=execution_id,
                    prompt=final_prompt,
                    trace=trace,
                    status="failed",
                    error_message=error_message,
                )
            except Exception as audit_exc:
                audit_error = _safe_task_error(audit_exc, fallback="交互审计失败")
                try:
                    task_service.rag_resource_leases.mark_audit_result(
                        execution_id=execution_id,
                        interaction_id=None,
                        error_message=audit_error,
                    )
                except Exception:
                    logger.critical(
                        "交互审计失败后资源租约状态也无法更新: execution_id=%s",
                        execution_id,
                        exc_info=True,
                    )
                logger.critical(
                    "文件分析交互审计失败，保留全部 RAG 现场: file_name=%s execution_id=%s",
                    file_name,
                    execution_id,
                    exc_info=True,
                )
                _finalize_file_failure(
                    task_service=task_service,
                    progress_hub=progress_hub,
                    file_name=file_name,
                    original_name=original_name,
                    stage="audit",
                    error_message=audit_error,
                    callback_url=callback_url,
                    callback_timeout=callback_timeout,
                )
                return

            try:
                task_service.rag_resource_leases.mark_audit_result(
                    execution_id=execution_id,
                    interaction_id=audit_result.interaction_id,
                )
            except Exception as lease_exc:
                lease_error = _safe_task_error(
                    lease_exc,
                    fallback="资源租约审计状态更新失败",
                )
                logger.critical(
                    "失败交互已审计但资源租约推进失败: interaction_id=%s execution_id=%s",
                    audit_result.interaction_id,
                    execution_id,
                    exc_info=True,
                )
                _finalize_file_failure(
                    task_service=task_service,
                    progress_hub=progress_hub,
                    file_name=file_name,
                    original_name=original_name,
                    stage="resource_lease",
                    error_message=lease_error,
                    callback_url=callback_url,
                    callback_timeout=callback_timeout,
                )
                if session is not None:
                    _close_audited_session(
                        task_service=task_service,
                        session=session,
                        interaction_id=audit_result.interaction_id,
                        execution_id=execution_id,
                        audited_trace=trace,
                        retain_document=False,
                    )
                return
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                original_name=original_name,
                stage=failure_stage,
                error_message=error_message,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
            if session is not None:
                _close_audited_session(
                    task_service=task_service,
                    session=session,
                    interaction_id=audit_result.interaction_id,
                    execution_id=execution_id,
                    audited_trace=trace,
                    retain_document=False,
                )
            else:
                # open_isolated_session 失败时，Gateway 已把内部回滚写入初始 trace；没有
                # 可供业务层再次 close 的 Session。原子审计入口已经按回滚事件写入 cleanup
                # 终态；这里只在回滚成功时终结资源租约，失败时继续保留待恢复记录。
                rollback_failed = any(
                    event.operation == "context_rollback" and not event.success
                    for event in trace.lifecycle_events
                )
                if not rollback_failed:
                    task_service.rag_resource_leases.mark_closed(
                        execution_id=execution_id,
                    )
                else:
                    logger.critical(
                        "隔离 Session 打开回滚失败，资源租约保持待恢复: "
                        "interaction_id=%s execution_id=%s",
                        audit_result.interaction_id,
                        execution_id,
                    )
            return

        successful_trace = session.trace
        try:
            audit_result = task_service.create_llm_interaction_with_trace(
                business_type="file",
                business_key=file_name,
                execution_id=execution_id,
                prompt=final_prompt,
                trace=successful_trace,
                status="succeeded",
            )
        except Exception as audit_exc:
            audit_error = _safe_task_error(audit_exc, fallback="交互审计失败")
            try:
                task_service.rag_resource_leases.mark_audit_result(
                    execution_id=execution_id,
                    interaction_id=None,
                    error_message=audit_error,
                )
            except Exception:
                logger.critical(
                    "成功结果审计失败后资源租约状态也无法更新: execution_id=%s",
                    execution_id,
                    exc_info=True,
                )
            logger.critical(
                "文件分析成功结果审计失败，禁止永久入库并保留现场: "
                "file_name=%s execution_id=%s",
                file_name,
                execution_id,
                exc_info=True,
            )
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                original_name=original_name,
                stage="audit",
                error_message=audit_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
            return

        try:
            task_service.rag_resource_leases.mark_audit_result(
                execution_id=execution_id,
                interaction_id=audit_result.interaction_id,
            )
        except Exception as lease_exc:
            lease_error = _safe_task_error(lease_exc, fallback="资源租约审计状态更新失败")
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                original_name=original_name,
                stage="resource_lease",
                error_message=lease_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
            _close_audited_session(
                task_service=task_service,
                session=session,
                interaction_id=audit_result.interaction_id,
                execution_id=execution_id,
                audited_trace=successful_trace,
                retain_document=False,
            )
            return

        retain_document = False
        knowledge_store_succeeded = False
        try:
            _store_prepared_analysis_document(
                knowledge_index_factory=knowledge_index_factory,
                execution_id=execution_id,
                file_name=file_name,
                original_name=original_name,
                mapped_result=mapped_result,
                architecture_list=build_effective_analysis_ranges(params)["architectureList"],
                prepared_document=prepared_document,
            )
            retain_document = True
            knowledge_store_succeeded = True
        except KnowledgeIndexDocumentReleasedError as knowledge_exc:
            # 只有该类型能证明 Gateway 已解绑永久集合并提交补偿成功状态，此时允许 RAG
            # Session 永久删除未转交的全局文档。
            logger.exception(
                "永久知识库写入失败且已完成文档释放补偿: file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
            knowledge_error = _safe_task_error(knowledge_exc, fallback="永久知识库写入失败")
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                original_name=original_name,
                stage="knowledge_index",
                error_message=knowledge_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
        except KnowledgeIndexRetentionRequiredError as knowledge_exc:
            retain_document = True
            logger.exception(
                "永久知识库写入状态需人工恢复，保留全局文档: file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
            knowledge_error = _safe_task_error(knowledge_exc, fallback="永久知识库需要恢复")
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                original_name=original_name,
                stage="knowledge_index_recovery",
                error_message=knowledge_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
        except Exception as knowledge_exc:
            # 未分类异常无法证明永久集合没有接管文档。安全策略必须保留全局实体，等待
            # 协调记录对账；错误删除会破坏永久知识库中可能已经提交的引用。
            retain_document = True
            logger.exception(
                "永久知识库写入发生未分类异常，保留全局文档: "
                "file_name=%s execution_id=%s error_type=%s",
                file_name,
                execution_id,
                type(knowledge_exc).__name__,
            )
            knowledge_error = _safe_task_error(knowledge_exc, fallback="永久知识库写入状态不确定")
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                original_name=original_name,
                stage="knowledge_index_unknown",
                error_message=knowledge_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
        if not knowledge_store_succeeded:
            _close_audited_session(
                task_service=task_service,
                session=session,
                interaction_id=audit_result.interaction_id,
                execution_id=execution_id,
                audited_trace=successful_trace,
                retain_document=retain_document,
            )
            return

        try:
            task_service.update_task_progress(
                "file", file_name, progress=0.65, message="正在翻译文档", status="1"
            )
            _publish_progress(progress_hub, file_name, 0.65)
            enriched_result = enrich_with_translations(
                mapped_result,
                downloaded_path,
                params.get("enableFullTranslation", True),
            )
            task_service.update_task_progress(
                "file", file_name, progress=0.95, message="翻译完成，准备回调", status="1"
            )
            _publish_progress(progress_hub, file_name, 0.95)
            callback_payload = build_file_callback_payload(
                file_name,
                enriched_result,
                status="2",
            )
            task_service.mark_business_result(
                "file",
                file_name,
                callback_payload,
                status="2",
                message="解析完成",
            )
            _publish_progress(progress_hub, file_name, 1.0)
            _submit_callback(
                task_service=task_service,
                file_name=file_name,
                original_name=original_name,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
                callback_payload=callback_payload,
            )
            logger.info(
                "文件分析任务完成: file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
        except Exception as exc:
            post_transfer_error = _safe_task_error(exc, fallback="知识库转交后业务处理失败")
            logger.exception(
                "文件分析在文档所有权转交后失败: file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                original_name=original_name,
                stage="post_transfer",
                error_message=post_transfer_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
        finally:
            _close_audited_session(
                task_service=task_service,
                session=session,
                interaction_id=audit_result.interaction_id,
                execution_id=execution_id,
                audited_trace=successful_trace,
                retain_document=True,
            )


def run_file_analysis_task(
        *,
        task_service: LLMTaskService,
        progress_hub: LLMProgressHub,
        request_payload: Dict[str, Any],
        download_root: str,
        callback_url: str,
        callback_timeout: float,
        document_rag_factory: DocumentRagFactory,
        knowledge_index_factory: KnowledgeIndexFactory,
) -> None:
    """提供后台线程的最终异常边界，并委托阶段 9 单文件状态机。

    状态机内部已经处理所有创建 Session 后的异常。本边界主要覆盖 Factory 进入失败、依赖
    契约错误等尚未创建外部资源的异常，确保后台线程不会让任务永久停留在处理中。若未来
    在内部增加新的外部副作用，必须先把相应审计和补偿加入状态机，不能依赖本兜底处理。
    """
    try:
        _execute_file_analysis_task(
            task_service=task_service,
            progress_hub=progress_hub,
            request_payload=request_payload,
            download_root=download_root,
            callback_url=callback_url,
            callback_timeout=callback_timeout,
            document_rag_factory=document_rag_factory,
            knowledge_index_factory=knowledge_index_factory,
        )
    except Exception as exc:
        params_list = request_payload.get("params", [])
        params = params_list[0] if params_list and isinstance(params_list[0], dict) else {}
        file_name = _as_text(params.get("fileName"))
        original_name = (
            _as_business_original_file_name(params.get("originalFileName"))
            or file_name
        )
        error_message = _safe_task_error(exc, fallback="文件分析编排失败")
        logger.exception(
            "文件分析后台线程未处理异常: file_name=%s error_type=%s",
            file_name,
            type(exc).__name__,
        )
        if file_name:
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                original_name=original_name,
                stage="orchestration",
                error_message=error_message,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )


def run_file_analysis_batch_task(
        *,
        task_service: LLMTaskService,
        progress_hub: LLMProgressHub,
        request_payload: Dict[str, Any],
        download_root: str,
        callback_url: str,
        callback_timeout: float,
        document_rag_factory: DocumentRagFactory,
        knowledge_index_factory: KnowledgeIndexFactory,
) -> None:
    """按请求顺序执行批量分析，并保证每个文件分别进入两类 Factory 租约。"""
    params_list = request_payload.get("params", [])
    for index, params in enumerate(params_list):
        if not isinstance(params, dict):
            continue
        file_name = _as_text(params.get("fileName"))
        if not file_name:
            continue
        if index > 0:
            task_service.update_task_progress(
                "file",
                file_name,
                progress=0.0,
                message="准备开始解析",
                status="1",
            )
            _publish_progress(progress_hub, file_name, 0.0)
        run_file_analysis_task(
            task_service=task_service,
            progress_hub=progress_hub,
            request_payload={"businessType": "file", "params": [params]},
            download_root=download_root,
            callback_url=callback_url,
            callback_timeout=callback_timeout,
            document_rag_factory=document_rag_factory,
            knowledge_index_factory=knowledge_index_factory,
        )
