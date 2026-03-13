from __future__ import annotations

import json
import threading
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request
from flask_sock import Sock

from config import load_llm_integration_config
from app.services.llm_analysis_service import run_file_analysis_task
from app.services.llm_progress_hub import LLMProgressHub
from app.services.llm_report_service import run_report_task
from app.services.llm_task_service import LLMTaskService
from app.settings import LLM_TASK_DB_PATH


llm_bp = Blueprint("llm", __name__)
sock = Sock()
task_service = LLMTaskService(str(LLM_TASK_DB_PATH))
llm_config = load_llm_integration_config()
progress_hub = LLMProgressHub()


def _get_first_param(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    params = payload.get("params")
    if not isinstance(params, list) or not params:
        return None
    first = params[0]
    return first if isinstance(first, dict) else None


def _extract_progress_key(payload: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    business_type = payload.get("businessType")
    if business_type not in {"file", "report"}:
        return None, None

    params = _get_first_param(payload)
    if params is None:
        return None, None

    if business_type == "file":
        file_name = params.get("fileName")
        if not isinstance(file_name, str) or not file_name.strip():
            return None, None
        return business_type, file_name.strip()

    report_id = params.get("reportId")
    if report_id is None:
        return None, None
    return business_type, str(report_id)


@llm_bp.post("/llm/analysis")
def llm_analysis():
    payload = request.get_json(silent=True) or {}
    if payload.get("businessType") != "file":
        return jsonify({"error": "businessType必须为file"}), 400

    raw_params = payload.get("params")
    if not isinstance(raw_params, list) or not raw_params:
        return jsonify({"error": "params不能为空"}), 400

    accepted_tasks = []
    for param in raw_params:
        if not isinstance(param, dict):
            continue
        file_name = param.get("fileName")
        if not isinstance(file_name, str) or not file_name.strip():
            continue
        file_path = param.get("filePath")
        if not isinstance(file_path, str) or not file_path.strip():
            continue

        file_name = file_name.strip()
        task = task_service.create_file_task(file_name=file_name, request_payload=payload)
        progress_hub.publish(
            "file",
            file_name,
            {"businessType": "file", "data": {"fileName": file_name, "progress": 0.0}},
        )

        worker = threading.Thread(
            target=run_file_analysis_task,
            kwargs={
                "task_service": task_service,
                "progress_hub": progress_hub,
                "request_params": param,
                "download_root": llm_config.download_dir,
                "callback_url": llm_config.callback_url or "",
                "callback_timeout": llm_config.callback_timeout,
            },
            daemon=True,
        )
        worker.start()
        accepted_tasks.append(task)

    if not accepted_tasks:
        return jsonify({"error": "params中没有有效的文件信息"}), 400

    return jsonify({"message": "accepted", "businessType": "file", "tasks": accepted_tasks}), 202


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
    file_path_list = params.get("filePathList")
    if not isinstance(file_path_list, list) or not file_path_list:
        return jsonify({"error": "filePathList不能为空"}), 400

    task = task_service.create_report_task(report_id=int(report_id), request_payload=payload)
    progress_hub.publish(
        "report",
        str(report_id),
        {"businessType": "report", "data": {"reportId": int(report_id), "progress": 0.0}},
    )

    worker = threading.Thread(
        target=run_report_task,
        kwargs={
            "task_service": task_service,
            "progress_hub": progress_hub,
            "request_payload": payload,
            "download_root": llm_config.download_dir,
            "callback_url": llm_config.callback_url or "",
            "callback_timeout": llm_config.callback_timeout,
        },
        daemon=True,
    )
    worker.start()
    return jsonify({"message": "accepted", "businessType": "report", "task": task}), 202


@llm_bp.post("/llm/check-task")
def llm_check_task():
    payload = request.get_json(silent=True) or {}
    business_type = payload.get("businessType")
    if business_type not in {"file", "report"}:
        return jsonify({"error": "businessType无效"}), 400

    raw_params = payload.get("params")
    if not isinstance(raw_params, list) or not raw_params:
        return jsonify({"error": "params不能为空"}), 400

    results = []
    for param in raw_params:
        if not isinstance(param, dict):
            continue

        if business_type == "file":
            business_key = param.get("fileName")
            if not isinstance(business_key, str) or not business_key.strip():
                continue
            response_key = "fileName"
            normalized_key = business_key.strip()
        else:
            report_id = param.get("reportId")
            if report_id is None:
                continue
            response_key = "reportId"
            normalized_key = str(report_id)

        task = task_service.get_task(business_type, normalized_key)
        if not task:
            continue

        task_service.replay_callback_if_needed(
            business_type,
            normalized_key,
            callback_url=llm_config.callback_url or "",
            timeout=llm_config.callback_timeout,
        )
        task = task_service.get_task(business_type, normalized_key)
        assert task is not None

        results.append(
            {
                response_key: int(normalized_key) if business_type == "report" else normalized_key,
                "status": task["status"],
                "progress": task["progress"],
                "callbackStatus": task["callback_status"],
            }
        )

    if not results:
        return jsonify({"error": "任务不存在"}), 404

    return jsonify(
        {
            "businessType": business_type,
            "data": results,
        }
    )


@sock.route("/llm/progress")
def llm_progress(ws):
    raw_message = ws.receive()
    if raw_message is None:
        return

    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        ws.send(json.dumps({"error": "订阅消息不是合法JSON"}, ensure_ascii=False))
        return

    business_type, business_key = _extract_progress_key(payload)
    if not business_type or not business_key:
        ws.send(json.dumps({"error": "订阅参数无效"}, ensure_ascii=False))
        return

    def _forward(message: Dict[str, Any]) -> None:
        ws.send(json.dumps(message, ensure_ascii=False))

    progress_hub.subscribe(business_type, business_key, _forward)
    try:
        current_task = task_service.get_task(business_type, business_key)
        if current_task is not None:
            message = {"businessType": business_type, "data": {"progress": current_task["progress"]}}
            if business_type == "file":
                message["data"]["fileName"] = business_key
            else:
                message["data"]["reportId"] = int(business_key)
            _forward(message)
        while True:
            raw_query = ws.receive()
            if raw_query is None:
                break
            try:
                query = json.loads(raw_query)
            except json.JSONDecodeError:
                continue
            query_bt, query_key = _extract_progress_key(query)
            if not query_bt or not query_key:
                continue
            current = task_service.get_task(query_bt, query_key)
            if current is None:
                continue
            response: Dict[str, Any] = {"businessType": query_bt, "data": {"progress": current["progress"]}}
            if query_bt == "file":
                response["data"]["fileName"] = query_key
            else:
                response["data"]["reportId"] = int(query_key)
            try:
                ws.send(json.dumps(response, ensure_ascii=False))
            except Exception:
                break
    finally:
        progress_hub.unsubscribe(business_type, business_key, _forward)
