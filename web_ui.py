"""Web UI entrypoint.

本文件保持为“极薄”入口：仅负责创建 Flask App 并启动服务。
业务路由与逻辑被拆分到 app/ 目录，便于多人协作开发与后期扩展。
"""

from __future__ import annotations

import os

# 在导入 app 之前加载 .env
from dotenv import load_dotenv
load_dotenv()

from app import create_app


def main() -> None:
    app = create_app()
    host = os.environ.get("WEB_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_UI_PORT", 5001))
    debug = os.environ.get("WEB_UI_DEBUG", "true").lower() in ("true", "1", "yes")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
