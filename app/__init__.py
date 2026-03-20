"""Flask app factory.

设计目标：
1) 入口 web_ui.py 仅负责启动。
2) 分类/抽取 与 对话 功能通过 blueprint 解耦，降低协作冲突。
3) API 路由使用 /api/<feature>/... 命名空间，减少未来扩展冲突。
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask

from app.blueprints.chat import chat_bp
from app.blueprints.classify import classify_bp
from app.blueprints.llm import llm_bp, sock
from app.blueprints.main import main_bp
from app.logging_config import setup_logging
from app.settings import MAX_CONTENT_LENGTH


def create_app() -> Flask:
    setup_logging()
    project_root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        template_folder=str(project_root / "templates"),
        static_folder=str(project_root / "static"),
    )
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
    )
    sock.init_app(app)

    # 页面入口
    app.register_blueprint(main_bp)

    # 功能模块（API + 页面）
    app.register_blueprint(classify_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(llm_bp)

    return app
