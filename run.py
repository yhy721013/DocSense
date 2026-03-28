"""Service entrypoint.

本文件保持为"极薄"入口：仅负责创建 Flask App 并启动服务。
当前仓库以甲方协议接口为主，业务路由与逻辑位于 app/ 目录。
"""

from __future__ import annotations

import os

# 在导入 app 之前加载 .env
from dotenv import load_dotenv
load_dotenv(override=True)

from app import create_app


def main() -> None:
    app = create_app()
    host = os.environ.get("APP_HOST").strip()
    port = int(os.environ.get("APP_PORT").strip())
    debug = os.environ.get("APP_DEBUG").strip().lower() in ("true", "1", "yes")

    # 【新增】根据环境变量选择是否使用 waitress 生产模式
    use_waitress = os.environ.get("USE_WAITRESS", "true").lower() in ("true", "1", "yes")

    if use_waitress:
        # 生产模式：使用 waitress（Windows 兼容，支持长时间任务）
        try:
            from waitress import serve
            print(f"Starting production server with Waitress on {host}:{port}")
            serve(app, host=host, port=port, threads=8, connection_limit=20)
        except ImportError:
            print("Warning: waitress not installed, falling back to Flask development server")
            app.run(host=host, port=port, debug=debug, threaded=True)
    else:
        # 开发模式：使用 Flask 内置服务器
        print(f"Starting development server on {host}:{port} (debug={debug})")
        app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    main()
