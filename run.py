"""Service entrypoint.

本文件保持为"极薄"入口：仅负责创建 Flask App 并启动服务。
当前仓库以甲方协议接口为主，业务路由与逻辑位于 app/ 目录。
"""

from __future__ import annotations

import os
import logging

# 在导入 app 之前加载 .env。
# 已显式传入的环境变量优先级更高，避免本地测试或启动脚本指定的端口被 .env 覆盖。
from dotenv import load_dotenv
load_dotenv()

from app import create_app


logger = logging.getLogger(__name__)


def main() -> None:
    app = create_app()
    host = os.environ.get("APP_HOST").strip()
    port = int(os.environ.get("APP_PORT").strip())
    debug = os.environ.get("APP_DEBUG").strip().lower() in ("true", "1", "yes")

    # 开发与生产模式均使用 Flask 内置服务器
    logger.info(
        "Flask 服务准备启动: host=%s port=%s debug_enabled=%s",
        host,
        port,
        debug,
    )
    # 报告 Dispatcher 当前明确是单实例进程内 Worker。Werkzeug debug reloader 会先在
    # 监控父进程创建应用，再启动子进程，从而同时产生两个 Dispatcher。保留 debug
    # 错误页和日志，但关闭自动重载；代码变更后由开发者显式重启进程。
    app.run(
        host=host,
        port=port,
        debug=debug,
        threaded=True,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
