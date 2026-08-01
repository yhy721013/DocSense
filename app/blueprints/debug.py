from __future__ import annotations

import logging

from flask import Blueprint, jsonify, render_template, request

from app.blueprints.dependencies import get_application_services
from app.presenters.debug import present_callback_preview, present_chat_bootstrap


logger = logging.getLogger(__name__)
debug_bp = Blueprint("debug", __name__)


@debug_bp.get("/debug/api/callback")
def callback_debug_api():
    services = get_application_services()
    result = services.debug_services.callback_preview.execute(
        record=request.args.get("record")
    )
    return jsonify(present_callback_preview(result))


@debug_bp.get("/debug/api/chat/bootstrap")
def chat_debug_bootstrap_api():
    """调用类型化 Debug Query，并呈现冻结的内部调试 JSON。"""
    services = get_application_services()
    result = services.debug_services.chat_bootstrap.execute()
    return jsonify(present_chat_bootstrap(result))


@debug_bp.get("/debug/callback")
def callback_debug_page():
    return render_template("debug/callback.html")


@debug_bp.get("/debug/chat")
def chat_debug_page():
    logger.debug("渲染文件对话调试页面")
    return render_template("debug/chat.html")
