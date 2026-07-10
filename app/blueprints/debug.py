from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from app.container import get_application_services
from app.services.utils.callback_preview import load_callback_preview
from app.services.utils.chat_debug_preview import load_chat_debug_bootstrap


debug_bp = Blueprint("debug", __name__)


@debug_bp.get("/debug/api/callback")
def callback_debug_api():
    return jsonify(load_callback_preview(record=request.args.get("record")))


@debug_bp.get("/debug/api/chat/bootstrap")
def chat_debug_bootstrap_api():
    """使用当前应用容器中的数据库服务构建文件对话调试初始化数据。"""
    services = get_application_services()
    return jsonify(
        load_chat_debug_bootstrap(
            chat_store=services.chat_store,
            kb_service=services.kb_service,
        )
    )


@debug_bp.get("/debug/callback")
def callback_debug_page():
    return render_template("debug/callback.html")


@debug_bp.get("/debug/chat")
def chat_debug_page():
    return render_template("debug/chat.html")
