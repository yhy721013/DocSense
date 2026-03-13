from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable

import fitz

from anythingllm_client import AnythingLLMClient
from config import load_anythingllm_config
from pipeline import process_file_with_rag as pipeline_process_file_with_rag

from app.services.llm_callback_service import post_callback_payload
from app.services.llm_download_service import download_to_temp_file
from app.services.llm_progress_hub import LLMProgressHub
from app.services.llm_prompts import build_file_analysis_prompt
from app.services.llm_task_service import LLMTaskService


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
    {"id": 101, "level": 1, "name": "军事基地", "path": "101", "pathName": "军事基地", "sort": 1},
    {"id": 102, "level": 1, "name": "体系运用", "path": "102", "pathName": "体系运用", "sort": 2},
    {"id": 103, "level": 1, "name": "装备型号", "path": "103", "pathName": "装备型号", "sort": 3},
    {"id": 10301, "level": 2, "name": "空中装备", "path": "103/10301", "pathName": "装备型号/空中装备", "sort": 1},
    {"id": 10302, "level": 2, "name": "水面装备", "path": "103/10302", "pathName": "装备型号/水面装备", "sort": 2},
    {"id": 10303, "level": 2, "name": "水下装备", "path": "103/10303", "pathName": "装备型号/水下装备", "sort": 3},
    {"id": 104, "level": 1, "name": "作战环境", "path": "104", "pathName": "作战环境", "sort": 4},
    {"id": 105, "level": 1, "name": "作战指挥", "path": "105", "pathName": "作战指挥", "sort": 5},
    {"id": 10501, "level": 2, "name": "条令条例", "path": "105/10501", "pathName": "作战指挥/条令条例", "sort": 1},
    {"id": 10502, "level": 2, "name": "组织机构", "path": "105/10502", "pathName": "作战指挥/组织机构", "sort": 2},
]


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
    }


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _round_score(value: Any) -> float:
    try:
        score = round(float(value), 1)
    except (TypeError, ValueError):
        return 0.0
    if score < 0:
        return 0.0
    if score > 5:
        return 5.0
    return score


def _match_option_value(value: Any, options: Iterable[Dict[str, Any]]) -> str:
    target = _scalar_text(value)
    if not target:
        return ""
    for item in options:
        if not isinstance(item, dict):
            continue
        if target in {_as_text(item.get("value")), _as_text(item.get("key"))}:
            return _as_text(item.get("value"))
    return ""


def _match_architecture_id(parsed_result: Dict[str, Any], architecture_list: Iterable[Dict[str, Any]]) -> int:
    candidate_items = [item for item in architecture_list if isinstance(item, dict)]
    candidate_ids = set()
    for item in candidate_items:
        try:
            candidate_ids.add(int(item.get("id") or 0))
        except (TypeError, ValueError):
            continue

    raw_id = _first_non_empty_value(parsed_result, "architectureId", "领域体系ID")
    if raw_id is not None:
        try:
            matched_id = int(raw_id)
            return matched_id if matched_id in candidate_ids else 0
        except (TypeError, ValueError):
            return 0

    architecture_obj = _first_non_empty_value(parsed_result, "领域体系")
    if isinstance(architecture_obj, dict):
        raw_arch_id = architecture_obj.get("id")
        if raw_arch_id is not None:
            try:
                matched_id = int(raw_arch_id)
                return matched_id if matched_id in candidate_ids else 0
            except (TypeError, ValueError):
                return 0

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
        return 0

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
                    return 0

    return 0


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
    for item in options:
        if not isinstance(item, dict):
            continue
        for key in ("value", "key"):
            candidate = _as_text(item.get(key))
            if candidate and candidate in original_text:
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
    match = re.search(r"【([^】]+?)(\d{4}年\d{1,2}月\d{1,2}日)", original_text)
    if match:
        return match.group(1).strip()
    return ""


def map_analysis_result(parsed_result: Dict[str, Any], request_params: Dict[str, Any], original_text: str = "") -> Dict[str, Any]:
    file_name = _as_text(request_params.get("fileName"))
    ranges = build_effective_analysis_ranges(request_params)
    file_item = parsed_result.get("fileDataItem")
    if not isinstance(file_item, dict):
        file_item = parsed_result.get("文件解析详细数据")
    if not isinstance(file_item, dict):
        file_item = {}

    resolved_country = _match_option_value(
        _first_non_empty_value(parsed_result, "country", "国家"),
        ranges["country"],
    )
    resolved_channel = _match_option_value(
        _first_non_empty_value(parsed_result, "channel", "渠道"),
        ranges["channel"],
    )
    resolved_maturity = _match_option_value(
        _first_non_empty_value(parsed_result, "maturity", "成熟度"),
        ranges["maturity"],
    )
    resolved_format = _match_option_value(
        _first_non_empty_value(parsed_result, "format", "格式"),
        ranges["format"],
    )

    resolved_original_link = _resolve_field(parsed_result, file_item, "originalLink", "原文链接", "链接")
    resolved_date = _resolve_field(parsed_result, file_item, "dataTime", "资料年代", "日期", "时间")
    resolved_language = _resolve_field(parsed_result, file_item, "language", "语种")
    normalized_original_text = _as_text(original_text or _resolve_field(parsed_result, file_item, "originalText", "文件原文", "原文"))
    extracted_title = _extract_title(normalized_original_text)

    return {
        "country": resolved_country or _match_option_value_from_text(ranges["country"], normalized_original_text),
        "channel": resolved_channel,
        "maturity": resolved_maturity,
        "format": resolved_format,
        "architectureId": _match_architecture_id(parsed_result, ranges["architectureList"]),
        "fileDataItem": {
            "fileName": file_name,
            "dataTime": resolved_date or _extract_date(normalized_original_text),
            "keyword": _resolve_field(parsed_result, file_item, "keyword", "keywords", "关键词"),
            "summary": _resolve_field(parsed_result, file_item, "summary", "摘要") or extracted_title,
            "score": _round_score(_first_non_empty_value(parsed_result, "score", "评分")),
            "fileNo": _resolve_field(parsed_result, file_item, "fileNo", "文件编号", "编号"),
            "source": _resolve_field(parsed_result, file_item, "source", "资料来源", "来源") or _extract_source(normalized_original_text),
            "originalLink": resolved_original_link or _extract_original_link(normalized_original_text),
            "language": resolved_language or _infer_language(normalized_original_text),
            "dataFormat": _resolve_field(parsed_result, file_item, "dataFormat", "资料格式"),
            "associatedEquipment": _resolve_field(parsed_result, file_item, "associatedEquipment", "所属装备"),
            "relatedTechnology": _resolve_field(parsed_result, file_item, "relatedTechnology", "所属技术"),
            "equipmentModel": _resolve_field(parsed_result, file_item, "equipmentModel", "装备型号"),
            "documentOverview": _resolve_field(parsed_result, file_item, "documentOverview", "文件概述", "概述")
            or extracted_title,
            "originalText": normalized_original_text,
            "documentTranslationOne": "",
            "documentTranslationTwo": "",
        },
    }


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
        return json.loads(text)
    return {}


def _read_original_text(file_path: str) -> str:
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".json", ".csv"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".pdf":
        with fitz.open(path) as doc:
            return "\n".join(page.get_text() for page in doc)
    return ""


def run_file_analysis_task(
    *,
    task_service: LLMTaskService,
    progress_hub: LLMProgressHub,
    request_payload: Dict[str, Any],
    download_root: str,
    callback_url: str,
    callback_timeout: float,
) -> None:
    params = request_payload["params"][0]
    file_name = _as_text(params.get("fileName"))
    file_path = _as_text(params.get("filePath"))

    try:
        task_service.update_task_progress("file", file_name, progress=0.15, message="正在下载文件", status="1")
        _publish_progress(progress_hub, file_name, 0.15)

        downloaded_path = download_to_temp_file(file_path, file_name, download_root, timeout=60)

        task_service.update_task_progress("file", file_name, progress=0.35, message="正在执行文档解析")
        _publish_progress(progress_hub, file_name, 0.35)

        client = AnythingLLMClient(load_anythingllm_config())
        raw_result = pipeline_process_file_with_rag(
            client=client,
            file_path=downloaded_path,
            prompt=build_file_analysis_prompt(params),
            workspace_name=f"llm-file-{int(time.time() * 1000)}",
            thread_name=f"analysis-{Path(file_name).stem}",
            user_id=1,
        )
        parsed_result = _parse_model_result(raw_result)
        mapped_result = map_analysis_result(parsed_result, params, original_text=_read_original_text(downloaded_path))
        callback_payload = build_file_callback_payload(file_name, mapped_result, status="2")

        task_service.mark_business_result("file", file_name, callback_payload, status="2", message="解析完成")
        _publish_progress(progress_hub, file_name, 1.0)

        if callback_url:
            if post_callback_payload(callback_url, callback_payload, timeout=callback_timeout):
                task_service.mark_callback_success("file", file_name)
            else:
                task_service.mark_callback_failed("file", file_name, "callback failed")
    except Exception:
        callback_payload = build_file_callback_payload(file_name, {}, status="3")
        task_service.mark_business_result("file", file_name, callback_payload, status="3", message="解析失败")
        _publish_progress(progress_hub, file_name, 1.0)
        if callback_url:
            if post_callback_payload(callback_url, callback_payload, timeout=callback_timeout):
                task_service.mark_callback_success("file", file_name)
            else:
                task_service.mark_callback_failed("file", file_name, "callback failed")
