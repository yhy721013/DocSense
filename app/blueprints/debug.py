from __future__ import annotations

from flask import Blueprint, jsonify

from app.services.utils.callback_preview import load_callback_preview


debug_bp = Blueprint("debug", __name__)


@debug_bp.get("/debug/api/callback")
def callback_debug_api():
    return jsonify(load_callback_preview())
