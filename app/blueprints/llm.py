from __future__ import annotations

from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request


llm_bp = Blueprint("llm", __name__)


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

    return jsonify({"message": "accepted", "businessType": "file", "params": params}), 202


@llm_bp.post("/llm/generate-report")
def llm_generate_report():
    payload = request.get_json(silent=True) or {}
    if payload.get("businessType") != "report":
        return jsonify({"error": "businessType必须为report"}), 400

    params = _get_first_param(payload)
    if params is None:
        return jsonify({"error": "params不能为空"}), 400

    return jsonify({"message": "accepted", "businessType": "report", "params": params}), 202
