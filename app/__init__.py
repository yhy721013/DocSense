"""Flask app factory.

设计目标：
1) 入口 web_ui.py 仅负责启动。
2) 默认仅注册核心业务路由；调试路由需显式开启。
"""

from __future__ import annotations

import os

from flask import Flask

from app.blueprints.debug import debug_bp
from app.blueprints.llm import llm_bp, sock
from app.services.core.logging import setup_logging
from app.services.core.settings import MAX_CONTENT_LENGTH


def _parse_bool(raw_value: str | None, default: bool = False) -> bool:
    if raw_value is None:
        return default
    value = raw_value.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def create_app() -> Flask:
    setup_logging()
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
        ENABLE_DEBUG_CALLBACK_API=_parse_bool(os.getenv("DOCSENSE_ENABLE_DEBUG_CALLBACK_API")),
    )
    sock.init_app(app)

    app.register_blueprint(llm_bp)
    if app.config["ENABLE_DEBUG_CALLBACK_API"]:
        app.register_blueprint(debug_bp)

    return app
