from __future__ import annotations

import json
import logging
import threading
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

from flask import Blueprint, Response, jsonify, request
from flask_sock import Sock

from app.container import ApplicationServices, get_application_services
from app.presenters.chat_stream import (
    close_chat_stream_resource,
    finalize_chat_run_stream,
    mark_chat_run_failed,
)
from app.services.chat import (
    ChatRunBusyError,
    ChatRunStreamRequest,
    record_chat_run_events,
)
from app.services.core.progress import normalize_progress
from app.services.llm_service.analysis_service import (
    run_file_analysis_batch_task,
    run_file_analysis_task,
)
from app.services.llm_service.report_service import run_report_task
from app.services.llm_service.weaponry_service import run_weaponry_task
from app.services.llm_service.chat_service import (
    handle_chat_events,
    delete_chat,
    ChatNotFoundError,
)
from app.services.utils.anythingllm_client import AnythingLLMClient


llm_bp = Blueprint("llm", __name__)
sock = Sock()

logger = logging.getLogger(__name__)


def _services() -> ApplicationServices:
    """读取当前 Flask 应用的依赖容器，禁止路由使用模块级可变服务单例。"""
    return get_application_services()


def _normalize_weaponry_file_path_list(value: Any) -> List[str]:
    """将 weaponry filePathList 中的 URL/裸文件名归一化为哈希文件名。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("filePathList必须为数组")

    normalized: List[str] = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"filePathList中第{index + 1}项不是有效字符串")

        raw_value = item.strip()
        parsed = urlparse(raw_value)
        decoded_path = unquote(parsed.path or raw_value).replace("\\", "/")
        file_name = PurePosixPath(decoded_path).name.strip()
        if not file_name or file_name in {".", ".."}:
            raise ValueError(f"filePathList中第{index + 1}项无法提取文件名")

        key = file_name.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(file_name)

    return normalized


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
        data["progress"] = normalize_progress(task["progress"])
    else:
        data["exists"] = False

    return {"businessType": business_type, "data": data}


def _send_latest_progress(
    send_message,
    business_type: str,
    business_key: str,
    *,
    services: ApplicationServices,
) -> None:
    """从显式应用依赖读取并发送任务最新进度或数据库快照。"""
    latest = services.progress_hub.get_latest(business_type, business_key)
    if latest is not None:
        send_message(latest)
        return

    current_task = services.task_service.get_task(business_type, business_key)
    send_message(_build_progress_snapshot(business_type, business_key, current_task))


def _handle_progress_command(
    send_message,
    subscriptions: dict[tuple[str, str], Any],
    command: Dict[str, Any],
    *,
    emit_ack: bool,
    services: Optional[ApplicationServices] = None,
) -> None:
    """执行进度订阅命令，并允许纯函数测试显式注入应用依赖。"""
    resolved_services = services or _services()
    progress_hub = resolved_services.progress_hub
    task_service = resolved_services.task_service
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

            _send_latest_progress(
                send_message,
                business_type,
                business_key,
                services=resolved_services,
            )

        if emit_ack:
            send_message({"type": "ack", "action": action, "count": len(keys)})
        return

    if action == "query":
        for business_type, business_key in keys:
            _send_latest_progress(
                send_message,
                business_type,
                business_key,
                services=resolved_services,
            )

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
    services = _services()
    task_service = services.task_service
    kb_service = services.kb_service
    progress_hub = services.progress_hub
    llm_config = services.llm_config
    payload = request.get_json(silent=True) or {}
    logger.info("收到文件分析请求: payload_keys=%s", list(payload.keys()))
    if payload.get("businessType") != "file":
        logger.warning(
            "文件分析请求被拒绝: businessType无效 businessType=%s",
            payload.get("businessType"),
        )
        return jsonify({"error": "businessType必须为file"}), 400

    params_list = _get_params(payload)
    if not params_list:
        logger.warning("文件分析请求被拒绝: params为空或格式无效")
        return jsonify({"error": "params不能为空"}), 400

    seen_file_names = set()
    for index, params in enumerate(params_list):
        file_name = params.get("fileName")
        if not isinstance(file_name, str) or not file_name.strip():
            logger.warning("文件分析请求被拒绝: fileName为空 index=%s", index)
            return jsonify({"error": "fileName不能为空"}), 400
        normalized_name = file_name.strip()
        if normalized_name in seen_file_names:
            logger.warning(
                "文件分析请求被拒绝: fileName重复 fileName=%s index=%s",
                normalized_name,
                index,
            )
            return jsonify({"error": "fileName不能重复"}), 400
        seen_file_names.add(normalized_name)

        file_path = params.get("filePath")
        if not isinstance(file_path, str) or not file_path.strip():
            logger.warning(
                "文件分析请求被拒绝: filePath为空 fileName=%s index=%s",
                normalized_name,
                index,
            )
            return jsonify({"error": "filePath不能为空"}), 400

        existing_task = task_service.get_task("file", normalized_name)
        if existing_task and existing_task["status"] in {"0", "1"}:
            logger.warning(
                "文件分析请求被拒绝: 任务正在处理中 fileName=%s status=%s",
                normalized_name,
                existing_task["status"],
            )
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

    _task_fn = run_file_analysis_task if len(tasks) == 1 else run_file_analysis_batch_task
    _task_kwargs = {
        "task_service": task_service,
        "progress_hub": progress_hub,
        "request_payload": payload if len(tasks) > 1 else {"businessType": "file", "params": [params_list[0]]},
        "download_root": llm_config.download_dir,
        "callback_url": llm_config.callback_url or "",
        "callback_timeout": llm_config.callback_timeout,
        # Factory 本身只保存不可变配置和线程安全协调依赖。真正持有网络 Session 的
        # Gateway 在后台任务线程内部按文件创建，批量任务也不会跨文件共享有状态对象。
        "document_rag_factory": services.document_rag_factory,
        "knowledge_index_factory": services.knowledge_index_factory,
    }
    worker = threading.Thread(
        target=services.upload_task_limiter.run,
        args=(_task_fn,),
        kwargs=_task_kwargs,
        daemon=True,
    )
    worker.start()
    logger.info("已启动后台文件分析线程: task_count=%d", len(tasks))
    if len(tasks) == 1:
        return jsonify({"message": "accepted", "businessType": "file", "task": tasks[0]}), 202
    return jsonify({"message": "accepted", "businessType": "file", "tasks": tasks}), 202


@llm_bp.post("/llm/generate-report")
def llm_generate_report():
    services = _services()
    task_service = services.task_service
    progress_hub = services.progress_hub
    llm_config = services.llm_config
    payload = request.get_json(silent=True) or {}
    logger.info("收到报告生成请求: payload_keys=%s", list(payload.keys()))
    if payload.get("businessType") != "report":
        logger.warning(
            "报告生成请求被拒绝: businessType无效 businessType=%s",
            payload.get("businessType"),
        )
        return jsonify({"error": "businessType必须为report"}), 400

    params = _get_first_param(payload)
    if params is None:
        logger.warning("报告生成请求被拒绝: params为空或格式无效")
        return jsonify({"error": "params不能为空"}), 400
    report_id = params.get("reportId")
    if report_id is None:
        logger.warning("报告生成请求被拒绝: reportId为空")
        return jsonify({"error": "reportId不能为空"}), 400
    file_path_list = params.get("filePathList")
    if not isinstance(file_path_list, list) or not file_path_list:
        logger.warning(
            "报告生成请求被拒绝: filePathList无效 reportId=%s file_path_list_type=%s file_count=%s",
            report_id,
            type(file_path_list).__name__,
            len(file_path_list) if isinstance(file_path_list, list) else "n/a",
        )
        return jsonify({"error": "filePathList不能为空"}), 400
    template_outline = params.get("templateOutline")
    if not isinstance(template_outline, str) or not template_outline.strip():
        logger.warning("报告生成请求被拒绝: templateOutline为空 reportId=%s", report_id)
        return jsonify({"error": "templateOutline不能为空"}), 400

    task = task_service.create_report_task(report_id=int(report_id), request_payload=payload)
    progress_hub.publish(
        "report",
        str(report_id),
        {"businessType": "report", "data": {"reportId": int(report_id), "progress": 0.0}},
    )

    _report_kwargs = {
        "task_service": task_service,
        "progress_hub": progress_hub,
        "request_payload": payload,
        "download_root": llm_config.download_dir,
        "callback_url": llm_config.callback_url or "",
        "callback_timeout": llm_config.callback_timeout,
    }
    worker = threading.Thread(
        target=services.upload_task_limiter.run,
        args=(run_report_task,),
        kwargs=_report_kwargs,
        daemon=True,
    )
    worker.start()
    logger.info("已启动后台报告生成线程: report_id=%s", report_id)
    return jsonify({"message": "accepted", "businessType": "report", "task": task}), 202


@llm_bp.post("/llm/weaponry")
def llm_weaponry():
    services = _services()
    task_service = services.task_service
    kb_service = services.kb_service
    progress_hub = services.progress_hub
    llm_config = services.llm_config
    payload = request.get_json(silent=True) or {}
    logger.info("收到武器装备提取请求: payload_keys=%s", list(payload.keys()))
    if payload.get("businessType") != "weaponry":
        logger.warning(
            "武器装备提取请求被拒绝: businessType无效 businessType=%s",
            payload.get("businessType"),
        )
        return jsonify({"error": "businessType必须为weaponry"}), 400

    params = payload.get("params")
    if not isinstance(params, dict):
        logger.warning(
            "武器装备提取请求被拒绝: params无效 params_type=%s",
            type(params).__name__,
        )
        return jsonify({"error": "params不能为空"}), 400

    architecture_id = params.get("architectureId")
    if architecture_id is None:
        logger.warning("武器装备提取请求被拒绝: architectureId为空")
        return jsonify({"error": "architectureId不能为空"}), 400

    try:
        selected_file_names = _normalize_weaponry_file_path_list(params.get("filePathList"))
    except ValueError as exc:
        logger.warning(
            "武器装备提取请求被拒绝: filePathList无效 architectureId=%s error=%s",
            architecture_id,
            exc,
        )
        return jsonify({"error": str(exc)}), 400

    for file_name in selected_file_names:
        document_record = kb_service.get_document_record(
            file_name,
            architecture_id=int(architecture_id),
        )
        if not document_record:
            logger.warning(
                "武器装备提取请求被拒绝: 选中文件尚未解析 architectureId=%s fileName=%s",
                architecture_id,
                file_name,
            )
            return jsonify({"error": f"文件 {file_name} 尚未解析"}), 404
        if str(document_record.get("architecture_id")) != str(architecture_id):
            logger.warning(
                "武器装备提取请求被拒绝: 选中文件不属于当前类别 architectureId=%s fileName=%s actualArchitectureId=%s",
                architecture_id,
                file_name,
                document_record.get("architecture_id"),
            )
            return jsonify({"error": f"文件 {file_name} 不属于当前类别"}), 400

    field_list = params.get("weaponryTemplateFieldList")
    if not isinstance(field_list, list) or not field_list:
        logger.warning(
            "武器装备提取请求被拒绝: weaponryTemplateFieldList无效 architectureId=%s field_list_type=%s field_count=%s",
            architecture_id,
            type(field_list).__name__,
            len(field_list) if isinstance(field_list, list) else "n/a",
        )
        return jsonify({"error": "weaponryTemplateFieldList不能为空"}), 400

    # 校验 analyseData / analyseDataSource 必须为空
    for field_index, field in enumerate(field_list):
        if field.get("analyseData") or field.get("analyseDataSource"):
            logger.warning(
                "武器装备提取请求被拒绝: 字段解析结果未清空 architectureId=%s field_index=%s fieldName=%s",
                architecture_id,
                field_index,
                field.get("fieldName"),
            )
            return jsonify({"error": "analyseData和analyseDataSource必须清空"}), 400
        if field.get("fieldType") == "TABLE":
            for row_index, row in enumerate(field.get("tableFieldList") or []):
                if isinstance(row, list):
                    for cell_index, cell in enumerate(row):
                        if isinstance(cell, dict) and (cell.get("analyseData") or cell.get("analyseDataSource")):
                            logger.warning(
                                "武器装备提取请求被拒绝: 表格单元格解析结果未清空 architectureId=%s field_index=%s fieldName=%s row_index=%s cell_index=%s cellFieldName=%s",
                                architecture_id,
                                field_index,
                                field.get("fieldName"),
                                row_index,
                                cell_index,
                                cell.get("fieldName"),
                            )
                            return jsonify({"error": "analyseData和analyseDataSource必须清空"}), 400

    architecture_id_str = str(architecture_id)
    existing_task = task_service.get_task("weaponry", architecture_id_str)
    if existing_task and existing_task["status"] in {"0", "1"}:
        logger.warning(
            "武器装备提取请求被拒绝: 任务正在处理中 architectureId=%s status=%s",
            architecture_id,
            existing_task["status"],
        )
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
            "selected_file_names": selected_file_names,
            "callback_url": llm_config.callback_url or "",
            "callback_timeout": llm_config.callback_timeout,
        },
        daemon=True,
    )
    worker.start()
    logger.info("已启动后台武器装备提取线程: architectureId=%s", architecture_id)
    return jsonify({"message": "accepted", "businessType": "weaponry", "task": task}), 202


@llm_bp.post("/llm/check-task")
def llm_check_task():
    services = _services()
    task_service = services.task_service
    llm_config = services.llm_config
    payload = request.get_json(silent=True) or {}
    business_type = payload.get("businessType")
    if business_type not in {"file", "report", "weaponry"}:
        logger.warning(
            "任务查询请求被拒绝: businessType无效 businessType=%s",
            business_type,
        )
        return jsonify({"error": "businessType无效"}), 400

    params_list = _get_params(payload)
    if not params_list:
        logger.warning(
            "任务查询请求被拒绝: params为空或格式无效 businessType=%s",
            business_type,
        )
        return jsonify({"error": "params不能为空"}), 400

    items = []
    for index, params in enumerate(params_list):
        if business_type == "file":
            business_key = params.get("fileName")
            if not isinstance(business_key, str) or not business_key.strip():
                logger.warning(
                    "任务查询请求被拒绝: fileName为空 index=%s",
                    index,
                )
                return jsonify({"error": "fileName不能为空"}), 400
            response_key = "fileName"
            normalized_key = business_key.strip()
            response_value: Any = normalized_key
        elif business_type == "weaponry":
            architecture_id = params.get("architectureId")
            if architecture_id is None:
                logger.warning(
                    "任务查询请求被拒绝: architectureId为空 index=%s",
                    index,
                )
                return jsonify({"error": "architectureId不能为空"}), 400
            response_key = "architectureId"
            normalized_key = str(architecture_id)
            response_value = architecture_id
        else:
            report_id = params.get("reportId")
            if report_id is None:
                logger.warning(
                    "任务查询请求被拒绝: reportId为空 index=%s",
                    index,
                )
                return jsonify({"error": "reportId不能为空"}), 400
            response_key = "reportId"
            normalized_key = str(report_id)
            response_value = int(normalized_key)

        task = task_service.get_task(business_type, normalized_key)
        if not task:
            if len(params_list) == 1:
                logger.warning(
                    "任务查询请求未找到任务: businessType=%s businessKey=%s",
                    business_type,
                    normalized_key,
                )
                return jsonify({"error": "任务不存在"}), 404
            logger.warning(
                "批量任务查询项未找到任务: businessType=%s businessKey=%s index=%s",
                business_type,
                normalized_key,
                index,
            )
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


@llm_bp.post("/llm/reassign")
def llm_reassign():
    services = _services()
    kb_service = services.kb_service
    anythingllm_config = services.anythingllm_config
    payload = request.get_json(silent=True) or {}
    logger.info("收到文档分类变更请求: payload_keys=%s", list(payload.keys()))

    if payload.get("businessType") != "reassign":
        logger.warning(
            "文档分类变更请求被拒绝: businessType无效 businessType=%s",
            payload.get("businessType"),
        )
        return jsonify({"error": "businessType必须为reassign"}), 400

    params = payload.get("params")
    if not isinstance(params, dict):
        logger.warning(
            "文档分类变更请求被拒绝: params无效 params_type=%s",
            type(params).__name__,
        )
        return jsonify({"error": "params不能为空"}), 400

    file_name = params.get("fileName")
    if not isinstance(file_name, str) or not file_name.strip():
        logger.warning("文档分类变更请求被拒绝: fileName为空")
        return jsonify({"error": "fileName不能为空"}), 400
    file_name = file_name.strip()

    old_architecture_id = params.get("oldArchitectureId")
    if old_architecture_id is None:
        logger.warning(
            "文档分类变更请求被拒绝: oldArchitectureId为空 fileName=%s",
            file_name,
        )
        return jsonify({"error": "oldArchitectureId不能为空"}), 400

    new_architecture_id = params.get("newArchitectureId")
    if new_architecture_id is None:
        logger.warning(
            "文档分类变更请求被拒绝: newArchitectureId为空 fileName=%s oldArchitectureId=%s",
            file_name,
            old_architecture_id,
        )
        return jsonify({"error": "newArchitectureId不能为空"}), 400

    if old_architecture_id == new_architecture_id:
        logger.warning(
            "文档分类变更请求被拒绝: 新旧分类相同 fileName=%s architectureId=%s",
            file_name,
            old_architecture_id,
        )
        return jsonify({"error": "oldArchitectureId与newArchitectureId不能相同"}), 400

    doc_record = kb_service.get_document_record(
        file_name,
        architecture_id=int(old_architecture_id),
    )
    if not doc_record:
        logger.warning(
            "文档分类变更失败: 文档记录不存在 fileName=%s oldArchitectureId=%s newArchitectureId=%s",
            file_name,
            old_architecture_id,
            new_architecture_id,
        )
        return jsonify({
            "businessType": "reassign",
            "msg": "变更失败",
            "data": {
                "fileName": file_name,
                "oldArchitectureId": old_architecture_id,
                "newArchitectureId": new_architecture_id,
                "success": False,
                "message": "文档记录不存在"
            }
        }), 500

    actual_old_id = doc_record["architecture_id"]
    if str(actual_old_id) != str(old_architecture_id):
        logger.warning(
            "变更分类请求与现有记录不一致: 记录中 architecture_id=%s, 请求 oldArchitectureId=%s",
            actual_old_id, old_architecture_id
        )
        return jsonify({
            "businessType": "reassign",
            "msg": "变更失败",
            "data": {
                "fileName": file_name,
                "oldArchitectureId": old_architecture_id,
                "newArchitectureId": new_architecture_id,
                "success": False,
                "message": "分类不一致，变更失败"
            }
        }), 500

    client = AnythingLLMClient(anythingllm_config)
    doc_path = doc_record.get("doc_path")

    try:
        if doc_path:
            old_workspace_slug = kb_service.get_workspace_slug(int(actual_old_id))
            if old_workspace_slug:
                client.update_embeddings_batch(old_workspace_slug, deletes=[doc_path], user_id=1)

            new_workspace_slug = kb_service.get_workspace_slug(new_architecture_id)
            if not new_workspace_slug:
                workspace_name = f"architectureId-{new_architecture_id}"
                ws_info = client.create_rag_workspace(workspace_name, user_id=1)
                if ws_info and ws_info.get("slug"):
                    new_workspace_slug = ws_info["slug"]
                    kb_service.add_workspace(new_architecture_id, new_workspace_slug)

            if new_workspace_slug:
                metadata = {"file_name": file_name, "architecture_id": new_architecture_id}
                client.update_embeddings(doc_path, new_workspace_slug, user_id=1, metadata=metadata)
    except Exception as e:
        logger.error("在调整工作区关联时失败: file_name=%s, exception=%s", file_name, e)
        return jsonify({
            "businessType": "reassign",
            "msg": "变更失败",
            "data": {
                "fileName": file_name,
                "oldArchitectureId": old_architecture_id,
                "newArchitectureId": new_architecture_id,
                "success": False,
                "message": f"处理知识库节点映射报错: {str(e)}"
            }
        }), 500

    kb_service.update_document_architecture(
        file_name,
        new_architecture_id,
        current_architecture_id=int(old_architecture_id),
    )

    return jsonify({
        "businessType": "reassign",
        "msg": "变更成功",
        "data": {
            "fileName": file_name,
            "oldArchitectureId": old_architecture_id,
            "newArchitectureId": new_architecture_id,
            "success": True,
            "message": "变更成功"
        }
    }), 200


@sock.route("/llm/progress")
def llm_progress(ws):
    services = _services()
    progress_hub = services.progress_hub
    subscriptions: dict[tuple[str, str], Any] = {}
    try:
        while True:
            raw_message = ws.receive()
            if raw_message is None:
                break

            try:
                payload = json.loads(raw_message)
            except json.JSONDecodeError:
                logger.warning("进度订阅消息被拒绝: 非法JSON")
                ws.send(json.dumps({"type": "error", "message": "订阅消息不是合法JSON"}, ensure_ascii=False))
                continue

            try:
                command = _parse_progress_command(payload)
            except ValueError as exc:
                logger.warning(
                    "进度订阅消息被拒绝: %s payload_keys=%s",
                    exc,
                    list(payload.keys()) if isinstance(payload, dict) else "n/a",
                )
                ws.send(json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False))
                continue

            def _send_message(message: Dict[str, Any]) -> None:
                ws.send(json.dumps(message, ensure_ascii=False))

            _handle_progress_command(
                _send_message,
                subscriptions,
                command,
                emit_ack="action" in payload,
                services=services,
            )
    finally:
        for (business_type, business_key), callback in list(subscriptions.items()):
            progress_hub.unsubscribe(business_type, business_key, callback)


# ══════════════════════════════════════════════════════════════
#  文件对话接口
# ══════════════════════════════════════════════════════════════

@llm_bp.post("/llm/chat")
def llm_chat():
    services = _services()
    kb_service = services.kb_service
    chat_db = services.chat_db
    anythingllm_config = services.anythingllm_config
    payload = request.get_json(silent=True) or {}
    logger.info("收到文件对话请求: payload_keys=%s", list(payload.keys()))

    if payload.get("businessType") != "chat":
        logger.warning(
            "文件对话请求被拒绝: businessType无效 businessType=%s",
            payload.get("businessType"),
        )
        return jsonify({"error": "businessType必须为chat"}), 400

    params = payload.get("params")
    if not isinstance(params, dict):
        logger.warning(
            "文件对话请求被拒绝: params无效 params_type=%s",
            type(params).__name__,
        )
        return jsonify({"error": "params不能为空"}), 400

    chat_id = params.get("chatId")
    if not isinstance(chat_id, str) or not chat_id.strip():
        logger.warning("文件对话请求被拒绝: chatId为空")
        return jsonify({"error": "chatId不能为空"}), 400
    chat_id = chat_id.strip()

    file_names = params.get("fileNames")
    if not isinstance(file_names, list):
        logger.warning(
            "文件对话请求被拒绝: fileNames类型无效 chatId=%s file_names_type=%s",
            chat_id,
            type(file_names).__name__,
        )
        return jsonify({"error": "fileNames必须为数组"}), 400

    message = params.get("message")
    if not isinstance(message, str) or not message.strip():
        logger.warning("文件对话请求被拒绝: message为空 chatId=%s", chat_id)
        return jsonify({"error": "message不能为空"}), 400
    message = message.strip()
    logger.info(
        "文件对话请求参数校验通过: chatId=%s raw_file_count=%d message_length=%d",
        chat_id,
        len(file_names),
        len(message),
    )

    normalized_file_names: list[str] = []
    seen_file_names: set[str] = set()

    # 对话入参中的 fileNames 是业务侧哈希文件名。这里必须在进入 AnythingLLM 前
    # 完成“存在性校验 + 首次出现顺序去重”，否则本地历史快照与远端检索上下文会不一致。
    for index, fn in enumerate(file_names):
        if not isinstance(fn, str) or not fn.strip():
            logger.warning(
                "文件对话请求被拒绝: fileNames中包含无效文件名 chatId=%s index=%s",
                chat_id,
                index,
            )
            return jsonify({"error": "fileNames中包含无效文件名"}), 400
        normalized_file_name = fn.strip()
        if normalized_file_name in seen_file_names:
            logger.debug(
                "文件对话请求去重重复文件名: chatId=%s fileName=%s index=%s",
                chat_id,
                normalized_file_name,
                index,
            )
            continue
        seen_file_names.add(normalized_file_name)
        doc_record = kb_service.get_document_record(normalized_file_name)
        if not doc_record:
            logger.warning(
                "文件对话请求被拒绝: 文件尚未解析 chatId=%s fileName=%s index=%s",
                chat_id,
                normalized_file_name,
                index,
            )
            return jsonify({"error": f"文件 {fn} 尚未解析，无法用于对话"}), 404
        normalized_file_names.append(normalized_file_name)

    # 将哈希文件名映射为原始文件名
    file_original_names = [kb_service.get_original_name(fn) for fn in normalized_file_names]
    logger.info(
        "文件对话引用文件校验完成: chatId=%s normalized_file_count=%d",
        chat_id,
        len(normalized_file_names),
    )

    chat_commands = services.chat_commands
    try:
        chat_run = chat_commands.start_chat_run(chat_id=chat_id)
    except ChatRunBusyError:
        logger.warning(
            "文件对话请求被拒绝: chatId已有进行中的流式响应 chatId=%s",
            chat_id,
        )
        return jsonify({"error": "当前对话已有进行中的流式响应"}), 409
    logger.info(
        "文件对话run已分配，准备创建流式响应: chatId=%s runId=%s file_count=%d",
        chat_id,
        chat_run.run_id,
        len(normalized_file_names),
    )

    client: AnythingLLMClient | None = None
    try:
        chat_run_request = ChatRunStreamRequest(
            run_id=chat_run.run_id,
            chat_id=chat_id,
            file_names=tuple(normalized_file_names),
            file_original_names=tuple(file_original_names),
            message=message,
        )
        client = AnythingLLMClient(anythingllm_config)
        logger.info(
            "文件对话AnythingLLM客户端已创建: chatId=%s runId=%s",
            chat_id,
            chat_run.run_id,
        )
        stream = handle_chat_events(
            chat_db=chat_db,
            kb_service=kb_service,
            client=client,
            chat_id=chat_run_request.chat_id,
            file_names=list(chat_run_request.file_names),
            file_original_names=list(chat_run_request.file_original_names),
            message=chat_run_request.message,
        )
        stream = record_chat_run_events(
            request=chat_run_request,
            events=stream,
            store=services.chat_store,
            chat_commands=chat_commands,
        )
        generator = finalize_chat_run_stream(
            stream=stream,
            run_id=chat_run.run_id,
            on_close=client.close,
        )
        logger.info(
            "文件对话SSE响应已创建: chatId=%s runId=%s",
            chat_id,
            chat_run.run_id,
        )
        return Response(
            generator,
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception as exc:
        logger.exception(
            "文件对话请求在SSE响应创建前失败: chatId=%s runId=%s",
            chat_id,
            chat_run.run_id,
        )
        if client is not None:
            close_chat_stream_resource(
                client,
                run_id=chat_run.run_id,
                label="client",
            )
        mark_chat_run_failed(
            chat_commands=chat_commands,
            run_id=chat_run.run_id,
            error_message=str(exc) or exc.__class__.__name__,
        )
        raise


@llm_bp.get("/llm/chat/history")
def llm_chat_history():
    services = _services()
    chat_id = request.args.get("chatId", "").strip()
    if not chat_id:
        logger.warning("对话历史请求被拒绝: chatId为空")
        return jsonify({"error": "chatId不能为空"}), 400

    history = services.chat_history.list_history(chat_id)
    logger.info("返回文件对话历史: chatId=%s message_count=%d", chat_id, len(history))
    return jsonify(history)


@llm_bp.post("/llm/chat/abort")
def llm_chat_abort():
    services = _services()
    payload = request.get_json(silent=True) or {}
    logger.info("收到中断对话请求: payload_keys=%s", list(payload.keys()))

    if payload.get("businessType") != "chat":
        logger.warning(
            "中断对话请求被拒绝: businessType无效 businessType=%s",
            payload.get("businessType"),
        )
        return jsonify({"error": "businessType必须为chat"}), 400

    params = payload.get("params")
    if not isinstance(params, dict):
        logger.warning(
            "中断对话请求被拒绝: params无效 params_type=%s",
            type(params).__name__,
        )
        return jsonify({"error": "params不能为空"}), 400

    chat_id = params.get("chatId")
    if not isinstance(chat_id, str) or not chat_id.strip():
        logger.warning("中断对话请求被拒绝: chatId为空")
        return jsonify({"error": "chatId不能为空"}), 400

    result = services.chat_abort.abort_chat(chat_id=chat_id)
    logger.info(
        "返回文件对话中断结果: chatId=%s aborted=%s",
        result.chat_id,
        result.aborted,
    )
    return jsonify(result.to_response())


@llm_bp.post("/llm/chat/delete")
def llm_chat_delete():
    services = _services()
    chat_db = services.chat_db
    anythingllm_config = services.anythingllm_config
    payload = request.get_json(silent=True) or {}
    logger.info("收到删除对话请求: payload_keys=%s", list(payload.keys()))

    if payload.get("businessType") != "chat":
        logger.warning(
            "删除对话请求被拒绝: businessType无效 businessType=%s",
            payload.get("businessType"),
        )
        return jsonify({"error": "businessType必须为chat"}), 400

    params = payload.get("params")
    if not isinstance(params, dict):
        logger.warning(
            "删除对话请求被拒绝: params无效 params_type=%s",
            type(params).__name__,
        )
        return jsonify({"error": "params不能为空"}), 400

    chat_id = params.get("chatId")
    if not isinstance(chat_id, str) or not chat_id.strip():
        logger.warning("删除对话请求被拒绝: chatId为空")
        return jsonify({"error": "chatId不能为空"}), 400
    chat_id = chat_id.strip()

    client = AnythingLLMClient(anythingllm_config)
    try:
        delete_chat(chat_db=chat_db, client=client, chat_id=chat_id)
        return jsonify({"chatId": chat_id, "deleted": True, "msg": "对话已删除"})
    except ChatNotFoundError:
        logger.warning("删除对话请求未找到对话: chatId=%s", chat_id)
        return jsonify({"error": "对话不存在"}), 404
