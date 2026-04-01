"""Flask app factory.

设计目标：
1) 入口 web_ui.py 仅负责启动。
2) 默认注册核心业务路由与本地调试路由。
"""

from __future__ import annotations

from flask import Flask

from app.blueprints.debug import debug_bp
from app.blueprints.llm import llm_bp, sock
from app.services.core.logging import setup_logging
from app.services.core.settings import MAX_CONTENT_LENGTH


def create_app() -> Flask:
    setup_logging()
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
    )
    sock.init_app(app)

    app.register_blueprint(llm_bp)
    app.register_blueprint(debug_bp)

    return app
