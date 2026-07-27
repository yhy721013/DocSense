from __future__ import annotations

import logging

from flask import Blueprint, jsonify, render_template, request

from app.container import get_application_services
from app.services.utils.callback_preview import load_callback_preview
from app.services.utils.chat_debug_preview import load_chat_debug_bootstrap


logger = logging.getLogger(__name__)
debug_bp = Blueprint("debug", __name__)


@debug_bp.get("/debug/api/callback")
def callback_debug_api():
    return jsonify(load_callback_preview(record=request.args.get("record")))


@debug_bp.get("/debug/api/chat/bootstrap")
def chat_debug_bootstrap_api():
    """使用当前应用容器中的数据库服务构建文件对话调试初始化数据。"""
    logger.info("收到文件对话调试初始化数据请求")
    services = get_application_services()
    payload = load_chat_debug_bootstrap(
        chat_store=services.chat_store,
        kb_service=services.kb_service,
    )
    data = payload.get("data") if isinstance(payload, dict) else {}
    sessions = data.get("sessions", []) if isinstance(data, dict) else []
    current_binding_count = 0
    for item in sessions:
        if not isinstance(item, dict):
            continue
        file_names = item.get("fileNames", [])
        # bootstrap 当前保证 fileNames 为数组；这里仍采用防御性计数，避免调试日志
        # 因未来的异常预览数据反过来影响原本可返回的调试响应。
        if isinstance(file_names, list):
            current_binding_count += len(file_names)
    logger.info(
        "文件对话调试初始化数据响应已生成: "
        "ok=%s session_count=%d current_binding_count=%d "
        "available_file_count=%d",
        bool(payload.get("ok")) if isinstance(payload, dict) else False,
        len(sessions),
        current_binding_count,
        len(data.get("availableFiles", [])) if isinstance(data, dict) else 0,
    )
    return jsonify(payload)


@debug_bp.get("/debug/callback")
def callback_debug_page():
    return render_template("debug/callback.html")


@debug_bp.get("/debug/chat")
def chat_debug_page():
    logger.debug("渲染文件对话调试页面")
    return render_template("debug/chat.html")
