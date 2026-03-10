from __future__ import annotations

from typing import Any, Dict, Iterable


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
