from __future__ import annotations

import json
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
    target = _as_text(value)
    if not target:
        return ""
    for item in options:
        if not isinstance(item, dict):
            continue
        if target in {_as_text(item.get("value")), _as_text(item.get("key"))}:
            return _as_text(item.get("value"))
    return ""


def _match_architecture_id(parsed_result: Dict[str, Any], architecture_list: Iterable[Dict[str, Any]]) -> int:
    raw_id = parsed_result.get("architectureId")
    if raw_id is not None:
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            return 0

    target_name = _as_text(parsed_result.get("architectureName") or parsed_result.get("architecture"))
    if not target_name:
        return 0

    for item in architecture_list:
        if not isinstance(item, dict):
            continue
        if target_name == _as_text(item.get("name")):
            try:
                return int(item.get("id") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def map_analysis_result(parsed_result: Dict[str, Any], request_params: Dict[str, Any], original_text: str = "") -> Dict[str, Any]:
    file_name = _as_text(request_params.get("fileName"))
    return {
        "country": _match_option_value(parsed_result.get("country"), request_params.get("country", [])),
        "channel": _match_option_value(parsed_result.get("channel"), request_params.get("channel", [])),
        "maturity": _match_option_value(parsed_result.get("maturity"), request_params.get("maturity", [])),
        "format": _match_option_value(parsed_result.get("format"), request_params.get("format", [])),
        "architectureId": _match_architecture_id(parsed_result, request_params.get("architectureList", [])),
        "fileDataItem": {
            "fileName": file_name,
            "dataTime": _as_text(parsed_result.get("dataTime")),
            "keyword": _as_text(parsed_result.get("keyword")),
            "summary": _as_text(parsed_result.get("summary")),
            "score": _round_score(parsed_result.get("score")),
            "fileNo": _as_text(parsed_result.get("fileNo")),
            "source": _as_text(parsed_result.get("source")),
            "originalLink": _as_text(parsed_result.get("originalLink")),
            "language": _as_text(parsed_result.get("language")),
            "dataFormat": _as_text(parsed_result.get("dataFormat")),
            "associatedEquipment": _as_text(parsed_result.get("associatedEquipment")),
            "relatedTechnology": _as_text(parsed_result.get("relatedTechnology")),
            "equipmentModel": _as_text(parsed_result.get("equipmentModel")),
            "documentOverview": _as_text(parsed_result.get("documentOverview")),
            "originalText": _as_text(original_text or parsed_result.get("originalText")),
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
