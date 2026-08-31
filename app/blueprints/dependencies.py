"""Flask 蓝图访问应用依赖容器的唯一 Adapter。

框架无关组合根位于 ``app.container``；只有本模块可以读取 ``current_app.extensions``。
这使同一 ``ApplicationServices`` 能被 Flask、离线 Worker 和未来其他 Web 框架复用。
"""

from __future__ import annotations

from flask import current_app

from app.container import APPLICATION_SERVICES_EXTENSION, ApplicationServices


def get_application_services() -> ApplicationServices:
    """读取当前 Flask 应用的依赖容器，缺失或类型错误时明确失败。"""

    services = current_app.extensions.get(APPLICATION_SERVICES_EXTENSION)
    if services is None:
        raise RuntimeError("Flask 应用尚未安装 DocSense 依赖容器")
    if not isinstance(services, ApplicationServices):
        raise RuntimeError("Flask 应用中的 DocSense 依赖容器类型无效")
    return services
