from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request


chat_bp = Blueprint("chat", __name__)


@chat_bp.get("/chat")
def chat_page():
    """对话功能页面（占位）。"""
    return render_template("chat.html")


# =============================
# 对话功能 API（占位实现）
# =============================


@chat_bp.post("/api/chat/upload")
def chat_upload():
    """上传文件/文件夹以建立对话上下文。

    说明：
    - 这里预留接口与返回格式，便于后续对接 AnythingLLM。
    - 当前版本返回明确的占位错误，避免前端静默失败。
    """

    return jsonify(
        {
            "error": "对话功能尚未实现：请在 app/blueprints/chat.py 与相关 service 中补全上传与建 Workspace/Thread 逻辑。"
        }
    ), 501


@chat_bp.post("/api/chat/message")
def chat_message():
    """发送一条对话消息（占位）。"""
    payload = request.get_json(silent=True) or {}
    _ = payload.get("message")
    return jsonify({"error": "对话功能尚未实现"}), 501
