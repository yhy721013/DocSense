from __future__ import annotations

import json
import threading
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request
from flask_sock import Sock

from config import load_llm_integration_config
from app.services.llm_analysis_service import run_file_analysis_batch_task, run_file_analysis_task
from app.services.llm_progress_hub import LLMProgressHub
from app.services.llm_report_service import run_report_task
from app.services.llm_task_service import LLMTaskService
from app.services.llm_weaponry_service import run_weaponry_task
from app.settings import LLM_TASK_DB_PATH, KNOWLEDGE_BASE_DB_PATH
from app.services.knowledge_base.database_service import DatabaseService


llm_bp = Blueprint("llm", __name__)
sock = Sock()
task_service = LLMTaskService(str(LLM_TASK_DB_PATH))
kb_service = DatabaseService(str(KNOWLEDGE_BASE_DB_PATH))
llm_config = load_llm_integration_config()
progress_hub = LLMProgressHub()


def _get_params(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    params = payload.get("params")
    if not isinstance(params, list) or not params:
        return []
    return [item for item in params if isinstance(item, dict)]


def _get_first_param(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    params = _get_params(payload)
    return params[0] if params else None


def _extract_progress_key(payload: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    business_type = payload.get("businessType")
    if business_type not in {"file", "report", "weaponry"}:
        return None, None

    params = _get_first_param(payload)
    if params is None:
        return None, None

    if business_type == "file":
        file_name = params.get("fileName")
        if not isinstance(file_name, str) or not file_name.strip():
            return None, None
        return business_type, file_name.strip()

    if business_type == "weaponry":
        architecture_id = params.get("architectureId")
        if architecture_id is None:
            return None, None
        return business_type, str(architecture_id)

    report_id = params.get("reportId")
    if report_id is None:
        return None, None
    return business_type, str(report_id)


def _parse_progress_command(payload: Dict[str, Any]) -> Dict[str, Any]:
    action = payload.get("action") or "subscribe"
    if action not in {"subscribe", "unsubscribe", "query"}:
        raise ValueError("不支持的action")

    business_type = payload.get("businessType")
    if business_type not in {"file", "report", "weaponry"}:
        raise ValueError("businessType无效")

    params_list = _get_params(payload)
    if not params_list:
        raise ValueError("params不能为空")

    keys = []
    for params in params_list:
        if business_type == "file":
            file_name = params.get("fileName")
            if not isinstance(file_name, str) or not file_name.strip():
                raise ValueError("fileName不能为空")
            keys.append((business_type, file_name.strip()))
        elif business_type == "weaponry":
            architecture_id = params.get("architectureId")
            if architecture_id is None:
                raise ValueError("architectureId不能为空")
            keys.append((business_type, str(architecture_id)))
        else:
            report_id = params.get("reportId")
            if report_id is None:
                raise ValueError("reportId不能为空")
            keys.append((business_type, str(report_id)))

    return {"action": action, "business_type": business_type, "keys": keys}


def _build_progress_snapshot(business_type: str, business_key: str, task: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "progress": 0.0,
    }
    if business_type == "file":
        data["fileName"] = business_key
    elif business_type == "report":
        data["reportId"] = int(business_key)
    elif business_type == "weaponry":
        data["architectureId"] = business_key

    if task is not None:
        data["progress"] = task["progress"]
    else:
        data["exists"] = False

    return {"businessType": business_type, "data": data}


def _send_latest_progress(send_message, business_type: str, business_key: str) -> None:
    latest = progress_hub.get_latest(business_type, business_key)
    if latest is not None:
        send_message(latest)
        return

    current_task = task_service.get_task(business_type, business_key)
    send_message(_build_progress_snapshot(business_type, business_key, current_task))


def _handle_progress_command(send_message, subscriptions: dict[tuple[str, str], Any], command: Dict[str, Any], *, emit_ack: bool) -> None:
    action = command["action"]
    keys = command["keys"]

    if action == "subscribe":
        for business_type, business_key in keys:
            key = (business_type, business_key)
            if key not in subscriptions:
                def _forward(message: Dict[str, Any]) -> None:
                    send_message(message)

                subscriptions[key] = _forward
                progress_hub.subscribe(business_type, business_key, _forward)

                if progress_hub.get_latest(business_type, business_key) is None:
                    current_task = task_service.get_task(business_type, business_key)
                    send_message(_build_progress_snapshot(business_type, business_key, current_task))
                continue

            _send_latest_progress(send_message, business_type, business_key)

        if emit_ack:
            send_message({"type": "ack", "action": action, "count": len(keys)})
        return

    if action == "query":
        for business_type, business_key in keys:
            _send_latest_progress(send_message, business_type, business_key)

        if emit_ack:
            send_message({"type": "ack", "action": action, "count": len(keys)})
        return

    for business_type, business_key in keys:
        callback = subscriptions.pop((business_type, business_key), None)
        if callback is not None:
            progress_hub.unsubscribe(business_type, business_key, callback)

    if emit_ack:
        send_message({"type": "ack", "action": action, "count": len(keys)})


@llm_bp.post("/llm/analysis")
def llm_analysis():
    payload = request.get_json(silent=True) or {}
    if payload.get("businessType") != "file":
        return jsonify({"error": "businessType必须为file"}), 400

    params_list = _get_params(payload)
    if not params_list:
        return jsonify({"error": "params不能为空"}), 400

    seen_file_names = set()
    for params in params_list:
        file_name = params.get("fileName")
        if not isinstance(file_name, str) or not file_name.strip():
            return jsonify({"error": "fileName不能为空"}), 400
        normalized_name = file_name.strip()
        if normalized_name in seen_file_names:
            return jsonify({"error": "fileName不能重复"}), 400
        seen_file_names.add(normalized_name)

        file_path = params.get("filePath")
        if not isinstance(file_path, str) or not file_path.strip():
            return jsonify({"error": "filePath不能为空"}), 400

        existing_task = task_service.get_task("file", normalized_name)
        if existing_task and existing_task["status"] in {"0", "1"}:
            return jsonify({"error": "任务正在处理中"}), 409

    tasks = []
    for index, params in enumerate(params_list):
        file_name = params["fileName"]
        task = task_service.create_file_task(
            file_name=file_name.strip(),
            request_payload={"businessType": "file", "params": [params]},
            status="1" if index == 0 else "0",
        )
        tasks.append(task)
        progress_hub.publish(
            "file",
            file_name.strip(),
            {"businessType": "file", "data": {"fileName": file_name.strip(), "progress": 0.0}},
        )

    worker = threading.Thread(
        target=run_file_analysis_task if len(tasks) == 1 else run_file_analysis_batch_task,
        kwargs={
            "task_service": task_service,
            "kb_service": kb_service,
            "progress_hub": progress_hub,
            "request_payload": payload if len(tasks) > 1 else {"businessType": "file", "params": [params_list[0]]},
            "download_root": llm_config.download_dir,
            "callback_url": llm_config.callback_url or "",
            "callback_timeout": llm_config.callback_timeout,
        },
        daemon=True,
    )
    worker.start()
    if len(tasks) == 1:
        return jsonify({"message": "accepted", "businessType": "file", "task": tasks[0]}), 202
    return jsonify({"message": "accepted", "businessType": "file", "tasks": tasks}), 202


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


@llm_bp.post("/llm/weaponry")
def llm_weaponry():
    payload = request.get_json(silent=True) or {}
    if payload.get("businessType") != "weaponry":
        return jsonify({"error": "businessType必须为weaponry"}), 400

    params = payload.get("params")
    if not isinstance(params, dict):
        return jsonify({"error": "params不能为空"}), 400

    architecture_id = params.get("architectureId")
    if architecture_id is None:
        return jsonify({"error": "architectureId不能为空"}), 400

    field_list = params.get("weaponryTemplateFieldList")
    if not isinstance(field_list, list) or not field_list:
        return jsonify({"error": "weaponryTemplateFieldList不能为空"}), 400

    # 校验 analyseData / analyseDataSource 必须为空
    for field in field_list:
        if field.get("analyseData") or field.get("analyseDataSource"):
            return jsonify({"error": "analyseData和analyseDataSource必须清空"}), 400
        if field.get("fieldType") == "TABLE":
            for row in (field.get("tableFieldList") or []):
                if isinstance(row, list):
                    for cell in row:
                        if isinstance(cell, dict) and (cell.get("analyseData") or cell.get("analyseDataSource")):
                            return jsonify({"error": "analyseData和analyseDataSource必须清空"}), 400

    architecture_id_str = str(architecture_id)
    existing_task = task_service.get_task("weaponry", architecture_id_str)
    if existing_task and existing_task["status"] in {"0", "1"}:
        return jsonify({"error": "任务正在处理中"}), 409

    task = task_service.create_weaponry_task(
        architecture_id=architecture_id,
        request_payload=payload,
    )
    progress_hub.publish(
        "weaponry",
        architecture_id_str,
        {"businessType": "weaponry", "data": {"architectureId": architecture_id_str, "progress": 0.0}},
    )

    worker = threading.Thread(
        target=run_weaponry_task,
        kwargs={
            "task_service": task_service,
            "kb_service": kb_service,
            "progress_hub": progress_hub,
            "request_payload": payload,
            "callback_url": llm_config.callback_url or "",
            "callback_timeout": llm_config.callback_timeout,
        },
        daemon=True,
    )
    worker.start()
    return jsonify({"message": "accepted", "businessType": "weaponry", "task": task}), 202


@llm_bp.post("/llm/check-task")
def llm_check_task():
    payload = request.get_json(silent=True) or {}
    business_type = payload.get("businessType")
    if business_type not in {"file", "report", "weaponry"}:
        return jsonify({"error": "businessType无效"}), 400

    params_list = _get_params(payload)
    if not params_list:
        return jsonify({"error": "params不能为空"}), 400

    items = []
    for params in params_list:
        if business_type == "file":
            business_key = params.get("fileName")
            if not isinstance(business_key, str) or not business_key.strip():
                return jsonify({"error": "fileName不能为空"}), 400
            response_key = "fileName"
            normalized_key = business_key.strip()
            response_value: Any = normalized_key
        elif business_type == "weaponry":
            architecture_id = params.get("architectureId")
            if architecture_id is None:
                return jsonify({"error": "architectureId不能为空"}), 400
            response_key = "architectureId"
            normalized_key = str(architecture_id)
            response_value = architecture_id
        else:
            report_id = params.get("reportId")
            if report_id is None:
                return jsonify({"error": "reportId不能为空"}), 400
            response_key = "reportId"
            normalized_key = str(report_id)
            response_value = int(normalized_key)

        task = task_service.get_task(business_type, normalized_key)
        if not task:
            if len(params_list) == 1:
                return jsonify({"error": "任务不存在"}), 404
            items.append({response_key: response_value, "exists": False, "message": "任务不存在"})
            continue

        replayed = task_service.replay_callback_if_needed(
            business_type,
            normalized_key,
            callback_url=llm_config.callback_url or "",
            timeout=llm_config.callback_timeout,
        )
        task = task_service.get_task(business_type, normalized_key)
        assert task is not None

        items.append(
            {
                response_key: response_value,
                "status": task["status"],
                "progress": task["progress"],
                "callbackStatus": task["callback_status"],
                "callbackReplayed": replayed,
            }
        )

    if len(items) == 1:
        item = items[0]
        callback_replayed = bool(item.pop("callbackReplayed", False))
        return jsonify({"businessType": business_type, "data": item, "callbackReplayed": callback_replayed})
    return jsonify({"businessType": business_type, "data": items})


@sock.route("/llm/progress")
def llm_progress(ws):
    subscriptions: dict[tuple[str, str], Any] = {}
    try:
        while True:
            raw_message = ws.receive()
            if raw_message is None:
                break

            try:
                payload = json.loads(raw_message)
            except json.JSONDecodeError:
                ws.send(json.dumps({"type": "error", "message": "订阅消息不是合法JSON"}, ensure_ascii=False))
                continue

            try:
                command = _parse_progress_command(payload)
            except ValueError as exc:
                ws.send(json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False))
                continue

            def _send_message(message: Dict[str, Any]) -> None:
                ws.send(json.dumps(message, ensure_ascii=False))

            _handle_progress_command(
                _send_message,
                subscriptions,
                command,
                emit_ack="action" in payload,
            )
    finally:
        for (business_type, business_key), callback in list(subscriptions.items()):
            progress_hub.unsubscribe(business_type, business_key, callback)
