"""DocSense Flask 应用工厂。

应用工厂负责安装路由、WebSocket 扩展和应用级依赖容器。外部系统的网络会话不会在应用
启动阶段创建；AnythingLLM Transport 由容器中的任务级 Factory 延迟到后台线程内创建。
"""

from __future__ import annotations

import atexit

from flask import Flask

from app.blueprints.debug import debug_bp
from app.blueprints.llm import llm_bp, sock
from app.container import (
    APPLICATION_SERVICES_EXTENSION,
    ApplicationServices,
    create_application_services,
)
from app.services.core.logging import setup_logging
from app.services.core.settings import MAX_CONTENT_LENGTH


def create_app(*, services: ApplicationServices | None = None) -> Flask:
    """创建 Flask 应用，并允许测试注入完全离线的依赖容器。

    参数:
        services: 可选的应用级依赖。省略时构建生产容器；显式传入时不会构建生产服务
            或网络对象，便于路由测试证明应用初始化不触发外部 HTTP。
    """
    setup_logging()
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
    )
    owns_services = services is None
    resolved_services = services if services is not None else create_application_services()
    app.extensions[APPLICATION_SERVICES_EXTENSION] = resolved_services
    sock.init_app(app)

    app.register_blueprint(llm_bp)
    app.register_blueprint(debug_bp)

    if owns_services:
        # 显式注入的离线测试默认不启动任何后台线程；只有应用工厂自行创建的生产容器
        # 才拥有 Dispatcher 生命周期。启动失败必须让应用创建失败，不能在没有恢复扫描
        # 的情况下继续对外返回 202。
        resolved_services.start_background_services()
        atexit.register(resolved_services.close)

    return app
