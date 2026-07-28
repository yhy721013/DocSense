"""DocSense Flask 应用工厂。

应用工厂负责安装路由、WebSocket 扩展和应用级依赖容器。外部系统的网络会话不会在应用
启动阶段创建；AnythingLLM Transport 由容器中的任务级 Factory 延迟到后台线程内创建。
"""

from __future__ import annotations

import atexit
import logging

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


logger = logging.getLogger(__name__)


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
        try:
            resolved_services.start_background_services()
        except BaseException as startup_error:
            # 生产容器由应用工厂独占。即使各 Dispatcher 已在 start() 内完成局部
            # 回滚，这里仍统一 close，确保失败组件刚取得的进程锁、维护线程和其他
            # 容器资源都走完最终释放路径。关闭异常只记录，不能遮蔽真正的启动错误。
            logger.error(
                "DocSense 后台服务启动失败，开始关闭应用拥有的依赖容器: "
                "error_type=%s",
                type(startup_error).__name__,
            )
            try:
                resolved_services.close()
            except BaseException:
                logger.critical(
                    "DocSense 启动失败后的容器关闭异常，必须检查进程锁与后台资源",
                    exc_info=True,
                )
            raise
        atexit.register(resolved_services.close)

    return app
