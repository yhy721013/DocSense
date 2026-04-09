from __future__ import annotations

from flask import Blueprint, jsonify, render_template

from app.services.core.database import ChatDatabaseService, DatabaseService
from app.services.core.settings import CHAT_DB_PATH, KNOWLEDGE_BASE_DB_PATH
from app.services.utils.callback_preview import load_callback_preview
from app.services.utils.chat_debug_preview import load_chat_debug_bootstrap


debug_bp = Blueprint("debug", __name__)
chat_db = ChatDatabaseService(str(CHAT_DB_PATH))
kb_service = DatabaseService(str(KNOWLEDGE_BASE_DB_PATH))


@debug_bp.get("/debug/api/callback")
def callback_debug_api():
    return jsonify(load_callback_preview())


@debug_bp.get("/debug/api/chat/bootstrap")
def chat_debug_bootstrap_api():
    return jsonify(load_chat_debug_bootstrap(chat_db=chat_db, kb_service=kb_service))


@debug_bp.get("/debug/callback")
def callback_debug_page():
    return render_template("debug/callback.html")


@debug_bp.get("/debug/chat")
def chat_debug_page():
    return render_template("debug/chat.html")
