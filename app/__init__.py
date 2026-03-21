"""Flask app factory.

设计目标：
1) 入口 web_ui.py 仅负责启动。
2) 仅注册甲方协议 /llm/* 相关路由。
"""

from __future__ import annotations

from flask import Flask

from app.blueprints.llm import llm_bp, sock
from app.core.logging import setup_logging
from app.core.settings import MAX_CONTENT_LENGTH


def create_app() -> Flask:
    setup_logging()
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
    )
    sock.init_app(app)

    # 仅保留甲方协议接口
    app.register_blueprint(llm_bp)

    return app
