from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable

import fitz

from app.ports import DocumentRagFactory
from app.services.utils.anythingllm_client import AnythingLLMClient
from app.services.core.config import load_anythingllm_config, load_ocr_config
from app.services.utils.ocr_preprocessor import prepare_analysis_file_for_upload
from app.services.utils.rag_pipeline import RAGExecutionDetails, run_anythingllm_rag

from app.services.utils.callback_client import post_callback_payload
from app.services.utils.file_downloader import download_to_temp_file
from app.services.utils.mhtml_normalizer import extract_text_from_mhtml, is_mhtml_file, normalize_file_for_llm
from app.services.core.progress_hub import LLMProgressHub
from app.services.core.prompts import build_file_analysis_prompt
from app.services.llm_service.task_service import LLMTaskService
from app.services.llm_service.translation_service import get_translation_service
from app.services.core.database import DatabaseService


logger = logging.getLogger(__name__)

DEFAULT_COUNTRY_OPTIONS = [
    {"key": "02", "value": "美国"},
    {"key": "03", "value": "俄罗斯"},
    {"key": "04", "value": "日本"},
    {"key": "05", "value": "英国"},
    {"key": "06", "value": "法国"},
]

DEFAULT_CHANNEL_OPTIONS = [
    {"key": "02", "value": "装发"},
    {"key": "03", "value": "军情"},
    {"key": "04", "value": "科技"},
    {"key": "05", "value": "训练"},
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
        "channel": _normalize_range_list(request_params.get("channel"), DEFAULT_CHANNEL_OPTIONS),
        "format": _normalize_range_list(request_params.get("format"), DEFAULT_FORMAT_OPTIONS),
        "maturity": _normalize_range_list(request_params.get("maturity"), DEFAULT_MATURITY_OPTIONS),
        "architectureList": _normalize_range_list(request_params.get("architectureList"), DEFAULT_ARCHITECTURE_OPTIONS),
        "architectureStandardList": _normalize_range_list(request_params.get("architectureStandardList"), []),
    }


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


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


def _match_data_standard_architecture_id(
        architecture_list: Iterable[Dict[str, Any]],
        *context_values: Any,
) -> int | None:
    if not _contains_gjb_standard_reference(*context_values):
        return None

    for item in architecture_list:
        if not isinstance(item, dict):
            continue
        names = [_as_text(item.get("name")), _as_text(item.get("pathName"))]
        if any("数据标准" in name for name in names):
            try:
                return int(item.get("id"))
            except (TypeError, ValueError):
                return None
    return None


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


def _match_architecture_id(parsed_result: Dict[str, Any], architecture_list: Iterable[Dict[str, Any]]) -> int:
    def _fallback(reason: str, detail: Any = None) -> int:
        if detail is None:
            logger.info(
                "architectureId匹配失败: reason=%s fallback=%s",
                reason,
                ARCHITECTURE_FALLBACK_ID,
            )
        else:
            logger.info(
                "architectureId匹配失败: reason=%s detail=%s fallback=%s",
                reason,
                detail,
                ARCHITECTURE_FALLBACK_ID,
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
            logger.warning("keyword 被截断: 原始长度=%d 超过上限 %d", len(kw), MAX_KEYWORD_LENGTH)
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


def map_analysis_result(parsed_result: Dict[str, Any], request_params: Dict[str, Any], original_text: str = "") -> Dict[
    str, Any]:
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
    raw_format = _first_non_empty_value(parsed_result, "format", "格式")
    if raw_format in (None, "", [], {}):
        raw_format = _first_non_empty_value(file_item, "dataFormat", "资料格式")

    resolved_country = _match_option_value(raw_country, ranges["country"])
    resolved_channel = _match_option_value(raw_channel, ranges["channel"])
    resolved_maturity = _match_option_value(raw_maturity, ranges["maturity"])
    resolved_format = _match_option_value(raw_format, ranges["format"])

    for field_name, raw_value, resolved_value in (
        ("country", raw_country, resolved_country),
        ("channel", raw_channel, resolved_channel),
        ("maturity", raw_maturity, resolved_maturity),
        ("format", raw_format, resolved_format),
    ):
        if raw_value not in (None, "", [], {}) and not resolved_value:
            logger.info("字段候选匹配失败: field=%s raw=%s", field_name, _scalar_text(raw_value))

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
            logger.info(f"[LLMAnalysis] 开始全文翻译：{file_path}")

            # 【新增】定义进度回调函数，将翻译进度反馈到任务状态
            def translation_progress_callback(progress: float, message: str):
                # 计算总体进度（翻译占 0.35~0.95 区间，共 0.6 权重）
                overall_progress = 0.35 + (progress * 0.6)
                logger.info(f"[LLMAnalysis] 翻译进度：{message} ({overall_progress:.0%})")

            # 设置进度回调
            translation_service.set_progress_callback(translation_progress_callback)

            bilingual_html_content, monolingual_html_content = translation_service.translate_document(
                file_path=file_path,
                target_lang="Chinese",
                translate_all=0,
                fast_translate=True,
                use_minerU= True,
            )

            mapped_result["fileDataItem"]["documentTranslationOne"] = monolingual_html_content
            mapped_result["fileDataItem"]["documentTranslationTwo"] = bilingual_html_content

        else:
            # 快速模式：只翻译摘要
            if summary:
                logger.info(f"[LLMAnalysis] 翻译摘要：{summary[:50]}...")
                translated_summary = translation_service.translate_text_only(summary)
                mapped_result["fileDataItem"]["documentTranslationOne"] = translated_summary
                mapped_result["fileDataItem"]["documentTranslationTwo"] = summary+"\n"+translated_summary

        return mapped_result

    except Exception as e:
        logger.info(f"[LLMAnalysis] 翻译过程中出错：{e}，返回未翻译的结果")
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


def _parse_model_result(raw_result: Any) -> Dict[str, Any]:
    if isinstance(raw_result, dict):
        return raw_result
    if isinstance(raw_result, str):
        text = raw_result.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            # 尝试从文本中提取 JSON 块
            # 优先查找完整的 { ... }
            match = re.search(r"(\{[\s\S]*\})", text)
            if not match:
                # 如果没有完整的 {}，且文本包含 {，则尝试从第一个 { 提取到末尾（可能是截断）
                match = re.search(r"(\{[\s\S]*)", text)
            
            if match:
                extracted = match.group(1)
                try:
                    return json.loads(extracted)
                except json.JSONDecodeError:
                    # 如果还是失败，尝试通过补全右括号来处理截断问题
                    # 即使原本有 }，补齐额外的 } 也可能让部分被解析
                    for _ in range(5):
                        extracted += "}"
                        try:
                            return json.loads(extracted)
                        except json.JSONDecodeError:
                            continue
            
            logger.error("解析模型结果 JSON 失败: %s. 原始文本: %s", e, text)
            return {}
    return {}


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


def _prepare_analysis_upload_files(file_path: str) -> list[str]:
    path = Path(file_path)
    if not path.exists():
        return []

    upload_path = prepare_analysis_file_for_upload(str(path), load_ocr_config())
    upload_path_obj = Path(upload_path)
    if not upload_path_obj.exists():
        return [str(path)]

    return [str(upload_path_obj)]


def run_file_analysis_task(
        *,
        task_service: LLMTaskService,
        kb_service: DatabaseService,
        progress_hub: LLMProgressHub,
        request_payload: Dict[str, Any],
        download_root: str,
        callback_url: str,
        callback_timeout: float,
        document_rag_factory: DocumentRagFactory | None = None,
) -> None:
    """执行单文件分析任务。

    ``document_rag_factory`` 是阶段 6 建立的任务级注入接缝。当前阶段仍执行 legacy RAG
    流程，因此只接收但不进入 Factory 租约，确保改造期间不会创建一套未使用的 Transport。
    阶段 7 将把该参数改为必需依赖，并在本任务函数内部使用 ``with factory.create()``
    完成纯方案 B 迁移。
    """
    params = request_payload["params"][0]
    file_name = _as_text(params.get("fileName"))
    original_name = _as_text(params.get("originalFileName")) or file_name
    file_path = _as_text(params.get("filePath"))
    client: AnythingLLMClient | None = None
    analysis_prompt = ""
    raw_result: str | None = None
    task_error = ""
    rag_details = RAGExecutionDetails()

    logger.info("开始执行文件分析任务: file_name=%s", file_name)
    logger.debug(
        "文件分析任务依赖已装配: file_name=%s document_rag_factory_injected=%s",
        file_name,
        document_rag_factory is not None,
    )

    try:
        task_service.update_task_progress("file", file_name, progress=0.15, message="正在下载文件", status="1")
        _publish_progress(progress_hub, file_name, 0.15)

        downloaded_path = download_to_temp_file(file_path, file_name, download_root, timeout=60)

        task_service.update_task_progress("file", file_name, progress=0.35, message="正在执行文档解析")
        _publish_progress(progress_hub, file_name, 0.35)

        llm_file_path = downloaded_path
        try:
            llm_file_path = normalize_file_for_llm(downloaded_path)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("mhtml归一化失败，降级使用原文件: %s (%s)", downloaded_path, exc)

        client = AnythingLLMClient(load_anythingllm_config())
        files_to_upload = _prepare_analysis_upload_files(llm_file_path)
        if files_to_upload:
            llm_file_path = files_to_upload[0]

        analysis_prompt = build_file_analysis_prompt(params)
        temporary_workspace_name = f"llm-file-{int(time.time() * 1000)}"
        raw_result = run_anythingllm_rag(
            client=client,
            files_to_upload=files_to_upload,
            prompt=analysis_prompt,
            workspace_name=temporary_workspace_name,
            thread_name=f"analysis-{Path(file_name).stem}",
            user_id=1,
            mode="query",
            reuse_workspace=False,
            execution_details=rag_details,
        )
        if rag_details.text_response is None and isinstance(raw_result, str):
            rag_details.text_response = raw_result
        if raw_result is None or (isinstance(raw_result, str) and not raw_result.strip()):
            raise RuntimeError("AnythingLLM结构化抽取未返回有效结果")
        parsed_result = _parse_model_result(raw_result)
        if not isinstance(parsed_result, dict) or not parsed_result:
            raise RuntimeError("AnythingLLM结构化抽取结果无法解析")
        mapped_result = map_analysis_result(parsed_result, params, original_text=_read_original_text(llm_file_path))

        try:
            result_architecture_id = mapped_result.get("architectureId")
            if result_architecture_id:
                architecture_list = build_effective_analysis_ranges(params)["architectureList"]
                storage_architecture_id = resolve_storage_architecture_id(
                    result_architecture_id,
                    architecture_list,
                )
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

                workspace_slug = kb_service.get_workspace_slug(storage_architecture_id)
                if not workspace_slug:
                    workspace_name = f"architectureId-{storage_architecture_id}"
                    ws_info = client.create_rag_workspace(workspace_name, user_id=1)
                    if ws_info and ws_info.get("slug"):
                        workspace_slug = ws_info["slug"]
                        kb_service.add_workspace(storage_architecture_id, workspace_slug)

                if workspace_slug:
                    doc_info = client.upload_document(llm_file_path, user_id=1)
                    if doc_info:
                        doc_id = doc_info.get("id") or doc_info.get("docId")
                        filename = Path(llm_file_path).name
                        doc_relative_path = (
                            doc_info.get("location")
                            or doc_info.get("docpath")
                            or f"custom-documents/{filename}-{doc_id}.json"
                        )
                        
                        client.wait_for_processing(doc_relative_path)
                        
                        metadata = {
                            "file_name": file_name,
                            "architecture_id": storage_architecture_id,
                        }
                        for k in ["country", "channel", "maturity", "format"]:
                            if mapped_result.get(k):
                                metadata[k] = mapped_result[k]
                                
                        if not client.update_embeddings(doc_relative_path, workspace_slug, user_id=1, metadata=metadata):
                            alt_path = f"custom-documents/{doc_id}.json"
                            client.update_embeddings(alt_path, workspace_slug, user_id=1, metadata=metadata)
                            
                        if doc_id:
                            kb_service.save_document_record(
                                file_name,
                                storage_architecture_id,
                                str(doc_id),
                                doc_path=doc_relative_path,
                                original_name=original_name,
                            )
        except Exception as e:
            logger.error("知识库尝试存入文件失败: %s", e)

        # 【新增】在回调前添加翻译
        task_service.update_task_progress("file", file_name, progress=0.65, message="正在翻译文档", status="1")
        _publish_progress(progress_hub, file_name, 0.65)
        # 根据配置决定是否启用全文翻译（可通过环境变量或请求参数控制）
        enable_full_translation = params.get("enableFullTranslation", True)
        enriched_result = enrich_with_translations(mapped_result, downloaded_path, enable_full_translation)
        # 翻译完成后更新进度到 0.95（接近完成）
        task_service.update_task_progress("file", file_name, progress=0.95, message="翻译完成，准备回调", status="1")
        _publish_progress(progress_hub, file_name, 0.95)

        callback_payload = build_file_callback_payload(file_name, enriched_result, status="2")
        task_service.mark_business_result("file", file_name, callback_payload, status="2", message="解析完成")
        _publish_progress(progress_hub, file_name, 1.0)

        if callback_url:
            callback_context = {
                "businessType": "file",
                "fileName": file_name,
                "originalFileName": original_name,
            }
            if post_callback_payload(
                callback_url,
                callback_payload,
                timeout=callback_timeout,
                callback_context=callback_context,
            ):
                task_service.mark_callback_success("file", file_name)
                logger.info("回调结果提交成功: file_name=%s", file_name)
            else:
                task_service.mark_callback_failed("file", file_name, "callback failed")
                logger.warning("回调结果提交失败: file_name=%s", file_name)

        logger.info("文件分析任务完成: file_name=%s", file_name)

    except Exception as e:
        task_error = str(e)
        logger.exception("文件分析任务执行异常: file_name=%s, error=%s", file_name, e)
        callback_payload = build_file_callback_payload(file_name, {}, status="3")
        task_service.mark_business_result("file", file_name, callback_payload, status="3", message="解析失败")
        _publish_progress(progress_hub, file_name, 1.0)
        if callback_url:
            callback_context = {
                "businessType": "file",
                "fileName": file_name,
                "originalFileName": original_name,
            }
            if post_callback_payload(
                callback_url,
                callback_payload,
                timeout=callback_timeout,
                callback_context=callback_context,
            ):
                task_service.mark_callback_success("file", file_name)
                logger.info("失败回调提交成功: file_name=%s", file_name)
            else:
                task_service.mark_callback_failed("file", file_name, "callback failed")
                logger.warning("失败回调提交失败: file_name=%s", file_name)
    finally:
        is_temporary_workspace = (
            rag_details.workspace_created
            and bool(rag_details.workspace_slug)
            and rag_details.workspace_name.startswith("llm-file-")
        )
        if is_temporary_workspace and client is not None:
            interaction_id: int | None = None
            try:
                interaction_succeeded = bool(rag_details.text_response and rag_details.text_response.strip())
                interaction_id = task_service.create_llm_interaction(
                    business_type="file",
                    business_key=file_name,
                    workspace_name=rag_details.workspace_name,
                    workspace_slug=rag_details.workspace_slug or "",
                    thread_slug=rag_details.thread_slug or "",
                    prompt=analysis_prompt,
                    response=rag_details.raw_response or rag_details.text_response,
                    sources=rag_details.sources,
                    status="succeeded" if interaction_succeeded else "failed",
                    error_message="" if interaction_succeeded else (task_error or "模型未返回有效结果"),
                )
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    "LLM交互落库失败，保留临时Workspace避免对话丢失: file_name=%s, workspace=%s",
                    file_name,
                    rag_details.workspace_slug,
                )

            if interaction_id is not None:
                cleanup_status = "failed"
                cleanup_error = "AnythingLLM删除Workspace失败"
                try:
                    if client.delete_workspace(rag_details.workspace_slug or "", user_id=1):
                        cleanup_status = "deleted"
                        cleanup_error = ""
                    else:
                        logger.warning(
                            "删除文件分析临时Workspace失败: file_name=%s, workspace=%s",
                            file_name,
                            rag_details.workspace_slug,
                        )
                except Exception as cleanup_exc:  # pylint: disable=broad-except
                    cleanup_error = str(cleanup_exc)
                    logger.warning(
                        "删除文件分析临时Workspace异常: file_name=%s, workspace=%s, error=%s",
                        file_name,
                        rag_details.workspace_slug,
                        cleanup_exc,
                    )

                try:
                    task_service.update_llm_interaction_cleanup(
                        interaction_id,
                        status=cleanup_status,
                        error_message=cleanup_error,
                    )
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "更新Workspace清理状态失败: interaction_id=%s, workspace=%s",
                        interaction_id,
                        rag_details.workspace_slug,
                    )


def run_file_analysis_batch_task(
        *,
        task_service: LLMTaskService,
        kb_service: DatabaseService,
        progress_hub: LLMProgressHub,
        request_payload: Dict[str, Any],
        download_root: str,
        callback_url: str,
        callback_timeout: float,
        document_rag_factory: DocumentRagFactory | None = None,
) -> None:
    """按请求顺序执行批量文件分析，并向每个子任务传递同一无状态 Factory。"""
    params_list = request_payload.get("params", [])
    for index, params in enumerate(params_list):
        if not isinstance(params, dict):
            continue
        file_name = _as_text(params.get("fileName"))
        if not file_name:
            continue

        if index > 0:
            task_service.update_task_progress("file", file_name, progress=0.0, message="准备开始解析", status="1")
            _publish_progress(progress_hub, file_name, 0.0)

        run_file_analysis_task(
            task_service=task_service,
            kb_service=kb_service,
            progress_hub=progress_hub,
            request_payload={"businessType": "file", "params": [params]},
            download_root=download_root,
            callback_url=callback_url,
            callback_timeout=callback_timeout,
            document_rag_factory=document_rag_factory,
        )
