from __future__ import annotations

import json
import hashlib
import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

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
from app.services.core.config import (
    ANALYSIS_DATA_STANDARD_MODE_LEGACY,
    ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    ANALYSIS_DATA_STANDARD_MODES,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
    ANALYSIS_FILENAME_CONSTRAINT_MODES,
    ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
    ANALYSIS_IDENTITY_RESELECT_MODE_OFF,
    ANALYSIS_IDENTITY_RESELECT_MODES,
    load_ocr_config,
)
from app.services.core.architecture_tree import (
    ArchitectureNodeProfile,
    ArchitectureTreeIndex,
    ArchitectureTreeIndexCache,
    ArchitectureTreeValidationError,
    build_architecture_tree_index,
)
from app.services.utils.ocr_preprocessor import prepare_analysis_file_for_upload

from app.services.utils.callback_client import post_callback_payload
from app.services.utils.file_downloader import download_to_temp_file
from app.services.utils.mhtml_normalizer import extract_text_from_mhtml, is_mhtml_file, normalize_file_for_llm
from app.services.utils.word_extractor import extract_text_from_word
from app.services.core.progress_hub import LLMProgressHub
from app.services.core.prompts import (
    ANALYSIS_KEYWORD_COUNT,
    ANALYSIS_KEYWORD_MAX_CHARS,
    UNKNOWN_SOURCE_VALUE,
    build_architecture_classification_prompt,
    build_architecture_repair_prompt,
    build_architecture_reselect_prompt,
    build_data_standard_classification_prompt,
    build_file_analysis_prompt,
    build_file_extraction_prompt,
    build_json_repair_prompt,
    data_standard_candidate_remark,
)
from app.services.llm_service.architecture_recall_service import (
    ArchitecturePromptBudgetError,
    ArchitectureRecallDecision,
    ArchitectureRecallError,
    DocumentArchitectureSignals,
    build_document_architecture_signals,
    recall_architecture_candidates,
)
from app.services.llm_service.task_service import (
    LLMTaskService,
    TaskExecutionConflictError,
    TaskStateConflictError,
)
from app.services.llm_service.translation_service import get_translation_service


logger = logging.getLogger(__name__)


ANALYSIS_CLASSIFICATION_MODES = frozenset(
    {"topk_two_stage", "topk_single", "legacy"}
)
MAX_ANALYSIS_PROMPT_CHARS = 32_000
MAX_ANALYSIS_MODEL_CALLS = 4
MAX_ANALYSIS_PHASE_CALLS = 2
MAX_ANALYSIS_PARAMS_PER_REQUEST = 32
MAX_ANALYSIS_REQUEST_BYTES = 64 * 1024 * 1024
_ARCHITECTURE_TREE_INDEX_CACHE = ArchitectureTreeIndexCache(capacity=4)

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
DATA_STANDARD_LEAF_NAMES = frozenset(
    {"建模与仿真", "军用软件", "目标特性", "术语与定义", "通用要求", "元数据"}
)
MAX_KEYWORD_COUNT = ANALYSIS_KEYWORD_COUNT
MAX_KEYWORD_LENGTH = ANALYSIS_KEYWORD_MAX_CHARS
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


def validate_analysis_architecture_ranges(
    request_params: Mapping[str, Any],
) -> ArchitectureTreeIndex:
    """在任何任务或远端副作用前校验 analysis 的领域树输入。

    缺失、``null`` 和空数组继续使用历史默认领域树，避免破坏既有调用方；只要调用方
    显式提供了非空范围，就必须完整通过结构、拓扑和资源边界校验，不能再静默过滤坏节点。
    ``architectureStandardList`` 是独立的有限树范围，不要求是主树的子集。
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
        "score": normalized_score,
        "fileNo": _resolve_field(parsed_result, file_item, "fileNo", "文件编号", "编号"),
        "source": resolved_source,
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
    if suffix == ".docx":
        return extract_text_from_word(str(path))
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


def _normalize_analysis_classification_mode(value: Any) -> str:
    mode = _as_text(value) or "topk_two_stage"
    if mode not in ANALYSIS_CLASSIFICATION_MODES:
        raise ValueError(
            "analysis_classification_mode 必须是 "
            "topk_two_stage、topk_single 或 legacy"
        )
    return mode


def _normalize_analysis_filename_constraint_mode(value: Any) -> str:
    mode = _as_text(value) or ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY
    if mode not in ANALYSIS_FILENAME_CONSTRAINT_MODES:
        raise ValueError(
            "analysis_filename_constraint_mode 必须是 legacy 或 scope_guard"
        )
    return mode


def _normalize_analysis_data_standard_mode(value: Any) -> str:
    mode = _as_text(value) or ANALYSIS_DATA_STANDARD_MODE_LEGACY
    if mode not in ANALYSIS_DATA_STANDARD_MODES:
        raise ValueError(
            "analysis_data_standard_mode 必须是 legacy 或 scope_guard"
        )
    return mode


def _normalize_analysis_identity_reselect_mode(value: Any) -> str:
    mode = _as_text(value) or ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE
    if mode not in ANALYSIS_IDENTITY_RESELECT_MODES:
        raise ValueError(
            "analysis_identity_reselect_mode 必须是 off、shadow 或 enforce"
        )
    return mode


def _extract_recall_headings(original_text: str) -> tuple[str, ...]:
    """从正文提取最多 64 条短标题信号，不把长正文行重复塞入召回查询。"""
    headings: list[str] = []
    heading_pattern = re.compile(
        r"^(?:#{1,6}\s+|第[一二三四五六七八九十百0-9]+[章节篇部]\s*|"
        r"(?:\d{1,3}|[一二三四五六七八九十]+)(?:[.、．]\d{0,3})*[.、．)）]?\s*)"
    )
    for raw_line in original_text.splitlines():
        line = raw_line.strip()
        if not line or len(line) > 160:
            continue
        if heading_pattern.match(line) or (
                len(line) <= 80
                and line.endswith(("章", "节", "概述", "简介", "说明", "要求", "范围"))
        ):
            if line not in headings:
                headings.append(line)
        if len(headings) >= 64:
            break
    return tuple(headings)


def _build_analysis_architecture_signals(
    *,
    file_name: str,
    original_name: str,
    original_text: str,
    title_override: str = "",
) -> DocumentArchitectureSignals:
    return build_document_architecture_signals(
        filename=file_name,
        original_filename=original_name,
        title=title_override or _extract_title(original_text),
        headings=_extract_recall_headings(original_text),
        body=original_text,
    )


def _data_standard_candidate_scope(
    *,
    tree_index: ArchitectureTreeIndex,
    architecture_list: Iterable[Dict[str, Any]],
) -> tuple[tuple[int, ...], dict[int, str]]:
    scope_ids = tuple(_ordered_data_standard_leaf_ids(architecture_list))
    remark_overrides = {
        node_id: remark
        for node_id in scope_ids
        for remark in (
            data_standard_candidate_remark(
                tree_index.require(node_id).semantic_path
            ),
        )
        if remark
    }
    return scope_ids, remark_overrides


def _architecture_signal_digest(signals: DocumentArchitectureSignals) -> str:
    serialized = json.dumps(
        {
            "filename": signals.filename,
            "originalFilename": signals.original_filename,
            "title": signals.title,
            "headings": signals.headings,
            "identifiers": signals.identifiers,
            "bodyExcerpt": signals.body_excerpt,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _node_prompt_projection(node: ArchitectureNodeProfile) -> Dict[str, Any]:
    projected: Dict[str, Any] = {
        "id": node.id,
        "pathName": node.semantic_path,
        "nodeType": "leaf" if node.is_leaf else "parent",
    }
    if node.remark:
        projected["remark"] = node.remark[:512]
    return projected


def _normalize_bounded_analysis_prompt(prompt: str) -> str:
    normalized = normalize_rag_prompt(prompt)
    if len(normalized) > MAX_ANALYSIS_PROMPT_CHARS:
        raise ArchitecturePromptBudgetError(
            f"模型 Prompt 共 {len(normalized)} 字符，超过 "
            f"{MAX_ANALYSIS_PROMPT_CHARS} 字符上限"
        )
    return normalized


def _validate_topk_architecture_id(
        raw_id: Any,
        *,
        visible_ids: set[int],
        tree_index: ArchitectureTreeIndex,
        architecture_list: Iterable[Dict[str, Any]],
) -> int:
    if isinstance(raw_id, bool) or not isinstance(raw_id, int):
        raise ArchitectureContractError("architectureId 必须是数字 ID")
    if raw_id not in visible_ids or raw_id not in tree_index.nodes_by_id:
        raise ArchitectureContractError("architectureId 不属于模型可见候选")
    node = tree_index.require(raw_id)
    if node.parent_id is None:
        raise ArchitectureContractError("领域树根节点不能作为最终分类")
    if not node.is_leaf:
        if _is_data_standard_parent_id(raw_id, architecture_list):
            raise DataStandardParentContractError(
                "architectureId 是数据标准父节点，必须兜底到其下叶子节点"
            )
    return raw_id


def _parse_topk_classification_result(
        raw_result: Any,
        *,
        visible_ids: set[int],
        tree_index: ArchitectureTreeIndex,
        architecture_list: Iterable[Dict[str, Any]],
) -> tuple[Dict[str, Any], int]:
    parsed = _parse_strict_json_object(raw_result)
    if parsed is None:
        raise ArchitectureContractError("分类结果不是严格 JSON 对象")
    if set(parsed) != {"architectureId"}:
        raise ArchitectureContractError("分类结果只能包含 architectureId")
    if parsed.get("architectureId") is None:
        raise ArchitectureContractError("architectureId 为 null，证据不足")
    return parsed, _validate_topk_architecture_id(
        parsed.get("architectureId"),
        visible_ids=visible_ids,
        tree_index=tree_index,
        architecture_list=architecture_list,
    )


def _visible_data_standard_fallback_id(
        *,
        visible_ids: set[int],
        architecture_list: Iterable[Dict[str, Any]],
        force: bool,
        context_values: Iterable[Any],
) -> int | None:
    if not force and not _contains_gjb_standard_reference(*context_values):
        return None
    node_id = _general_data_standard_leaf_id(architecture_list)
    return node_id if node_id in visible_ids else None


_STRONG_FILENAME_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"([A-Za-z]{1,12}(?:[/_]+[A-Za-z]{1,12})?[/_\-\s]*"
    r"\d{1,8}[A-Za-z]?)"
    r"(?![A-Za-z0-9])"
)
_FILENAME_DATE_IDENTIFIER_PREFIXES = frozenset(
    {
        "jan", "january", "feb", "february", "mar", "march",
        "apr", "april", "may", "jun", "june", "jul", "july",
        "aug", "august", "sep", "sept", "september", "oct", "october",
        "nov", "november", "dec", "december",
    }
)
_EQUIPMENT_DETAIL_KINDS = frozenset(
    (
        "基础数据",
        "战技指标",
        "运用数据",
        "效能数据",
        "模型数据",
        "目特数据",
        "声像数据",
    )
)
_ORDERED_EQUIPMENT_DETAIL_KINDS = (
    "基础数据",
    "战技指标",
    "运用数据",
    "效能数据",
    "模型数据",
    "目特数据",
    "声像数据",
)
_STRONG_GJB_FILENAME_RE = re.compile(
    r"(?<![A-Za-z0-9])GJB(?:\s*[/_-]\s*Z)?\s*[- ]?\s*\d+[A-Za-z]?",
    re.IGNORECASE,
)
_GJB_STANDARD_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<prefix>GJB(?:\s*[/_-]?\s*Z)?)"
    r"\s*[- ]?\s*(?P<number>\d{1,8}[A-Za-z]?)"
    r"(?:\s*-\s*(?P<year>\d{4}))?",
    re.IGNORECASE,
)
_STANDARD_COMMENTARY_TITLE_RE = re.compile(
    r"(?:标准解读|标准释义|宣贯材料|培训材料|实施说明|编制说明|"
    r"审核报告|检测报告|检验报告|符合性(?:评价|报告))"
)
_STANDARD_STRUCTURE_MARKERS = (
    "范围",
    "规范性引用文件",
    "术语和定义",
    "术语与定义",
)
_JANE_COPYRIGHT_RE = re.compile(
    r"©\s*\d{4}\s+Jane[’']s\s+Group\s+UK\s+Limited",
    re.IGNORECASE,
)
_JANE_PAGE_ONE_RE = re.compile(
    r"(?im)^\s*Page\s+1\s+of\s+(?P<total_pages>\d+)\s*$",
)
_JANE_METADATA_RE = re.compile(
    r"(?im)^\s*(?:Date\s+Posted|Publication)\s*:",
)
_SCOPE_QUALIFIER_RE = re.compile(
    r"\b(?P<kind>Flight|Block|Batch)\s*"
    r"(?P<value>(?:"
    r"[0-9]+[A-Z]?(?:\s*(?:[/_-]\s*)?[IVXLCDM]+[A-Z]?)?"
    r"|[IVXLCDM]+[A-Z]?(?:\s*[/_-]\s*[IVXLCDM]+[A-Z]?)?"
    r"))(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_JANE_CLASS_RE = re.compile(r"(?<![A-Za-z])class(?![A-Za-z])", re.IGNORECASE)
_JANE_AIRCRAFT_TOTALS_RE = re.compile(
    r"(?im)^\s*Aircraft\s+totals\s*$",
)
_JANE_CATALOG_FILENAME_RE = re.compile(
    r"^(?:jfs|jaem|jawa|jumv)(?=[a-z0-9_-]*\d)[a-z0-9_-]+$",
    re.IGNORECASE,
)
_OPAQUE_IDENTITY_FILENAME_RE = re.compile(
    r"^(?:"
    r"[0-9a-f]{8,64}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?:upload|hash|temporary|temp)[_-][a-z0-9_-]{6,}"
    r"|technical[_-]upload(?:[_-][a-z0-9_-]+)?"
    r")$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _DataStandardClassificationProfile:
    active: bool = False
    standard_number: str = ""
    title: str = ""
    document_kind: str = "unknown"
    filename_identifiers: tuple[str, ...] = ()
    cover_identifiers: tuple[str, ...] = ()
    evidence_sources: tuple[str, ...] = ()
    identity_confirmed: bool = False
    identity_conflict: bool = False


def _normalized_gjb_source_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _as_text(value))
    return re.sub(r"[\-‐‑‒–—―－﹣]+", "-", normalized)


def _extract_gjb_standard_identifiers(
    *values: Any,
) -> tuple[tuple[str, str], ...]:
    """返回 ``(identity_key, display_number)``，用于文件名与首页双源核验。"""

    result: list[tuple[str, str]] = []
    for value in values:
        normalized = _normalized_gjb_source_text(value)
        for match in _GJB_STANDARD_NUMBER_RE.finditer(normalized):
            prefix = re.sub(
                r"[\s/_-]+",
                "",
                match.group("prefix"),
            ).upper()
            number = match.group("number").upper()
            year = match.group("year") or ""
            identity_key = f"{prefix}{number}{year}"
            display_prefix = "GJB/Z" if prefix == "GJBZ" else "GJB"
            display = f"{display_prefix} {number}"
            if year:
                display += f"-{year}"
            item = (identity_key, display)
            if item not in result:
                result.append(item)
    return tuple(result)


def _extract_data_standard_title(original_text: str) -> str:
    """从标准封面前部提取正式标题，不改动其他文档共用的通用标题解析。"""

    opening = _opening_text(original_text, max_chars=8_000, max_lines=160)
    if not opening:
        return ""
    for raw_line in opening.splitlines():
        normalized_line = _normalized_gjb_source_text(raw_line)
        line_without_number = _GJB_STANDARD_NUMBER_RE.sub(
            "",
            normalized_line,
        )
        candidate = re.sub(r"\s+", " ", line_without_number).strip(" -")
        if not candidate or not re.search(r"[\u4e00-\u9fff]", candidate):
            continue
        if candidate.startswith("FL ") or candidate.startswith("代替"):
            continue
        if candidate in {"中华人民共和国国家军用标准", "国家军用标准", "国军标"}:
            continue
        if any(
            marker in candidate
            for marker in (
                "发布",
                "实施",
                "颁布",
                "批准",
                "目次",
                "目录",
            )
        ):
            continue
        if re.match(r"^(?:第?[一二三四五六七八九十百0-9]+[章节篇部.]|\d+\s)", candidate):
            continue
        if len(candidate) <= 120:
            return candidate[:120]
    return ""


def _build_data_standard_classification_profile(
    *,
    file_name: str,
    original_name: str,
    original_text: str,
) -> _DataStandardClassificationProfile:
    identity_filename = _as_text(original_name) or _as_text(file_name)
    filename_items = _extract_gjb_standard_identifiers(identity_filename)
    opening = _opening_text(original_text, max_chars=8_000, max_lines=160)
    cover_items = _extract_gjb_standard_identifiers(opening)
    filename_keys = {item[0] for item in filename_items}
    cover_keys = {item[0] for item in cover_items}
    shared_keys = filename_keys & cover_keys
    identity_conflict = bool(
        filename_keys
        and cover_keys
        and not shared_keys
    )
    title = _extract_data_standard_title(original_text)
    header_present = (
        "国家军用标准" in opening
        or "国军标" in opening
    )
    structure_markers = tuple(
        marker
        for marker in _STANDARD_STRUCTURE_MARKERS
        if marker in opening
    )
    commentary = bool(
        _STANDARD_COMMENTARY_TITLE_RE.search(
            "\n".join((title, opening[:1_000]))
        )
    )
    # 同号可能只是正文引用，任意中文首行也不能充当标准封面证据。宁可保持旧链路，
    # 也只在军用标准页眉或至少两个标准结构章节存在时启用受限候选。
    cover_evidence = bool(
        header_present
        or len(structure_markers) >= 2
    )
    identity_confirmed = bool(
        not identity_conflict
        and not commentary
        and (
            (shared_keys and cover_evidence)
            or (
                cover_keys
                and header_present
                and (title or len(structure_markers) >= 2)
            )
        )
    )
    evidence_sources: list[str] = []
    if filename_items:
        evidence_sources.append("originalFileName")
    if cover_items:
        evidence_sources.append("coverIdentifier")
    if header_present:
        evidence_sources.append("coverStandardHeader")
    if title:
        evidence_sources.append("coverTitle")
    if len(structure_markers) >= 2:
        evidence_sources.append("standardStructure")

    display_number = ""
    for key, display in cover_items:
        if not shared_keys or key in shared_keys:
            display_number = display
            break
    if not display_number and filename_items:
        display_number = filename_items[0][1]
    if commentary:
        document_kind = "commentary"
    elif identity_confirmed:
        document_kind = "standard_body"
    elif filename_items or cover_items:
        document_kind = "reference_only"
    else:
        document_kind = "unknown"
    return _DataStandardClassificationProfile(
        active=bool(filename_items or cover_items),
        standard_number=display_number,
        title=title,
        document_kind=document_kind,
        filename_identifiers=tuple(item[1] for item in filename_items),
        cover_identifiers=tuple(item[1] for item in cover_items),
        evidence_sources=tuple(evidence_sources),
        identity_confirmed=identity_confirmed,
        identity_conflict=identity_conflict,
    )


def _data_standard_prompt_context(
    profile: _DataStandardClassificationProfile,
) -> dict[str, Any]:
    return {
        "standardNumber": profile.standard_number,
        "standardTitle": profile.title,
        "documentKind": profile.document_kind,
        "evidenceSources": list(profile.evidence_sources),
    }


@dataclass(frozen=True, slots=True)
class _JaneClassificationProfile:
    active: bool = False
    title: str = ""
    identity_filename: str = ""
    filename_identity_kind: str = "absent"
    filename_identifiers: tuple[str, ...] = ()
    trusted_filename_identifiers: tuple[str, ...] = ()
    title_identifiers: tuple[str, ...] = ()
    primary_identifier: str = ""
    qualifier: str = ""
    scope_kind: str = ""
    high_level_branch_hint: str = ""
    dominant_detail_kind: str = ""
    recall_identity_enabled: bool = False
    identity_confirmed: bool = False
    identity_conflict: bool = False


@dataclass(frozen=True, slots=True)
class _EquipmentIdentityReselectProfile:
    """候选召回之外建立的普通装备双证据身份快照。"""

    active: bool = False
    filename_identity_kind: str = "absent"
    filename_identifiers: tuple[str, ...] = ()
    opening_identifiers: tuple[str, ...] = ()
    shared_identifiers: tuple[str, ...] = ()
    conflicting_identifiers: tuple[str, ...] = ()
    identifier: str = ""
    target_parent_id: int | None = None
    target_parent_path: str = ""
    candidate_ids: tuple[int, ...] = ()
    evidence_sources: tuple[str, ...] = ()
    reason_code: str = "no_explicit_original_filename"


@dataclass(frozen=True, slots=True)
class _IdentityReselectGateDecision:
    should_reselect: bool = False
    relation: str = "not_evaluated"
    reason_code: str = "profile_inactive"


def _jane_recall_filename_signals(
    *,
    file_name: str,
    original_name: str,
    profile: _JaneClassificationProfile,
    scope_guard_active: bool,
) -> tuple[str, str]:
    """返回可进入召回的业务文件名和原文件名。"""

    if not scope_guard_active:
        return file_name, original_name
    if profile.filename_identity_kind in {"catalog", "opaque"}:
        return "", ""
    if original_name:
        return "", original_name
    return file_name, ""


@dataclass(frozen=True, slots=True)
class _ArchitectureScopeResolution:
    matched_scope_parent_id: int | None = None
    matched_branch_parent_id: int | None = None
    clustered_parent_ids: tuple[int, ...] = ()
    protected_parent_reasons: tuple[tuple[int, tuple[str, ...]], ...] = ()
    reason_code: str = "no_constraint_insufficient_evidence"
    tree_gap: bool = False

    @property
    def preferred_parent_reasons(self) -> dict[int, tuple[str, ...]]:
        return dict(self.protected_parent_reasons)


@dataclass(frozen=True, slots=True)
class _ArchitectureConstraintDecision:
    pre_architecture_id: int
    post_architecture_id: int
    reason_code: str
    matched_scope_parent_id: int | None
    tree_gap: bool


def _ordered_strong_identifiers(
    *values: Any,
    limit: int = 128,
) -> tuple[str, ...]:
    """按文本出现顺序提取数字型标识，过滤日期和版式限定词。"""

    identifiers: list[str] = []
    for value in values:
        normalized = unicodedata.normalize("NFKC", _as_text(value)).casefold()
        for match in _STRONG_FILENAME_IDENTIFIER_RE.finditer(normalized):
            raw_identifier = match.group(1).strip()
            compact = re.sub(r"[^a-z0-9]+", "", raw_identifier)
            letters = re.sub(r"[^a-z]+", "", compact)
            digits = re.sub(r"[^0-9]+", "", compact)
            if not letters or not digits:
                continue
            # 单字母紧凑写法（如正文序号 B2）过于宽泛；带显式分隔符的 F-35 仍保留。
            if len(letters) == 1 and re.fullmatch(r"[a-z]\d+", raw_identifier):
                continue
            if letters in _FILENAME_DATE_IDENTIFIER_PREFIXES:
                continue
            if letters in {"flight", "block", "batch", "page", "figure"}:
                continue
            if compact not in identifiers:
                identifiers.append(compact)
            if len(identifiers) >= limit:
                return tuple(identifiers)
    return tuple(identifiers)


def _strong_filename_identifiers(*values: Any) -> set[str]:
    """提取文件名中的数字型强标识，忽略正文、短词和纯年份。"""

    return set(_ordered_strong_identifiers(*values))


def _jane_filename_identity_kind(value: Any) -> str:
    """区分描述性文件名与不能代表装备身份的出版/上传技术名。"""

    normalized = unicodedata.normalize("NFKC", _as_text(value)).casefold()
    basename = re.split(r"[/\\]", normalized)[-1].strip()
    if not basename:
        return "absent"
    stem = re.sub(r"\.[a-z0-9]{1,12}$", "", basename).strip()
    if not stem:
        return "absent"
    if _JANE_CATALOG_FILENAME_RE.fullmatch(stem):
        return "catalog"
    if _OPAQUE_IDENTITY_FILENAME_RE.fullmatch(stem):
        return "opaque"
    return "descriptive"


def _identifier_prefix(identifier: str) -> str:
    match = re.match(r"[a-z]+", identifier.casefold())
    return match.group(0) if match else ""


def _extract_scope_qualifier(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _as_text(value))
    match = _SCOPE_QUALIFIER_RE.search(normalized)
    if not match:
        return ""
    kind = match.group("kind").casefold()
    kind_label = {
        "flight": "Flight",
        "block": "Block",
        "batch": "Batch",
    }[kind]
    qualifier_value = re.sub(
        r"\s+",
        " ",
        match.group("value").upper().strip(),
    )
    return f"{kind_label} {qualifier_value}"


def _normalize_scope_qualifier(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _as_text(value)).casefold()
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _extract_jane_title(original_text: str) -> tuple[bool, str]:
    """只在可靠 Jane's 首页版式中抽取版权声明之后的真实标题。"""

    if not original_text or not _JANE_COPYRIGHT_RE.search(original_text):
        return False, ""
    page_match = _JANE_PAGE_ONE_RE.search(original_text)
    if page_match is None:
        return False, ""
    metadata_match = _JANE_METADATA_RE.search(original_text, page_match.end())
    if metadata_match is None:
        return False, ""
    title_lines = [
        line.strip()
        for line in original_text[page_match.end():metadata_match.start()].splitlines()
        if line.strip()
    ]
    title = re.sub(r"\s+", " ", " ".join(title_lines)).strip()[:512]
    return bool(title), title


def _jane_dominant_detail_kind(original_text: str) -> str:
    """识别整份简氏资料是否由单一明细章节主导，避免把普通目录章节误当全文作用域。"""

    page_match = _JANE_PAGE_ONE_RE.search(original_text)
    if page_match is None:
        return ""
    try:
        total_pages = int(page_match.group("total_pages"))
    except (TypeError, ValueError):
        return ""
    # 只对极短资料做这一强判断。长篇综合资料即使包含 Specifications，也只能
    # 由分类模型结合全文决定，不能把普通目录章节提升为全文作用域。
    if total_pages > 3:
        return ""
    metadata_match = _JANE_METADATA_RE.search(original_text, page_match.end())
    if metadata_match is None:
        return ""
    first_page_end = re.search(
        r"(?im)^\s*Page\s+2\s+of\s+\d+\s*$",
        original_text[metadata_match.end():],
    )
    first_page_body = original_text[
        metadata_match.end():
        (
            metadata_match.end() + first_page_end.start()
            if first_page_end is not None
            else len(original_text)
        )
    ]
    lines = [
        line.strip().casefold()
        for line in first_page_body.splitlines()
        if line.strip()
    ]
    try:
        specifications_index = lines.index("specifications")
    except ValueError:
        return ""
    if "contents" in lines[:specifications_index]:
        return ""
    return "technical_specifications"


def _build_jane_classification_profile(
    *,
    file_name: str,
    original_name: str,
    original_text: str,
) -> _JaneClassificationProfile:
    active, title = _extract_jane_title(original_text)
    identity_filename = _as_text(original_name) or _as_text(file_name)
    filename_identity_kind = _jane_filename_identity_kind(identity_filename)
    if not active:
        return _JaneClassificationProfile(
            identity_filename=identity_filename,
            filename_identity_kind=filename_identity_kind,
        )

    filename_identifiers = _ordered_strong_identifiers(identity_filename)
    trusted_filename_identifiers = (
        filename_identifiers
        if filename_identity_kind == "descriptive"
        else ()
    )
    title_identifiers = _ordered_strong_identifiers(title)
    shared_identifiers = [
        identifier
        for identifier in title_identifiers
        if identifier in set(trusted_filename_identifiers)
    ]
    filename_qualifier = (
        _extract_scope_qualifier(identity_filename)
        if filename_identity_kind == "descriptive"
        else ""
    )
    title_qualifier = _extract_scope_qualifier(title)
    identifier_conflict = bool(
        trusted_filename_identifiers
        and title_identifiers
        and not shared_identifiers
    )
    qualifier_conflict = bool(
        filename_qualifier
        and title_qualifier
        and _normalize_scope_qualifier(filename_qualifier)
        != _normalize_scope_qualifier(title_qualifier)
    )
    identity_conflict = identifier_conflict or qualifier_conflict
    if shared_identifiers:
        primary_identifier = shared_identifiers[0]
    elif title_identifiers and not trusted_filename_identifiers:
        # 可靠 Jane's 首页标题可以保护召回候选；没有可信文件名独立佐证时，
        # 仍不得据此开启最终确定性覆盖。
        primary_identifier = title_identifiers[0]
    else:
        primary_identifier = ""
    qualifier = title_qualifier or filename_qualifier
    qualifier_kind = qualifier.partition(" ")[0].casefold()
    if qualifier_kind == "flight":
        scope_kind = "flight"
    elif qualifier_kind in {"block", "batch"}:
        scope_kind = "block"
    elif _JANE_CLASS_RE.search(title):
        scope_kind = "class"
    else:
        scope_kind = "single_model"
    high_level_branch_hint = (
        "air_equipment"
        if _JANE_AIRCRAFT_TOTALS_RE.search(_opening_text(original_text))
        else ""
    )
    dominant_detail_kind = (
        _jane_dominant_detail_kind(original_text)
        if scope_kind == "single_model"
        else ""
    )
    return _JaneClassificationProfile(
        active=True,
        title=title,
        identity_filename=identity_filename,
        filename_identity_kind=filename_identity_kind,
        filename_identifiers=filename_identifiers,
        trusted_filename_identifiers=trusted_filename_identifiers,
        title_identifiers=title_identifiers,
        primary_identifier=primary_identifier,
        qualifier=qualifier,
        scope_kind=scope_kind,
        high_level_branch_hint=high_level_branch_hint,
        dominant_detail_kind=dominant_detail_kind,
        recall_identity_enabled=bool(primary_identifier) and not identity_conflict,
        identity_confirmed=bool(shared_identifiers) and not identity_conflict,
        identity_conflict=identity_conflict,
    )


def _has_strong_gjb_filename_identity(*values: Any) -> bool:
    for value in values:
        normalized = unicodedata.normalize("NFKC", _as_text(value))
        if not normalized:
            continue
        if "国家军用标准" in normalized or "国军标" in normalized:
            return True
        if _STRONG_GJB_FILENAME_RE.search(normalized):
            return True
    return False


def _equipment_detail_kind(node_name: str, parent_name: str) -> str:
    if node_name in _EQUIPMENT_DETAIL_KINDS:
        return node_name
    for separator in ("-", "－", "—", "–", "﹣"):
        prefix = f"{parent_name}{separator}"
        if node_name.startswith(prefix):
            return node_name[len(prefix):].strip()
    return ""


def _has_seven_equipment_detail_leaves(
    node: ArchitectureNodeProfile,
    tree_index: ArchitectureTreeIndex,
) -> bool:
    detail_kinds = {
        _equipment_detail_kind(tree_index.require(child_id).name, node.name)
        for child_id in tree_index.children_by_id[node.id]
        if tree_index.require(child_id).is_leaf
    }
    return _EQUIPMENT_DETAIL_KINDS.issubset(detail_kinds)


def _equipment_entities_by_identifier(
    tree_index: ArchitectureTreeIndex,
) -> dict[str, tuple[int, ...]]:
    values: dict[str, list[int]] = {}
    for node in tree_index.nodes:
        if node.is_leaf or not _has_seven_equipment_detail_leaves(node, tree_index):
            continue
        for identifier in _ordered_strong_identifiers(node.name, limit=8):
            values.setdefault(identifier, []).append(node.id)
    return {
        identifier: tuple(dict.fromkeys(node_ids))
        for identifier, node_ids in values.items()
    }


def _ordered_equipment_family_scope_ids(
    parent_id: int,
    tree_index: ArchitectureTreeIndex,
) -> tuple[int, ...]:
    """返回七类明细叶与父节点；拓扑不唯一时拒绝建立受限分支。"""

    parent = tree_index.get(parent_id)
    if parent is None or parent.is_leaf:
        return ()
    children_by_kind: dict[str, list[int]] = {
        kind: [] for kind in _ORDERED_EQUIPMENT_DETAIL_KINDS
    }
    for child_id in tree_index.children_by_id[parent.id]:
        child = tree_index.require(child_id)
        if not child.is_leaf:
            continue
        detail_kind = _equipment_detail_kind(child.name, parent.name)
        if detail_kind in children_by_kind:
            children_by_kind[detail_kind].append(child.id)
    if any(len(children_by_kind[kind]) != 1 for kind in children_by_kind):
        return ()
    return tuple(
        children_by_kind[kind][0]
        for kind in _ORDERED_EQUIPMENT_DETAIL_KINDS
    ) + (parent.id,)


def _build_equipment_identity_reselect_profile(
    *,
    requested_original_name: str,
    original_text: str,
    tree_index: ArchitectureTreeIndex,
    visible_ids: set[int],
    jane_active: bool,
    data_standard_active: bool,
) -> _EquipmentIdentityReselectProfile:
    """用显式原文件名和 opening 身份区确认普通装备的唯一树分支。"""

    original_name = _as_text(requested_original_name)
    filename_identity_kind = _jane_filename_identity_kind(original_name)
    if not original_name:
        return _EquipmentIdentityReselectProfile()
    if jane_active:
        return _EquipmentIdentityReselectProfile(
            filename_identity_kind=filename_identity_kind,
            reason_code="jane_scope_owned",
        )
    if data_standard_active:
        return _EquipmentIdentityReselectProfile(
            filename_identity_kind=filename_identity_kind,
            reason_code="data_standard_scope_owned",
        )
    if filename_identity_kind != "descriptive":
        return _EquipmentIdentityReselectProfile(
            filename_identity_kind=filename_identity_kind,
            reason_code="filename_not_descriptive",
        )

    entities_by_identifier = _equipment_entities_by_identifier(tree_index)
    filename_identifiers = tuple(
        identifier
        for identifier in _ordered_strong_identifiers(original_name)
        if entities_by_identifier.get(identifier)
    )
    if not filename_identifiers:
        return _EquipmentIdentityReselectProfile(
            filename_identity_kind=filename_identity_kind,
            reason_code="filename_identifier_not_in_tree",
        )
    target_parent_ids = tuple(
        dict.fromkeys(
            parent_id
            for identifier in filename_identifiers
            for parent_id in entities_by_identifier[identifier]
        )
    )
    if len(target_parent_ids) != 1:
        return _EquipmentIdentityReselectProfile(
            filename_identity_kind=filename_identity_kind,
            filename_identifiers=filename_identifiers,
            reason_code="filename_identity_ambiguous",
        )

    target_parent_id = target_parent_ids[0]
    filename_prefixes = {
        prefix
        for prefix in map(_identifier_prefix, filename_identifiers)
        if prefix
    }
    opening_identifiers = tuple(
        identifier
        for identifier in _ordered_strong_identifiers(
            _opening_identity_evidence_text(
                original_text,
                original_name,
                max_chars=2000,
                max_lines=80,
            )
        )
        if (
            _identifier_prefix(identifier) in filename_prefixes
            and entities_by_identifier.get(identifier)
        )
    )
    shared_identifiers = tuple(
        identifier
        for identifier in filename_identifiers
        if identifier in set(opening_identifiers)
    )
    conflicting_identifiers = tuple(
        identifier
        for identifier in opening_identifiers
        if any(
            parent_id != target_parent_id
            for parent_id in entities_by_identifier[identifier]
        )
    )
    common_fields = {
        "filename_identity_kind": filename_identity_kind,
        "filename_identifiers": filename_identifiers,
        "opening_identifiers": opening_identifiers,
        "shared_identifiers": shared_identifiers,
        "conflicting_identifiers": conflicting_identifiers,
        "target_parent_id": target_parent_id,
        "target_parent_path": tree_index.require(target_parent_id).semantic_path,
    }
    if conflicting_identifiers:
        return _EquipmentIdentityReselectProfile(
            **common_fields,
            reason_code="opening_identity_conflict",
        )
    if not shared_identifiers:
        return _EquipmentIdentityReselectProfile(
            **common_fields,
            reason_code="independent_identity_missing",
        )

    candidate_ids = _ordered_equipment_family_scope_ids(
        target_parent_id,
        tree_index,
    )
    if len(candidate_ids) != 8:
        return _EquipmentIdentityReselectProfile(
            **common_fields,
            reason_code="equipment_family_topology_invalid",
        )
    if not set(candidate_ids).issubset(visible_ids):
        return _EquipmentIdentityReselectProfile(
            **common_fields,
            candidate_ids=candidate_ids,
            reason_code="equipment_family_scope_incomplete",
        )
    return _EquipmentIdentityReselectProfile(
        active=True,
        **common_fields,
        identifier=shared_identifiers[0],
        candidate_ids=candidate_ids,
        evidence_sources=("originalFileName", "openingIdentity"),
        reason_code="identity_confirmed",
    )


def _scope_parent_clusters(
    profile: _JaneClassificationProfile,
    original_text: str,
    tree_index: ArchitectureTreeIndex,
    entities_by_identifier: Mapping[str, Sequence[int]],
) -> tuple[tuple[int, tuple[int, ...]], ...]:
    primary_prefix = _identifier_prefix(profile.primary_identifier)
    if not primary_prefix:
        return ()

    parent_members: dict[int, list[int]] = {}
    body_identifiers = _ordered_strong_identifiers(
        original_text[:20_000],
        limit=256,
    )
    for identifier in body_identifiers:
        if _identifier_prefix(identifier) != primary_prefix:
            continue
        for entity_id in entities_by_identifier.get(identifier, ()):
            entity = tree_index.require(entity_id)
            parent_id = entity.parent_id
            if parent_id not in tree_index.nodes_by_id:
                continue
            members = parent_members.setdefault(parent_id, [])
            if entity_id not in members:
                members.append(entity_id)

    clusters = [
        (parent_id, tuple(member_ids))
        for parent_id, member_ids in parent_members.items()
        if len(member_ids) >= 2
    ]
    clusters.sort(key=lambda item: tree_index.require(item[0]).ordinal)
    return tuple(clusters)


def _parent_matches_scope_qualifier(
    parent: ArchitectureNodeProfile,
    qualifier: str,
) -> bool:
    normalized_qualifier = _normalize_scope_qualifier(qualifier)
    for prefix in ("flight", "block", "batch"):
        if normalized_qualifier.startswith(prefix):
            normalized_qualifier = normalized_qualifier[len(prefix):]
            break
    if not normalized_qualifier:
        return False
    normalized_parent = re.sub(
        r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+",
        "",
        unicodedata.normalize("NFKC", parent.name).casefold(),
    )
    return normalized_qualifier in normalized_parent


def _parent_declares_scope_qualifier(
    parent: ArchitectureNodeProfile,
) -> bool:
    normalized = unicodedata.normalize("NFKC", parent.name).casefold()
    return bool(
        re.search(r"(?:flight|block)\s*[0-9ivxlcdm]", normalized)
        or re.search(r"[ivxlcdm]+[a-z]?\s*型", normalized)
        or re.search(
            r"第[一二三四五六七八九十百0-9]+\s*批次",
            normalized,
        )
    )


def _unique_air_equipment_parent_id(
    tree_index: ArchitectureTreeIndex,
) -> int | None:
    matches = [
        node.id
        for node in tree_index.nodes
        if (
            not node.is_leaf
            and node.parent_id is not None
            and unicodedata.normalize("NFKC", node.name).strip() == "空中装备"
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _resolve_jane_architecture_scope(
    profile: _JaneClassificationProfile,
    *,
    original_text: str,
    tree_index: ArchitectureTreeIndex,
) -> _ArchitectureScopeResolution:
    if not profile.active:
        return _ArchitectureScopeResolution()
    if profile.identity_conflict:
        return _ArchitectureScopeResolution(reason_code="no_constraint_conflict")
    if not profile.recall_identity_enabled:
        return _ArchitectureScopeResolution()

    entities_by_identifier = _equipment_entities_by_identifier(tree_index)
    primary_entity_ids = tuple(
        dict.fromkeys(
            entities_by_identifier.get(profile.primary_identifier, ())
        )
    )
    if profile.scope_kind in {"class", "flight", "block"}:
        clusters = _scope_parent_clusters(
            profile,
            original_text,
            tree_index,
            entities_by_identifier,
        )
        clustered_parent_ids = tuple(parent_id for parent_id, _members in clusters)
        if not clustered_parent_ids:
            return _ArchitectureScopeResolution()

        qualifier_matches = tuple(
            parent_id
            for parent_id in clustered_parent_ids
            if _parent_matches_scope_qualifier(
                tree_index.require(parent_id),
                profile.qualifier,
            )
        )
        primary_parent_ids = tuple(
            dict.fromkeys(
                tree_index.require(entity_id).parent_id
                for entity_id in primary_entity_ids
                if tree_index.require(entity_id).parent_id
                in tree_index.nodes_by_id
            )
        )
        selected_parent_id: int | None = None
        tree_gap = False
        reason_code = "jane_scope_parent"
        if len(qualifier_matches) == 1:
            selected_parent_id = qualifier_matches[0]
        elif (
            len(clustered_parent_ids) == 1
            and profile.scope_kind in {"flight", "block"}
            and not _parent_declares_scope_qualifier(
                tree_index.require(clustered_parent_ids[0])
            )
        ):
            # 部分树只建总类节点而未把 Jane's Flight 0/Block 限定写进节点名。
            # 仅当父节点本身没有声明批次时才允许降级；若父节点已声明了
            # 不匹配的 Flight/Block/批次，即使主型号挂在该父下也不能选中。
            selected_parent_id = clustered_parent_ids[0]
        elif (
            len(clustered_parent_ids) == 1
            and profile.scope_kind == "class"
        ):
            selected_parent_id = clustered_parent_ids[0]
        elif profile.scope_kind == "class":
            lead_matches = tuple(
                parent_id
                for parent_id in primary_parent_ids
                if parent_id in clustered_parent_ids
            )
            if len(lead_matches) == 1:
                selected_parent_id = lead_matches[0]
                tree_gap = True
                reason_code = "jane_tree_gap_lead_parent"

        if selected_parent_id is None:
            return _ArchitectureScopeResolution(
                clustered_parent_ids=clustered_parent_ids,
            )
        return _ArchitectureScopeResolution(
            matched_scope_parent_id=selected_parent_id,
            matched_branch_parent_id=selected_parent_id,
            clustered_parent_ids=clustered_parent_ids,
            protected_parent_reasons=(
                (selected_parent_id, (reason_code,)),
            ),
            reason_code=reason_code,
            tree_gap=tree_gap,
        )

    if len(primary_entity_ids) == 1:
        branch_parent_id = primary_entity_ids[0]
        return _ArchitectureScopeResolution(
            matched_scope_parent_id=branch_parent_id,
            matched_branch_parent_id=branch_parent_id,
            protected_parent_reasons=(
                (branch_parent_id, ("jane_branch_guard",)),
            ),
            reason_code="jane_branch_guard",
        )

    if (
        not primary_entity_ids
        and profile.high_level_branch_hint == "air_equipment"
    ):
        air_parent_id = _unique_air_equipment_parent_id(tree_index)
        if air_parent_id is not None:
            return _ArchitectureScopeResolution(
                matched_scope_parent_id=air_parent_id,
                matched_branch_parent_id=air_parent_id,
                protected_parent_reasons=(
                    (air_parent_id, ("jane_high_level_branch",)),
                ),
                reason_code="jane_high_level_branch",
            )
    return _ArchitectureScopeResolution()


def _jane_classification_prompt_context(
    profile: _JaneClassificationProfile,
    resolution: _ArchitectureScopeResolution,
) -> dict[str, Any]:
    if not profile.active:
        return {}
    context: dict[str, Any] = {
        "title": profile.title,
        "primaryIdentifier": profile.primary_identifier,
        "qualifier": profile.qualifier,
        "scopeKind": profile.scope_kind,
        "highLevelBranchHint": profile.high_level_branch_hint,
        # 只有领域树已确认到具体装备实体时才把全文明细作用域交给模型。
        # MH-60R 这类仅能确认高层分支的树缺口场景不得据此猜测具体机型叶子。
        "dominantDetailKind": (
            profile.dominant_detail_kind
            if resolution.reason_code == "jane_branch_guard"
            else ""
        ),
    }
    if profile.scope_kind in {"class", "flight", "block"}:
        context["matchedScopeParentId"] = resolution.matched_scope_parent_id
        context["treeGap"] = resolution.tree_gap
    return context


def _unique_visible_equipment_identifier_parent(
        *,
        file_name: str,
        original_name: str,
        visible_ids: set[int],
        tree_index: ArchitectureTreeIndex,
        architecture_list: Iterable[Dict[str, Any]],
) -> int | None:
    """返回文件名唯一强匹配的、合法且模型可见的装备父节点。"""
    filename_identifiers = _strong_filename_identifiers(file_name, original_name)
    if not filename_identifiers:
        return None

    # 数据标准拓扑只计算一次。逐节点调用 _is_data_standard_parent_id() 会对完整
    # 6k+ 节点树反复重建拓扑，导致确定性约束退化为 O(n²)。
    topology_nodes, parent_ids = _architecture_candidate_topology(architecture_list)
    data_standard_parent_ids = _data_standard_candidate_ids(topology_nodes).intersection(
        parent_ids
    )

    matched_parent_ids: list[int] = []
    for node in tree_index.nodes:
        if (
                node.is_leaf
                or node.parent_id is None
                or node.id in data_standard_parent_ids
                or not _has_seven_equipment_detail_leaves(node, tree_index)
        ):
            continue
        node_identifiers = _strong_filename_identifiers(node.name)
        if not filename_identifiers.intersection(node_identifiers):
            continue
        matched_parent_ids.append(node.id)

    unique_ids = tuple(dict.fromkeys(matched_parent_ids))
    if len(unique_ids) != 1 or unique_ids[0] not in visible_ids:
        return None
    return unique_ids[0]


def _architecture_id_is_in_branch(
    architecture_id: int,
    branch_parent_id: int,
    tree_index: ArchitectureTreeIndex,
) -> bool:
    return (
        architecture_id == branch_parent_id
        or branch_parent_id in tree_index.ancestors_by_id[architecture_id]
    )


def _identity_reselect_relation(
    architecture_id: int,
    target_parent_id: int,
    tree_index: ArchitectureTreeIndex,
) -> str:
    if _architecture_id_is_in_branch(
        architecture_id,
        target_parent_id,
        tree_index,
    ):
        return "in_target_branch"
    if architecture_id in tree_index.ancestors_by_id[target_parent_id]:
        return "target_ancestor"

    selected = tree_index.require(architecture_id)
    target = tree_index.require(target_parent_id)
    selected_family_id = (
        selected.id
        if not selected.is_leaf
        and _has_seven_equipment_detail_leaves(selected, tree_index)
        else selected.parent_id
    )
    if (
        selected_family_id in tree_index.nodes_by_id
        and target.parent_id is not None
        and tree_index.require(selected_family_id).parent_id == target.parent_id
    ):
        return "sibling_equipment"
    return "cross_branch"


def _decide_identity_reselect_gate(
    architecture_id: int,
    *,
    profile: _EquipmentIdentityReselectProfile,
    tree_index: ArchitectureTreeIndex,
) -> _IdentityReselectGateDecision:
    if not profile.active or profile.target_parent_id is None:
        return _IdentityReselectGateDecision(reason_code=profile.reason_code)
    relation = _identity_reselect_relation(
        architecture_id,
        profile.target_parent_id,
        tree_index,
    )
    if relation == "in_target_branch":
        return _IdentityReselectGateDecision(
            relation=relation,
            reason_code="initial_result_in_target_branch",
        )
    return _IdentityReselectGateDecision(
        should_reselect=True,
        relation=relation,
        reason_code="branch_conflict_reselect",
    )


def _parse_architecture_reselect_result(
    raw_result: Any,
    *,
    scoped_ids: set[int],
    tree_index: ArchitectureTreeIndex,
    architecture_list: Iterable[Dict[str, Any]],
) -> int | None:
    parsed = _parse_strict_json_object(raw_result)
    if parsed is None or set(parsed) != {"architectureId"}:
        raise ArchitectureContractError(
            "受限重选结果必须是仅含 architectureId 的严格 JSON 对象"
        )
    if parsed["architectureId"] is None:
        return None
    return _validate_topk_architecture_id(
        parsed["architectureId"],
        visible_ids=scoped_ids,
        tree_index=tree_index,
        architecture_list=architecture_list,
    )


def _decide_topk_deterministic_architecture_constraint(
    architecture_id: int,
    *,
    file_name: str,
    original_name: str,
    visible_ids: set[int],
    tree_index: ArchitectureTreeIndex,
    architecture_list: Iterable[Dict[str, Any]],
    filename_constraint_mode: str = ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
    data_standard_mode: str = ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    data_standard_profile: _DataStandardClassificationProfile | None = None,
    jane_profile: _JaneClassificationProfile | None = None,
    scope_resolution: _ArchitectureScopeResolution | None = None,
) -> _ArchitectureConstraintDecision:
    """应用高置信文件信号约束，防止模型越过明确的数据标准或装备分支。"""

    filename_constraint_mode = _normalize_analysis_filename_constraint_mode(
        filename_constraint_mode
    )
    data_standard_mode = _normalize_analysis_data_standard_mode(
        data_standard_mode
    )
    standard_profile = (
        data_standard_profile
        or _DataStandardClassificationProfile()
    )
    profile = jane_profile or _JaneClassificationProfile()
    resolution = scope_resolution or _ArchitectureScopeResolution()
    use_data_standard_scope_guard = (
        data_standard_mode == ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD
    )
    has_confirmed_standard_identity = (
        standard_profile.identity_confirmed
        and standard_profile.document_kind == "standard_body"
    )
    use_gjb_constraint = (
        has_confirmed_standard_identity
        if use_data_standard_scope_guard
        else _has_strong_gjb_filename_identity(file_name, original_name)
    )
    if use_gjb_constraint:
        visible_standard_ids = tuple(
            node_id
            for node_id in _ordered_data_standard_leaf_ids(architecture_list)
            if node_id in visible_ids
        )
        if architecture_id in visible_standard_ids:
            constrained_id = architecture_id
            reason_code = "data_standard_model_leaf"
        else:
            general_id = _general_data_standard_leaf_id(architecture_list)
            if general_id is None or general_id not in visible_ids:
                raise ArchitectureContractError(
                    "已确认标准正文，但模型无法确定专业类别且候选中不存在通用要求叶节点"
                )
            constrained_id = general_id
            reason_code = "data_standard_general_fallback"
            logger.info(
                "GJB 文件身份约束覆盖普通分类: "
                "original_architecture_id=%s fallback_general_requirement_id=%s",
                architecture_id,
                constrained_id,
            )
        validated_id = _validate_topk_architecture_id(
            constrained_id,
            visible_ids=visible_ids,
            tree_index=tree_index,
            architecture_list=architecture_list,
        )
        return _ArchitectureConstraintDecision(
            pre_architecture_id=architecture_id,
            post_architecture_id=validated_id,
            reason_code=reason_code,
            matched_scope_parent_id=None,
            tree_gap=False,
        )

    use_scope_guard = (
        filename_constraint_mode
        == ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
        and profile.active
    )
    if use_scope_guard:
        if profile.identity_conflict:
            return _ArchitectureConstraintDecision(
                architecture_id,
                architecture_id,
                "no_constraint_conflict",
                None,
                False,
            )
        if not profile.identity_confirmed:
            return _ArchitectureConstraintDecision(
                architecture_id,
                architecture_id,
                "no_constraint_insufficient_evidence",
                resolution.matched_scope_parent_id,
                resolution.tree_gap,
            )

        matched_parent_id = resolution.matched_branch_parent_id
        if matched_parent_id is None or matched_parent_id not in visible_ids:
            return _ArchitectureConstraintDecision(
                architecture_id,
                architecture_id,
                "no_constraint_insufficient_evidence",
                resolution.matched_scope_parent_id,
                resolution.tree_gap,
            )

        if profile.scope_kind in {"class", "flight", "block"}:
            constrained_id = matched_parent_id
            reason_code = (
                "jane_tree_gap_lead_parent"
                if resolution.tree_gap
                else (
                    "jane_scope_parent"
                    if _architecture_id_is_in_branch(
                        architecture_id,
                        matched_parent_id,
                        tree_index,
                    )
                    else "jane_branch_guard"
                )
            )
        elif _architecture_id_is_in_branch(
            architecture_id,
            matched_parent_id,
            tree_index,
        ):
            constrained_id = architecture_id
            reason_code = resolution.reason_code
        else:
            constrained_id = matched_parent_id
            reason_code = resolution.reason_code

        validated_id = _validate_topk_architecture_id(
            constrained_id,
            visible_ids=visible_ids,
            tree_index=tree_index,
            architecture_list=architecture_list,
        )
        return _ArchitectureConstraintDecision(
            pre_architecture_id=architecture_id,
            post_architecture_id=validated_id,
            reason_code=reason_code,
            matched_scope_parent_id=resolution.matched_scope_parent_id,
            tree_gap=resolution.tree_gap,
        )

    if (
        filename_constraint_mode
        == ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
    ):
        # scope_guard 只允许经过正文首页识别和文件名交叉确认的 Jane 作用域约束。
        # 非 Jane 文件没有第二证据源时，文件名仍可参与召回，但不得落回 legacy
        # 的单源硬覆盖路径；需要旧行为的部署可显式选择 legacy。
        return _ArchitectureConstraintDecision(
            architecture_id,
            architecture_id,
            "no_constraint_insufficient_evidence",
            None,
            False,
        )

    matched_parent_id = _unique_visible_equipment_identifier_parent(
        file_name=file_name,
        original_name=original_name,
        visible_ids=visible_ids,
        tree_index=tree_index,
        architecture_list=architecture_list,
    )
    if matched_parent_id is None:
        return _ArchitectureConstraintDecision(
            architecture_id,
            architecture_id,
            "no_constraint_insufficient_evidence",
            None,
            False,
        )

    if _architecture_id_is_in_branch(
        architecture_id,
        matched_parent_id,
        tree_index,
    ):
        return _ArchitectureConstraintDecision(
            architecture_id,
            architecture_id,
            "legacy_identifier_parent",
            matched_parent_id,
            False,
        )

    logger.info(
        "文件名强标识约束覆盖越支分类: original_architecture_id=%s "
        "fallback_parent_id=%s",
        architecture_id,
        matched_parent_id,
    )
    validated_id = _validate_topk_architecture_id(
        matched_parent_id,
        visible_ids=visible_ids,
        tree_index=tree_index,
        architecture_list=architecture_list,
    )
    return _ArchitectureConstraintDecision(
        pre_architecture_id=architecture_id,
        post_architecture_id=validated_id,
        reason_code="legacy_identifier_parent",
        matched_scope_parent_id=matched_parent_id,
        tree_gap=False,
    )


def _apply_topk_deterministic_architecture_constraints(
    architecture_id: int,
    *,
    file_name: str,
    original_name: str,
    visible_ids: set[int],
    tree_index: ArchitectureTreeIndex,
    architecture_list: Iterable[Dict[str, Any]],
    filename_constraint_mode: str = ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
    data_standard_mode: str = ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    data_standard_profile: _DataStandardClassificationProfile | None = None,
    jane_profile: _JaneClassificationProfile | None = None,
    scope_resolution: _ArchitectureScopeResolution | None = None,
) -> int:
    """兼容旧内部调用，只返回确定性约束后的 ID。"""

    return _decide_topk_deterministic_architecture_constraint(
        architecture_id,
        file_name=file_name,
        original_name=original_name,
        visible_ids=visible_ids,
        tree_index=tree_index,
        architecture_list=architecture_list,
        filename_constraint_mode=filename_constraint_mode,
        data_standard_mode=data_standard_mode,
        data_standard_profile=data_standard_profile,
        jane_profile=jane_profile,
        scope_resolution=scope_resolution,
    ).post_architecture_id


def _log_architecture_constraint_decision(
    *,
    execution_id: str,
    file_name: str,
    filename_constraint_mode: str,
    profile: _JaneClassificationProfile,
    decision: _ArchitectureConstraintDecision,
    data_standard_mode: str = ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    data_standard_profile: _DataStandardClassificationProfile | None = None,
) -> None:
    if not decision.reason_code:
        return
    standard_profile = (
        data_standard_profile
        or _DataStandardClassificationProfile()
    )
    payload = {
        "executionId": execution_id,
        "fileName": file_name,
        "constraintMode": filename_constraint_mode,
        "dataStandardMode": data_standard_mode,
        "standardNumber": standard_profile.standard_number,
        "standardTitle": standard_profile.title,
        "standardDocumentKind": standard_profile.document_kind,
        "standardIdentityConfirmed": standard_profile.identity_confirmed,
        "standardIdentityConflict": standard_profile.identity_conflict,
        "standardEvidenceSources": list(standard_profile.evidence_sources),
        "scopeKind": profile.scope_kind,
        "extractedTitle": profile.title,
        "primaryIdentifier": profile.primary_identifier,
        "filenameIdentityKind": profile.filename_identity_kind,
        "filenameIdentifiers": list(profile.filename_identifiers),
        "trustedFilenameIdentifiers": list(
            profile.trusted_filename_identifiers
        ),
        "titleIdentifiers": list(profile.title_identifiers),
        "recallIdentityEnabled": profile.recall_identity_enabled,
        "identityConfirmed": profile.identity_confirmed,
        "identityConflict": profile.identity_conflict,
        "qualifier": profile.qualifier,
        "matchedScopeParentId": decision.matched_scope_parent_id,
        "preConstraintArchitectureId": decision.pre_architecture_id,
        "postConstraintArchitectureId": decision.post_architecture_id,
        "constraintReasonCode": decision.reason_code,
        "treeGap": decision.tree_gap,
    }
    logger.info(
        "analysis_architecture_constraint=%s",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def _phase_attempt_count(session: DocumentRagSession, start_count: int) -> int:
    return max(0, len(session.trace.attempts) - start_count)


def _elapsed_ms(started_at: float, *, floor: int = 0) -> int:
    return max(floor, int(math.ceil((time.perf_counter() - started_at) * 1000.0)))


def _recall_audit_fields(
        decision: ArchitectureRecallDecision,
        *,
        prompt_chars: int,
) -> Dict[str, Any]:
    return {
        "tree_fingerprint": decision.tree_fingerprint,
        "query_digest": decision.query_digest,
        "base_top64": list(decision.base_leaf_ids),
        "final_candidates": list(decision.prompt_candidates),
        "channel_rankings": {
            ranking.channel: list(ranking.node_ids)
            for ranking in decision.channel_rankings
        },
        "rrf_scores": dict(decision.rrf_scores),
        "protected_reasons": dict(decision.protected_reasons),
        "prompt_chars": prompt_chars,
        "recall_elapsed_ms": int(math.ceil(decision.elapsed_ms)),
    }


def _direct_recall_audit_fields(
        *,
        tree_index: ArchitectureTreeIndex,
        signals: DocumentArchitectureSignals,
        candidates: Iterable[ArchitectureNodeProfile],
        prompt_chars: int,
        recall_elapsed_ms: int,
        channel_name: str,
) -> Dict[str, Any]:
    nodes = tuple(candidates)
    return {
        "tree_fingerprint": tree_index.fingerprint,
        "query_digest": _architecture_signal_digest(signals),
        "base_top64": [node.id for node in nodes if node.is_leaf][:64],
        "final_candidates": [_node_prompt_projection(node) for node in nodes],
        "channel_rankings": {channel_name: [node.id for node in nodes]},
        "rrf_scores": {},
        "protected_reasons": (
            {nodes[0].id: ["single_candidate"]}
            if len(nodes) == 1 and channel_name == "direct"
            else {}
        ),
        "prompt_chars": prompt_chars,
        "recall_elapsed_ms": recall_elapsed_ms,
    }


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
    execution_id: str,
    original_name: str,
    callback_url: str,
    callback_timeout: float,
    callback_payload: Dict[str, Any],
) -> None:
    """在业务终态落库后执行可选回调，并精确推进回调状态机。"""
    task_service.require_current_execution(
        "file",
        file_name,
        execution_id,
        allowed_statuses=("2", "3"),
    )
    if not callback_url:
        try:
            task_service.mark_callback_skipped(
                "file",
                file_name,
                execution_id=execution_id,
            )
        except Exception:
            logger.critical(
                "未配置回调地址，但无法将任务标记为无需回调: file_name=%s",
                file_name,
                exc_info=True,
            )
        return
    claim = task_service.claim_callback_delivery(
        "file",
        file_name,
        timeout=callback_timeout,
        execution_id=execution_id,
    )
    if claim is None:
        logger.info(
            "文件分析回调已有发送租约，当前 worker 不重复提交: "
            "file_name=%s execution_id=%s",
            file_name,
            execution_id,
        )
        return
    callback_claim_id, _ = claim
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
            task_service.mark_callback_failed(
                "file",
                file_name,
                callback_error,
                execution_id=execution_id,
                claim_id=callback_claim_id,
            )
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
            task_service.mark_callback_success(
                "file",
                file_name,
                execution_id=execution_id,
                claim_id=callback_claim_id,
            )
            logger.info("文件分析回调提交成功: file_name=%s", file_name)
        else:
            task_service.mark_callback_failed(
                "file",
                file_name,
                "callback failed",
                execution_id=execution_id,
                claim_id=callback_claim_id,
            )
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
    execution_id: str,
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
            execution_id=execution_id,
        )
        _publish_progress(progress_hub, file_name, 1.0)
    except (TaskExecutionConflictError, TaskStateConflictError):
        logger.warning(
            "文件分析失败终结被CAS拒绝，停止进度与回调: "
            "file_name=%s execution_id=%s stage=%s",
            file_name,
            execution_id,
            stage,
        )
        return
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
            execution_id=execution_id,
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
    execution_id: str,
    analysis_classification_mode: str = "topk_two_stage",
    analysis_filename_constraint_mode: str = (
        ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY
    ),
    analysis_data_standard_mode: str = ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    analysis_identity_reselect_mode: str = ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
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
    classification_mode = _normalize_analysis_classification_mode(
        analysis_classification_mode
    )
    filename_constraint_mode = _normalize_analysis_filename_constraint_mode(
        analysis_filename_constraint_mode
    )
    data_standard_mode = _normalize_analysis_data_standard_mode(
        analysis_data_standard_mode
    )
    identity_reselect_mode = _normalize_analysis_identity_reselect_mode(
        analysis_identity_reselect_mode
    )
    # 三种运行模式都必须先持久化模型可见候选，审计故障时禁止创建远端 Session。
    # legacy 仍发送完整小树，但同样受全局 128 候选与 32K Prompt 硬门禁约束。
    recall_audit_enabled = True
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
    execution_id = _as_text(execution_id)
    if not execution_id:
        raise ValueError("execution_id不能为空")
    task_service.require_current_execution(
        "file",
        file_name,
        execution_id,
        allowed_statuses=("0", "1"),
    )
    workflow_started_at = time.perf_counter()
    analysis_prompt = ""
    original_text = ""
    tree_index: ArchitectureTreeIndex | None = None
    recall_decision: ArchitectureRecallDecision | None = None
    recall_audit_fields: Dict[str, Any] | None = None
    recall_audit_finalized = False
    visible_candidates: tuple[Dict[str, Any], ...] = ()
    visible_ids: set[int] = set()
    resolved_direct_architecture_id: int | None = None
    data_standard_profile = _DataStandardClassificationProfile()
    data_standard_scope_guard_active = False
    data_standard_scope_ids: tuple[int, ...] = ()
    data_standard_remark_overrides: dict[int, str] = {}
    jane_profile = _JaneClassificationProfile()
    equipment_identity_profile = _EquipmentIdentityReselectProfile()
    scope_resolution = _ArchitectureScopeResolution()
    constraint_decision: _ArchitectureConstraintDecision | None = None
    data_standard_general_fallback_applied = False

    def persist_initial_recall_audit(fields: Dict[str, Any]) -> None:
        task_service.upsert_architecture_recall_decision(
            execution_id=execution_id,
            **fields,
        )

    def fail_before_remote_session(
            *,
            stage: str,
            error: BaseException,
            fields: Dict[str, Any],
    ) -> None:
        nonlocal recall_audit_finalized
        error_message = _safe_task_error(error, fallback="领域分类前置处理失败")
        try:
            persist_initial_recall_audit(fields)
            task_service.finalize_architecture_recall_decision(
                execution_id=execution_id,
                returned_architecture_id=None,
                returned_rank=None,
                total_elapsed_ms=_elapsed_ms(
                    workflow_started_at,
                    floor=int(fields["recall_elapsed_ms"]),
                ),
                failure_stage=stage,
                error_message=error_message,
            )
            recall_audit_finalized = True
        except Exception as audit_exc:
            error_message = _safe_task_error(
                audit_exc,
                fallback="领域召回审计失败",
            )
            logger.exception(
                "领域召回审计失败，禁止创建远端 Session: "
                "file_name=%s execution_id=%s stage=%s",
                file_name,
                execution_id,
                stage,
            )
        _finalize_file_failure(
            task_service=task_service,
            progress_hub=progress_hub,
            file_name=file_name,
            execution_id=execution_id,
            original_name=original_name,
            stage=stage,
            error_message=error_message,
            callback_url=callback_url,
            callback_timeout=callback_timeout,
        )

    architecture_index_started_at = time.perf_counter()
    try:
        ranges = build_effective_analysis_ranges(params)
        tree_index = validate_analysis_architecture_ranges(params)
    except ArchitectureTreeValidationError as exc:
        fields = {
            "tree_fingerprint": "",
            "query_digest": hashlib.sha256(b"").hexdigest(),
            "base_top64": [],
            "final_candidates": [],
            "channel_rankings": {},
            "rrf_scores": {},
            "protected_reasons": {},
            "prompt_chars": 0,
            "recall_elapsed_ms": _elapsed_ms(
                architecture_index_started_at
            ),
        }
        fail_before_remote_session(
            stage="architecture_index",
            error=exc,
            fields=fields,
        )
        return
    architecture_index_elapsed_seconds = (
        time.perf_counter() - architecture_index_started_at
    )

    logger.info(
        "开始执行文件分析任务: file_name=%s execution_id=%s",
        file_name,
        execution_id,
    )
    try:
        task_service.update_task_progress(
            "file",
            file_name,
            progress=0.15,
            message="正在下载文件",
            status="1",
            execution_id=execution_id,
        )
        _publish_progress(progress_hub, file_name, 0.15)
        downloaded_path = download_to_temp_file(
            file_path,
            file_name,
            download_root,
            timeout=60,
        )
        task_service.update_task_progress(
            "file",
            file_name,
            progress=0.35,
            message="正在执行文档解析",
            execution_id=execution_id,
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
        # 正文只读取一次，并在任何 Factory/Session 创建前同时提供给召回和 mapper。
        original_text = _read_original_text(llm_file_path)
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
            execution_id=execution_id,
            original_name=original_name,
            stage="preparation",
            error_message=error_message,
            callback_url=callback_url,
            callback_timeout=callback_timeout,
        )
        return

    architecture_list = ranges["architectureList"]
    data_standard_profile = _build_data_standard_classification_profile(
        file_name=file_name,
        original_name=original_name,
        original_text=original_text,
    )
    data_standard_scope_guard_active = (
        data_standard_mode == ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD
        and classification_mode != "legacy"
        and data_standard_profile.identity_confirmed
        and data_standard_profile.document_kind == "standard_body"
    )
    jane_profile = _build_jane_classification_profile(
        file_name=file_name,
        original_name=original_name,
        original_text=original_text,
    )
    scope_guard_active = (
        filename_constraint_mode
        == ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
        and jane_profile.active
        and not data_standard_scope_guard_active
    )
    # 召回强证据收窄与 Jane 最终作用域约束是两个独立边界：普通非 Jane 文档在
    # scope_guard 模式下也只能让原文件名/可信标题参与 exact 与装备 family 规则，
    # 正文、章节和 Fleetlist 仍保留在 query_text 中参与 lexical/tree 召回；但这里
    # 不会激活下游 Jane 硬约束，最终分类仍由模型在可见候选内决定。
    recall_strong_evidence_only = (
        filename_constraint_mode
        == ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
        or data_standard_scope_guard_active
    )
    recall_file_name, recall_original_name = _jane_recall_filename_signals(
        file_name=file_name,
        original_name=original_name,
        profile=jane_profile,
        scope_guard_active=scope_guard_active,
    )
    signals = _build_analysis_architecture_signals(
        file_name=recall_file_name,
        original_name=recall_original_name,
        original_text=original_text,
        title_override=(
            data_standard_profile.title
            if data_standard_scope_guard_active
            else jane_profile.title
            if scope_guard_active
            else ""
        ),
    )
    signal_digest = _architecture_signal_digest(signals)
    # 领域树已在下载前完成索引。将其实际耗时折入既有 recall_elapsed_ms，同时排除
    # 中间的下载与正文读取耗时，保持审计指标原有语义。
    index_started_at = (
        time.perf_counter() - architecture_index_elapsed_seconds
    )

    if scope_guard_active:
        scope_resolution = _resolve_jane_architecture_scope(
            jane_profile,
            original_text=original_text,
            tree_index=tree_index,
        )

    try:
        if data_standard_scope_guard_active:
            (
                data_standard_scope_ids,
                data_standard_remark_overrides,
            ) = _data_standard_candidate_scope(
                tree_index=tree_index,
                architecture_list=architecture_list,
            )
            if not data_standard_scope_ids:
                raise ArchitectureRecallError(
                    "已确认标准正文，但 architectureList 中没有可用的数据标准叶节点"
                )
        if classification_mode == "legacy":
            analysis_prompt = normalize_rag_prompt(build_file_analysis_prompt(params))
            if (
                    len(tree_index.nodes) > 128
                    or len(analysis_prompt) > MAX_ANALYSIS_PROMPT_CHARS
            ):
                raise ArchitecturePromptBudgetError(
                    "legacy 完整领域树候选必须不超过 128 个且 Prompt "
                    "必须不超过 32000 字符"
                )
            visible_candidates = tuple(
                _node_prompt_projection(node) for node in tree_index.nodes
            )
            visible_ids = {node.id for node in tree_index.nodes}
            recall_audit_fields = _direct_recall_audit_fields(
                tree_index=tree_index,
                signals=signals,
                candidates=tree_index.nodes,
                prompt_chars=len(analysis_prompt),
                recall_elapsed_ms=_elapsed_ms(index_started_at),
                channel_name="legacy",
            )
        else:
            if len(tree_index.nodes) == 1:
                direct_node = tree_index.nodes[0]
                visible_candidates = (_node_prompt_projection(direct_node),)
                visible_ids = {direct_node.id}
                resolved_direct_architecture_id = direct_node.id
                _validate_data_standard_leaf_requirement(
                    direct_node.id,
                    architecture_list,
                )
            else:
                # 召回服务先以宽松估算上限返回实际候选；真实 Prompt 随后执行 32K 硬门禁。
                recall_decision = recall_architecture_candidates(
                    tree_index,
                    signals,
                    prompt_char_limit=2_000_000,
                    prompt_overhead_chars=0,
                    strong_evidence_only=recall_strong_evidence_only,
                    strong_identity_enabled=(
                        jane_profile.recall_identity_enabled
                        if scope_guard_active
                        else True
                    ),
                    # Jane 标题+正文类型别名是既有的双源特例，不能因普通非 Jane
                    # 文档启用召回强证据收窄而被意外激活。
                    jane_title_type_alias_enabled=scope_guard_active,
                    preferred_parent_reasons=(
                        scope_resolution.preferred_parent_reasons
                        if scope_guard_active
                        else None
                    ),
                    candidate_scope_ids=(
                        data_standard_scope_ids
                        if data_standard_scope_guard_active
                        else None
                    ),
                    candidate_scope_reason=(
                        "data-standard-scope"
                        if data_standard_scope_guard_active
                        else ""
                    ),
                    candidate_remark_overrides=(
                        data_standard_remark_overrides
                        if data_standard_scope_guard_active
                        else None
                    ),
                )
                visible_candidates = recall_decision.prompt_candidates
                visible_ids = set(recall_decision.final_candidate_ids)
                if len(visible_candidates) == 1:
                    resolved_direct_architecture_id = _validate_topk_architecture_id(
                        visible_candidates[0]["id"],
                        visible_ids=visible_ids,
                        tree_index=tree_index,
                        architecture_list=architecture_list,
                    )

            if resolved_direct_architecture_id is not None:
                direct_node = tree_index.require(resolved_direct_architecture_id)
                include_standard_fields = _is_architecture_in_standard_range(
                    resolved_direct_architecture_id,
                    architecture_list,
                    ranges["architectureStandardList"],
                )
                analysis_prompt = normalize_rag_prompt(
                    build_file_extraction_prompt(
                        params,
                        resolved_architecture_id=resolved_direct_architecture_id,
                        resolved_architecture_path_name=direct_node.semantic_path,
                        resolved_architecture_node_type=(
                            "leaf" if direct_node.is_leaf else "parent"
                        ),
                        include_data_standard_fields=include_standard_fields,
                    )
                )
            elif classification_mode == "topk_two_stage":
                analysis_prompt = normalize_rag_prompt(
                    (
                        build_data_standard_classification_prompt(
                            params,
                            visible_candidates,
                            standard_context=_data_standard_prompt_context(
                                data_standard_profile
                            ),
                        )
                        if data_standard_scope_guard_active
                        else build_architecture_classification_prompt(
                            params,
                            visible_candidates,
                            classification_context=(
                                _jane_classification_prompt_context(
                                    jane_profile,
                                    scope_resolution,
                                )
                                if scope_guard_active
                                else None
                            ),
                        )
                    )
                )
            else:
                limited_params = dict(params)
                limited_params["architectureList"] = list(visible_candidates)
                scope_contract = ""
                if data_standard_scope_guard_active:
                    scope_contract = (
                        "\n【数据标准作用域分类补充规则】\n"
                        "服务端已确认该文件是标准正文；只能在下方数据标准叶节点中分类。"
                        "专业类别必须由标准标题或范围支持；普通目录中的“术语和定义”不能"
                        "单独决定分类。不属于五个专业主题时选择“通用要求”。\n"
                        "服务端标准画像："
                        + json.dumps(
                            _data_standard_prompt_context(
                                data_standard_profile
                            ),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                elif scope_guard_active:
                    scope_contract = (
                        "\n【简氏作用域分类补充规则】\n"
                        "按全文主要对象和覆盖粒度分类；class 文档的首舰号只标识舰级，"
                        "Fleetlist 成员不能单独决定最终分类；Flight、Block、批次限定词"
                        "优先于基础型号；只有全文主要描述明细类别时才选择明细叶子。\n"
                        "服务端首页画像："
                        + json.dumps(
                            _jane_classification_prompt_context(
                                jane_profile,
                                scope_resolution,
                            ),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                analysis_prompt = normalize_rag_prompt(
                    build_file_analysis_prompt(limited_params)
                    + "\n【topk_single 受限候选补充合同】\n"
                    + "下方 JSON 是本次完整且唯一可选的模型候选，nodeType 必须保留语义。"
                    + "证据足以支持 leaf 时优先叶子；叶子证据不足但能可靠确定 parent 时，"
                    + "允许返回候选中最深的 parent。此规则替代上文只允许叶子的旧规则。\n"
                    + scope_contract
                    + json.dumps(
                        list(visible_candidates),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )

            if len(visible_candidates) > 128:
                raise ArchitecturePromptBudgetError("领域模型候选数量超过 128 个")
            if len(analysis_prompt) > MAX_ANALYSIS_PROMPT_CHARS:
                raise ArchitecturePromptBudgetError(
                    f"模型 Prompt 共 {len(analysis_prompt)} 字符，超过 32000 字符上限"
                )

            if recall_decision is not None:
                recall_audit_fields = _recall_audit_fields(
                    recall_decision,
                    prompt_chars=len(analysis_prompt),
                )
            else:
                direct_nodes = tuple(
                    tree_index.require(candidate["id"])
                    for candidate in visible_candidates
                )
                recall_audit_fields = _direct_recall_audit_fields(
                    tree_index=tree_index,
                    signals=signals,
                    candidates=direct_nodes,
                    prompt_chars=len(analysis_prompt),
                    recall_elapsed_ms=_elapsed_ms(index_started_at),
                    channel_name="direct",
                )
    except ArchitectureTreeValidationError as exc:
        fields = {
            "tree_fingerprint": tree_index.fingerprint,
            "query_digest": signal_digest,
            "base_top64": [],
            "final_candidates": [],
            "channel_rankings": {},
            "rrf_scores": {},
            "protected_reasons": {},
            "prompt_chars": 0,
            "recall_elapsed_ms": _elapsed_ms(index_started_at),
        }
        fail_before_remote_session(
            stage="architecture_index",
            error=exc,
            fields=fields,
        )
        return
    except ArchitecturePromptBudgetError as exc:
        if recall_decision is not None:
            fields = _recall_audit_fields(
                recall_decision,
                prompt_chars=len(analysis_prompt),
            )
        else:
            auditable_nodes = tree_index.nodes if len(tree_index.nodes) <= 128 else ()
            fields = _direct_recall_audit_fields(
                tree_index=tree_index,
                signals=signals,
                candidates=auditable_nodes,
                prompt_chars=len(analysis_prompt),
                recall_elapsed_ms=_elapsed_ms(index_started_at),
                channel_name="legacy" if classification_mode == "legacy" else "direct",
            )
        fail_before_remote_session(
            stage="architecture_prompt_budget",
            error=exc,
            fields=fields,
        )
        return
    except ArchitectureRecallError as exc:
        fields = {
            "tree_fingerprint": tree_index.fingerprint,
            "query_digest": signal_digest,
            "base_top64": [],
            "final_candidates": [],
            "channel_rankings": {},
            "rrf_scores": {},
            "protected_reasons": {},
            "prompt_chars": 0,
            "recall_elapsed_ms": _elapsed_ms(index_started_at),
        }
        fail_before_remote_session(
            stage="architecture_recall",
            error=exc,
            fields=fields,
        )
        return
    except ArchitectureContractError as exc:
        direct_nodes = tuple(
            tree_index.require(candidate["id"])
            for candidate in visible_candidates
        )
        fields = _direct_recall_audit_fields(
            tree_index=tree_index,
            signals=signals,
            candidates=direct_nodes,
            prompt_chars=len(analysis_prompt),
            recall_elapsed_ms=_elapsed_ms(index_started_at),
            channel_name="direct",
        )
        fail_before_remote_session(
            stage="architecture_contract",
            error=exc,
            fields=fields,
        )
        return

    if (
        identity_reselect_mode != ANALYSIS_IDENTITY_RESELECT_MODE_OFF
        and classification_mode == "topk_two_stage"
        and filename_constraint_mode
        == ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
        and resolved_direct_architecture_id is None
    ):
        try:
            equipment_identity_profile = (
                _build_equipment_identity_reselect_profile(
                    requested_original_name=requested_original_name,
                    original_text=original_text,
                    tree_index=tree_index,
                    visible_ids=visible_ids,
                    jane_active=jane_profile.active,
                    data_standard_active=data_standard_scope_guard_active,
                )
            )
        except Exception:
            equipment_identity_profile = _EquipmentIdentityReselectProfile(
                reason_code="identity_profile_error"
            )
            logger.exception(
                "装备双证据身份画像失败，保留普通分类链路: "
                "file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
    logger.info(
        "装备身份受限重选门禁已评估: execution_id=%s mode=%s active=%s "
        "reason=%s identifier=%s target_parent_id=%s candidate_count=%d",
        execution_id,
        identity_reselect_mode,
        equipment_identity_profile.active,
        equipment_identity_profile.reason_code,
        equipment_identity_profile.identifier,
        equipment_identity_profile.target_parent_id,
        len(equipment_identity_profile.candidate_ids),
    )

    if recall_audit_enabled and recall_audit_fields is None:
        raise RuntimeError("领域召回未生成可审计决策")
    if recall_audit_enabled:
        try:
            # 该写入是远端 Session 创建的硬前置；失败时下面的 Factory 代码不会执行。
            persist_initial_recall_audit(recall_audit_fields)
        except Exception as exc:
            error_message = _safe_task_error(exc, fallback="领域召回审计失败")
            logger.exception(
                "领域召回审计失败，禁止创建远端 Session: "
                "file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                execution_id=execution_id,
                original_name=original_name,
                stage="architecture_recall",
                error_message=error_message,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
            return

    task_service.require_current_execution(
        "file",
        file_name,
        execution_id,
        allowed_statuses=("0", "1"),
    )
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
            if recall_audit_enabled:
                try:
                    task_service.finalize_architecture_recall_decision(
                        execution_id=execution_id,
                        returned_architecture_id=None,
                        returned_rank=None,
                        total_elapsed_ms=_elapsed_ms(
                            workflow_started_at,
                            floor=int(recall_audit_fields["recall_elapsed_ms"]),
                        ),
                        failure_stage="architecture_contract",
                        error_message=lease_error,
                    )
                    recall_audit_finalized = True
                except Exception:
                    logger.critical(
                        "资源租约失败后无法终结召回审计: execution_id=%s",
                        execution_id,
                        exc_info=True,
                    )
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                execution_id=execution_id,
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
        workflow_failure_stage = "architecture_contract"
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
            if (
                    classification_mode == "topk_two_stage"
                    and resolved_direct_architecture_id is None
            ):
                classification_started = len(session.trace.attempts)
                rag_result = session.analyse(
                    llm_file_path,
                    analysis_prompt,
                    prompt_kind=RagPromptKind.ARCHITECTURE_CLASSIFICATION,
                    require_sources=True,
                    max_attempts=MAX_ANALYSIS_PHASE_CALLS,
                )
                prepared_document = rag_result.prepared_document
                _record_lease_resources(
                    task_service,
                    execution_id,
                    rag_result.trace,
                    prepared_document,
                )
                parsed_classification = _parse_strict_json_object(rag_result.text)
                architecture_id: int | None = None
                try:
                    parsed_classification, architecture_id = (
                        _parse_topk_classification_result(
                            rag_result.text,
                            visible_ids=visible_ids,
                            tree_index=tree_index,
                            architecture_list=architecture_list,
                        )
                    )
                except ArchitectureContractError as contract_error:
                    force_standard = isinstance(
                        contract_error,
                        DataStandardParentContractError,
                    )
                    architecture_id = (
                        None
                        if data_standard_scope_guard_active
                        else _visible_data_standard_fallback_id(
                            visible_ids=visible_ids,
                            architecture_list=architecture_list,
                            force=force_standard,
                            context_values=(original_text, original_name),
                        )
                    )
                    if architecture_id is None:
                        attempts_used = _phase_attempt_count(
                            session,
                            classification_started,
                        )
                        if attempts_used >= MAX_ANALYSIS_PHASE_CALLS:
                            if not data_standard_scope_guard_active:
                                raise ArchitectureContractError(
                                    "分类阶段实际模型调用预算已耗尽，无法 repair"
                                ) from contract_error
                            architecture_id = (
                                _visible_data_standard_fallback_id(
                                    visible_ids=visible_ids,
                                    architecture_list=architecture_list,
                                    force=True,
                                    context_values=(
                                        original_text,
                                        original_name,
                                    ),
                                )
                            )
                            if architecture_id is None:
                                raise ArchitectureContractError(
                                    "标准正文分类预算耗尽，且候选中不存在通用要求叶节点"
                                ) from contract_error
                            data_standard_general_fallback_applied = True
                        if architecture_id is None:
                            final_prompt = _normalize_bounded_analysis_prompt(
                                build_architecture_repair_prompt(
                                    parsed_classification
                                    or {"architectureId": None},
                                    visible_candidates,
                                    str(contract_error),
                                )
                            )
                            repaired_result = session.ask(
                                final_prompt,
                                prompt_kind=RagPromptKind.ARCHITECTURE_REPAIR,
                                require_sources=True,
                                max_attempts=(
                                    MAX_ANALYSIS_PHASE_CALLS - attempts_used
                                ),
                            )
                            try:
                                _repaired, architecture_id = (
                                    _parse_topk_classification_result(
                                        repaired_result.text,
                                        visible_ids=visible_ids,
                                        tree_index=tree_index,
                                        architecture_list=architecture_list,
                                    )
                                )
                            except ArchitectureContractError as repair_error:
                                architecture_id = (
                                    _visible_data_standard_fallback_id(
                                        visible_ids=visible_ids,
                                        architecture_list=architecture_list,
                                        force=True,
                                        context_values=(
                                            original_text,
                                            original_name,
                                        ),
                                    )
                                    if data_standard_scope_guard_active
                                    else None
                                )
                                if architecture_id is None:
                                    raise ArchitectureContractError(
                                        "标准正文分类 repair 后仍无法确定类别，且候选中"
                                        "不存在通用要求叶节点"
                                    ) from repair_error
                                data_standard_general_fallback_applied = True

                if architecture_id is None:
                    raise ArchitectureContractError("无法确定领域分类")
                initial_architecture_id = architecture_id
                identity_gate_decision = _decide_identity_reselect_gate(
                    architecture_id,
                    profile=equipment_identity_profile,
                    tree_index=tree_index,
                )
                identity_reselect_architecture_id: int | None = None
                identity_reselect_outcome = identity_gate_decision.reason_code
                classification_attempts_used = _phase_attempt_count(
                    session,
                    classification_started,
                )
                if identity_gate_decision.should_reselect:
                    if classification_attempts_used != 1:
                        identity_reselect_outcome = "skip_call_budget"
                    else:
                        try:
                            scoped_candidates = tuple(
                                _node_prompt_projection(tree_index.require(node_id))
                                for node_id in equipment_identity_profile.candidate_ids
                            )
                            reselect_prompt = _normalize_bounded_analysis_prompt(
                                build_architecture_reselect_prompt(
                                    {"architectureId": initial_architecture_id},
                                    {
                                        "identifier": (
                                            equipment_identity_profile.identifier
                                        ),
                                        "matchedParentId": (
                                            equipment_identity_profile.target_parent_id
                                        ),
                                        "matchedParentPath": (
                                            equipment_identity_profile.target_parent_path
                                        ),
                                        "evidenceSources": list(
                                            equipment_identity_profile.evidence_sources
                                        ),
                                    },
                                    scoped_candidates,
                                )
                            )
                        except Exception:
                            identity_reselect_outcome = "prompt_build_failed"
                            logger.exception(
                                "装备身份受限重选 Prompt 构造失败，保留初次分类: "
                                "file_name=%s execution_id=%s",
                                file_name,
                                execution_id,
                            )
                        else:
                            reselect_conversation_ready = (
                                session.start_fresh_conversation(
                                    conversation_name=(
                                        "analysis-identity-reselect-"
                                        f"{Path(file_name).stem}"
                                    ),
                                    failure_is_fatal=False,
                                )
                            )
                            _record_lease_resources(
                                task_service,
                                execution_id,
                                session.trace,
                                prepared_document,
                            )
                            if not reselect_conversation_ready:
                                identity_reselect_outcome = (
                                    "conversation_unavailable_keep_initial"
                                )
                            else:
                                final_prompt = reselect_prompt
                                reselect_result = session.ask_optional(
                                    final_prompt,
                                    prompt_kind=(
                                        RagPromptKind.ARCHITECTURE_RESELECT
                                    ),
                                    require_sources=True,
                                    max_attempts=1,
                                )
                                _record_lease_resources(
                                    task_service,
                                    execution_id,
                                    session.trace,
                                    prepared_document,
                                )
                                if reselect_result is None:
                                    identity_reselect_outcome = (
                                        "query_failed_keep_initial"
                                    )
                                else:
                                    try:
                                        identity_reselect_architecture_id = (
                                            _parse_architecture_reselect_result(
                                                reselect_result.text,
                                                scoped_ids=set(
                                                    equipment_identity_profile.candidate_ids
                                                ),
                                                tree_index=tree_index,
                                                architecture_list=architecture_list,
                                            )
                                        )
                                    except ArchitectureContractError:
                                        identity_reselect_outcome = (
                                            "invalid_result_keep_initial"
                                        )
                                        logger.warning(
                                            "装备身份受限重选结果不合法，保留初次分类: "
                                            "file_name=%s execution_id=%s",
                                            file_name,
                                            execution_id,
                                        )
                                    else:
                                        if identity_reselect_architecture_id is None:
                                            identity_reselect_outcome = (
                                                "null_result_keep_initial"
                                            )
                                        elif (
                                            identity_reselect_mode
                                            == ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE
                                        ):
                                            architecture_id = (
                                                identity_reselect_architecture_id
                                            )
                                            identity_reselect_outcome = (
                                                "enforce_applied"
                                            )
                                        else:
                                            identity_reselect_outcome = (
                                                "shadow_kept_initial"
                                            )
                logger.info(
                    "装备身份受限重选完成: execution_id=%s mode=%s relation=%s "
                    "gate_reason=%s initial_architecture_id=%s "
                    "reselect_architecture_id=%s pre_constraint_architecture_id=%s "
                    "classification_attempts=%d outcome=%s",
                    execution_id,
                    identity_reselect_mode,
                    identity_gate_decision.relation,
                    identity_gate_decision.reason_code,
                    initial_architecture_id,
                    identity_reselect_architecture_id,
                    architecture_id,
                    classification_attempts_used,
                    identity_reselect_outcome,
                )
                constraint_decision = (
                    _decide_topk_deterministic_architecture_constraint(
                        architecture_id,
                        file_name=file_name,
                        original_name=original_name,
                        visible_ids=visible_ids,
                        tree_index=tree_index,
                        architecture_list=architecture_list,
                        filename_constraint_mode=filename_constraint_mode,
                        data_standard_mode=data_standard_mode,
                        data_standard_profile=data_standard_profile,
                        jane_profile=jane_profile,
                        scope_resolution=scope_resolution,
                    )
                )
                if data_standard_general_fallback_applied:
                    constraint_decision = _ArchitectureConstraintDecision(
                        pre_architecture_id=architecture_id,
                        post_architecture_id=architecture_id,
                        reason_code="data_standard_general_fallback",
                        matched_scope_parent_id=None,
                        tree_gap=False,
                    )
                architecture_id = constraint_decision.post_architecture_id
                selected_node = tree_index.require(architecture_id)
                include_standard_fields = _is_architecture_in_standard_range(
                    architecture_id,
                    architecture_list,
                    ranges["architectureStandardList"],
                )
                extraction_prompt = _normalize_bounded_analysis_prompt(
                    build_file_extraction_prompt(
                        params,
                        resolved_architecture_id=architecture_id,
                        resolved_architecture_path_name=selected_node.semantic_path,
                        resolved_architecture_node_type=(
                            "leaf" if selected_node.is_leaf else "parent"
                        ),
                        include_data_standard_fields=include_standard_fields,
                    )
                )
                workflow_failure_stage = "analysis_extraction"
                session.start_fresh_conversation(
                    conversation_name=(
                        f"analysis-extraction-{Path(file_name).stem}"
                    ),
                )
                _record_lease_resources(
                    task_service,
                    execution_id,
                    session.trace,
                    prepared_document,
                )
                # 只有第二线程已经创建成功，抽取 Prompt 才成为审计中的最后实际请求。
                # 创建失败时 final_prompt 继续指向分类或分类 repair Prompt。
                final_prompt = extraction_prompt
                extraction_started = len(session.trace.attempts)
                extraction_result = session.ask(
                    final_prompt,
                    prompt_kind=RagPromptKind.ANALYSIS_EXTRACTION,
                    require_sources=True,
                    max_attempts=MAX_ANALYSIS_PHASE_CALLS,
                )
                parsed_result = _parse_strict_json_object(extraction_result.text)
                if parsed_result is None:
                    attempts_used = _phase_attempt_count(session, extraction_started)
                    if attempts_used >= MAX_ANALYSIS_PHASE_CALLS:
                        raise AnalysisContractError(
                            "字段抽取阶段实际模型调用预算已耗尽，无法 JSON repair"
                        )
                    final_prompt = _normalize_bounded_analysis_prompt(
                        build_json_repair_prompt(extraction_result.text)
                    )
                    repaired_result = session.ask(
                        final_prompt,
                        prompt_kind=RagPromptKind.JSON_REPAIR,
                        require_sources=True,
                        max_attempts=MAX_ANALYSIS_PHASE_CALLS - attempts_used,
                    )
                    parsed_result = _parse_strict_json_object(repaired_result.text)
                    if parsed_result is None:
                        raise AnalysisContractError(
                            "JSON 修复后仍不是严格 JSON 对象"
                        )
            elif resolved_direct_architecture_id is not None:
                architecture_id = resolved_direct_architecture_id
                workflow_failure_stage = "analysis_extraction"
                extraction_started = len(session.trace.attempts)
                rag_result = session.analyse(
                    llm_file_path,
                    analysis_prompt,
                    prompt_kind=RagPromptKind.ANALYSIS_EXTRACTION,
                    require_sources=True,
                    max_attempts=MAX_ANALYSIS_PHASE_CALLS,
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
                    attempts_used = _phase_attempt_count(session, extraction_started)
                    if attempts_used >= MAX_ANALYSIS_PHASE_CALLS:
                        raise AnalysisContractError(
                            "字段抽取阶段实际模型调用预算已耗尽，无法 JSON repair"
                        )
                    final_prompt = _normalize_bounded_analysis_prompt(
                        build_json_repair_prompt(rag_result.text)
                    )
                    repaired_result = session.ask(
                        final_prompt,
                        prompt_kind=RagPromptKind.JSON_REPAIR,
                        require_sources=True,
                        max_attempts=MAX_ANALYSIS_PHASE_CALLS - attempts_used,
                    )
                    parsed_result = _parse_strict_json_object(repaired_result.text)
                    if parsed_result is None:
                        raise AnalysisContractError(
                            "JSON 修复后仍不是严格 JSON 对象"
                        )
            else:
                workflow_failure_stage = "analysis_extraction"
                combined_started = len(session.trace.attempts)
                rag_result = session.analyse(
                    llm_file_path,
                    analysis_prompt,
                    prompt_kind=RagPromptKind.ANALYSIS,
                    require_sources=True,
                    max_attempts=MAX_ANALYSIS_PHASE_CALLS,
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
                    attempts_used = _phase_attempt_count(session, combined_started)
                    if attempts_used >= MAX_ANALYSIS_PHASE_CALLS:
                        raise AnalysisContractError(
                            "combined 阶段实际模型调用预算已耗尽，无法 JSON repair"
                        )
                    final_prompt = _normalize_bounded_analysis_prompt(
                        build_json_repair_prompt(rag_result.text)
                    )
                    repaired_result = session.ask(
                        final_prompt,
                        prompt_kind=RagPromptKind.JSON_REPAIR,
                        require_sources=True,
                        max_attempts=MAX_ANALYSIS_PHASE_CALLS - attempts_used,
                    )
                    parsed_result = _parse_strict_json_object(repaired_result.text)
                    if parsed_result is None:
                        raise AnalysisContractError(
                            "JSON 修复后仍不是严格 JSON 对象"
                        )

                workflow_failure_stage = "architecture_contract"
                try:
                    if classification_mode == "legacy":
                        architecture_id = _resolve_analysis_architecture_id(
                            parsed_result,
                            params,
                        )
                        architecture_id = _validate_topk_architecture_id(
                            architecture_id,
                            visible_ids=visible_ids,
                            tree_index=tree_index,
                            architecture_list=architecture_list,
                        )
                    else:
                        if "architectureId" not in parsed_result:
                            raise ArchitectureContractError("architectureId 缺失")
                        architecture_id = _validate_topk_architecture_id(
                            parsed_result.get("architectureId"),
                            visible_ids=visible_ids,
                            tree_index=tree_index,
                            architecture_list=architecture_list,
                        )
                except ArchitectureContractError as contract_error:
                    force_standard = isinstance(
                        contract_error,
                        DataStandardParentContractError,
                    )
                    if classification_mode == "legacy":
                        architecture_id = (
                            _general_data_standard_leaf_id(architecture_list)
                            if force_standard
                            else _match_gjb_architecture_candidate(
                                parsed_result,
                                params,
                                original_text,
                                architecture_list,
                            )
                        )
                    else:
                        architecture_id = (
                            None
                            if data_standard_scope_guard_active
                            else _visible_data_standard_fallback_id(
                                visible_ids=visible_ids,
                                architecture_list=architecture_list,
                                force=force_standard,
                                context_values=(
                                    original_text,
                                    original_name,
                                ),
                            )
                        )
                    if architecture_id is None:
                        attempts_used = _phase_attempt_count(
                            session,
                            combined_started,
                        )
                        if attempts_used >= MAX_ANALYSIS_PHASE_CALLS:
                            if not data_standard_scope_guard_active:
                                raise ArchitectureContractError(
                                    "combined 阶段实际模型调用预算已耗尽，无法 "
                                    "architecture repair"
                                ) from contract_error
                            architecture_id = (
                                _visible_data_standard_fallback_id(
                                    visible_ids=visible_ids,
                                    architecture_list=architecture_list,
                                    force=True,
                                    context_values=(
                                        original_text,
                                        original_name,
                                    ),
                                )
                            )
                            if architecture_id is None:
                                raise ArchitectureContractError(
                                    "标准正文分类预算耗尽，且候选中不存在通用要求叶节点"
                                ) from contract_error
                            data_standard_general_fallback_applied = True
                        if architecture_id is None:
                            final_prompt = _normalize_bounded_analysis_prompt(
                                build_architecture_repair_prompt(
                                    parsed_result,
                                    visible_candidates,
                                    str(contract_error),
                                )
                            )
                            repaired_result = session.ask(
                                final_prompt,
                                prompt_kind=RagPromptKind.ARCHITECTURE_REPAIR,
                                require_sources=True,
                                max_attempts=(
                                    MAX_ANALYSIS_PHASE_CALLS - attempts_used
                                ),
                            )
                            if classification_mode == "legacy":
                                architecture_id = (
                                    _validate_architecture_repair_result(
                                        repaired_result.text,
                                        params,
                                    )
                                )
                                architecture_id = _validate_topk_architecture_id(
                                    architecture_id,
                                    visible_ids=visible_ids,
                                    tree_index=tree_index,
                                    architecture_list=architecture_list,
                                )
                            else:
                                try:
                                    _repaired, architecture_id = (
                                        _parse_topk_classification_result(
                                            repaired_result.text,
                                            visible_ids=visible_ids,
                                            tree_index=tree_index,
                                            architecture_list=architecture_list,
                                        )
                                    )
                                except ArchitectureContractError as repair_error:
                                    architecture_id = (
                                        _visible_data_standard_fallback_id(
                                            visible_ids=visible_ids,
                                            architecture_list=architecture_list,
                                            force=True,
                                            context_values=(
                                                original_text,
                                                original_name,
                                            ),
                                        )
                                        if data_standard_scope_guard_active
                                        else None
                                    )
                                    if architecture_id is None:
                                        raise ArchitectureContractError(
                                            "标准正文分类 repair 后仍无法确定类别，且候选中"
                                            "不存在通用要求叶节点"
                                        ) from repair_error
                                    data_standard_general_fallback_applied = True

            if constraint_decision is None:
                constraint_decision = (
                    _decide_topk_deterministic_architecture_constraint(
                        architecture_id,
                        file_name=file_name,
                        original_name=original_name,
                        visible_ids=visible_ids,
                        tree_index=tree_index,
                        architecture_list=architecture_list,
                        filename_constraint_mode=filename_constraint_mode,
                        data_standard_mode=data_standard_mode,
                        data_standard_profile=data_standard_profile,
                        jane_profile=jane_profile,
                        scope_resolution=scope_resolution,
                    )
                )
            if data_standard_general_fallback_applied:
                constraint_decision = _ArchitectureConstraintDecision(
                    pre_architecture_id=architecture_id,
                    post_architecture_id=architecture_id,
                    reason_code="data_standard_general_fallback",
                    matched_scope_parent_id=None,
                    tree_gap=False,
                )
            architecture_id = constraint_decision.post_architecture_id
            _log_architecture_constraint_decision(
                execution_id=execution_id,
                file_name=file_name,
                filename_constraint_mode=filename_constraint_mode,
                profile=jane_profile,
                decision=constraint_decision,
                data_standard_mode=data_standard_mode,
                data_standard_profile=data_standard_profile,
            )
            if len(session.trace.attempts) > MAX_ANALYSIS_MODEL_CALLS:
                raise AnalysisContractError("文件分析实际模型调用超过 4 次")
            mapped_result = map_analysis_result(
                parsed_result,
                params,
                original_text=original_text,
                resolved_architecture_id=architecture_id,
            )
            returned_rank = next(
                index + 1
                for index, candidate in enumerate(visible_candidates)
                if candidate["id"] == architecture_id
            )
            if recall_audit_enabled:
                task_service.finalize_architecture_recall_decision(
                    execution_id=execution_id,
                    returned_architecture_id=architecture_id,
                    returned_rank=returned_rank,
                    total_elapsed_ms=_elapsed_ms(
                        workflow_started_at,
                        floor=int(recall_audit_fields["recall_elapsed_ms"]),
                    ),
                )
                recall_audit_finalized = True
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
            failure_stage = (
                "architecture_prompt_budget"
                if isinstance(exc, ArchitecturePromptBudgetError)
                else workflow_failure_stage
            )
            if recall_audit_enabled and not recall_audit_finalized:
                try:
                    task_service.finalize_architecture_recall_decision(
                        execution_id=execution_id,
                        returned_architecture_id=None,
                        returned_rank=None,
                        total_elapsed_ms=_elapsed_ms(
                            workflow_started_at,
                            floor=int(recall_audit_fields["recall_elapsed_ms"]),
                        ),
                        failure_stage=failure_stage,
                        error_message=error_message,
                    )
                    recall_audit_finalized = True
                except Exception as recall_audit_exc:
                    error_message = _safe_task_error(
                        recall_audit_exc,
                        fallback="领域召回终结审计失败",
                    )
                    logger.critical(
                        "文件分析失败后无法终结领域召回审计: "
                        "file_name=%s execution_id=%s",
                        file_name,
                        execution_id,
                        exc_info=True,
                    )
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
                    execution_id=execution_id,
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
                    execution_id=execution_id,
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
                execution_id=execution_id,
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
                execution_id=execution_id,
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
                execution_id=execution_id,
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
            task_service.require_current_execution(
                "file",
                file_name,
                execution_id,
                allowed_statuses=("0", "1"),
            )
        except (TaskExecutionConflictError, TaskStateConflictError):
            logger.warning(
                "永久知识库写入前执行身份已失效，清理本次RAG资源: "
                "file_name=%s execution_id=%s",
                file_name,
                execution_id,
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
        try:
            _store_prepared_analysis_document(
                knowledge_index_factory=knowledge_index_factory,
                execution_id=execution_id,
                file_name=file_name,
                original_name=original_name,
                mapped_result=mapped_result,
                architecture_list=architecture_list,
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
                execution_id=execution_id,
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
                execution_id=execution_id,
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
                execution_id=execution_id,
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
                "file",
                file_name,
                progress=0.65,
                message="正在翻译文档",
                status="1",
                execution_id=execution_id,
            )
            _publish_progress(progress_hub, file_name, 0.65)
            enriched_result = enrich_with_translations(
                mapped_result,
                downloaded_path,
                params.get("enableFullTranslation", True),
            )
            task_service.update_task_progress(
                "file",
                file_name,
                progress=0.95,
                message="翻译完成，准备回调",
                status="1",
                execution_id=execution_id,
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
                execution_id=execution_id,
            )
            _publish_progress(progress_hub, file_name, 1.0)
            _submit_callback(
                task_service=task_service,
                file_name=file_name,
                execution_id=execution_id,
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
        except (TaskExecutionConflictError, TaskStateConflictError):
            logger.warning(
                "文件分析知识库转交后执行身份已失效，不覆盖当前任务或发送回调: "
                "file_name=%s execution_id=%s",
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
                execution_id=execution_id,
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
    execution_id: str,
    analysis_classification_mode: str = "topk_two_stage",
    analysis_filename_constraint_mode: str = (
        ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY
    ),
    analysis_data_standard_mode: str = ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    analysis_identity_reselect_mode: str = ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
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
            execution_id=execution_id,
            analysis_classification_mode=analysis_classification_mode,
            analysis_filename_constraint_mode=analysis_filename_constraint_mode,
            analysis_data_standard_mode=analysis_data_standard_mode,
            analysis_identity_reselect_mode=analysis_identity_reselect_mode,
        )
    except (TaskExecutionConflictError, TaskStateConflictError):
        logger.warning(
            "文件分析worker执行身份已失效，停止且不写入当前任务: execution_id=%s",
            execution_id,
        )
        return
    except Exception as exc:
        params_list = request_payload.get("params", [])
        params = params_list[0] if params_list and isinstance(params_list[0], dict) else {}
        file_name = _as_text(params.get("fileName"))
        original_name = (
            _as_business_original_file_name(params.get("originalFileName"))
            or file_name
        )
        error_message = _safe_task_error(exc, fallback="文件分析编排失败")
        failure_stage = "orchestration"

        # Factory create/__enter__ 和无法提供 trace 的 Session 打开异常发生在召回
        # 决策已经写入之后。最终异常边界必须补齐该审计终态，不能把一条未终结决策
        # 永久留在库中，也不能用笼统 orchestration 隐藏稳定领域阶段。
        if file_name:
            try:
                task = task_service.require_current_execution(
                    "file",
                    file_name,
                    execution_id,
                )
            except TaskExecutionConflictError:
                logger.warning(
                    "文件分析兜底检测到执行已被替换，停止终结新任务: "
                    "file_name=%s execution_id=%s",
                    file_name,
                    execution_id,
                )
                return
            if task and _as_text(task.get("status")) in {"2", "3"}:
                # 正常/失败业务终态已经提交后，Factory 退出阶段仅可能剩下本地
                # Transport 关闭等资源告警。不得覆盖终态或再发送一份相反 callback。
                logger.critical(
                    "文件分析 Factory 退出异常，但业务任务已有终态，保持原结果: "
                    "file_name=%s status=%s error_type=%s",
                    file_name,
                    task.get("status"),
                    type(exc).__name__,
                    exc_info=True,
                )
                return
            if execution_id:
                try:
                    recall_audit = task_service.get_architecture_recall_decision(
                        execution_id
                    )
                except Exception:
                    recall_audit = None
                    logger.critical(
                        "文件分析兜底无法读取领域召回审计: execution_id=%s",
                        execution_id,
                        exc_info=True,
                    )
                if recall_audit and not recall_audit.get("finalized_at"):
                    failure_stage = "architecture_contract"
                    try:
                        task_service.finalize_architecture_recall_decision(
                            execution_id=execution_id,
                            returned_architecture_id=None,
                            returned_rank=None,
                            total_elapsed_ms=int(
                                recall_audit.get("recall_elapsed_ms") or 0
                            ),
                            failure_stage=failure_stage,
                            error_message=error_message,
                        )
                    except Exception as audit_exc:
                        error_message = _safe_task_error(
                            audit_exc,
                            fallback="领域召回终结审计失败",
                        )
                        logger.critical(
                            "文件分析兜底无法终结领域召回审计: "
                            "execution_id=%s",
                            execution_id,
                            exc_info=True,
                        )
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
                execution_id=execution_id,
                original_name=original_name,
                stage=failure_stage,
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
    execution_ids: Mapping[str, str],
    analysis_classification_mode: str = "topk_two_stage",
    analysis_filename_constraint_mode: str = (
        ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY
    ),
    analysis_data_standard_mode: str = ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    analysis_identity_reselect_mode: str = ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
) -> None:
    """按请求顺序执行批量分析，并保证每个文件分别进入两类 Factory 租约。"""
    params_list = request_payload.get("params", [])
    for params in params_list:
        if not isinstance(params, dict):
            continue
        file_name = _as_text(params.get("fileName"))
        if file_name and not _as_text(execution_ids.get(file_name)):
            raise ValueError(f"批量文件任务缺少execution_id: {file_name}")

    for index, params in enumerate(params_list):
        if not isinstance(params, dict):
            continue
        file_name = _as_text(params.get("fileName"))
        if not file_name:
            continue
        execution_id = _as_text(execution_ids[file_name])
        if index > 0:
            task_service.update_task_progress(
                "file",
                file_name,
                progress=0.0,
                message="准备开始解析",
                status="1",
                execution_id=execution_id,
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
            execution_id=execution_id,
            analysis_classification_mode=analysis_classification_mode,
            analysis_filename_constraint_mode=analysis_filename_constraint_mode,
            analysis_data_standard_mode=analysis_data_standard_mode,
            analysis_identity_reselect_mode=analysis_identity_reselect_mode,
        )
