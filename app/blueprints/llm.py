from __future__ import annotations

from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from app.services.llm_task_service import LLMTaskService
from app.settings import LLM_TASK_DB_PATH


llm_bp = Blueprint("llm", __name__)
task_service = LLMTaskService(str(LLM_TASK_DB_PATH))


def _get_first_param(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    params = payload.get("params")
    if not isinstance(params, list) or not params:
        return None
    first = params[0]
    return first if isinstance(first, dict) else None


@llm_bp.post("/llm/analysis")
def llm_analysis():
    payload = request.get_json(silent=True) or {}
    if payload.get("businessType") != "file":
        return jsonify({"error": "businessType必须为file"}), 400

    params = _get_first_param(payload)
    if params is None:
        return jsonify({"error": "params不能为空"}), 400
    file_name = params.get("fileName")
    if not isinstance(file_name, str) or not file_name.strip():
        return jsonify({"error": "fileName不能为空"}), 400

    task = task_service.create_file_task(file_name=file_name.strip(), request_payload=payload)
    return jsonify({"message": "accepted", "businessType": "file", "task": task}), 202


@llm_bp.post("/llm/generate-report")
def llm_generate_report():
    payload = request.get_json(silent=True) or {}
    if payload.get("businessType") != "report":
        return jsonify({"error": "businessType必须为report"}), 400

    params = _get_first_param(payload)
    if params is None:
        return jsonify({"error": "params不能为空"}), 400
    report_id = params.get("reportId")
    if report_id is None:
        return jsonify({"error": "reportId不能为空"}), 400

    task = task_service.create_report_task(report_id=int(report_id), request_payload=payload)
    return jsonify({"message": "accepted", "businessType": "report", "task": task}), 202
