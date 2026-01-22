"""Web UI entrypoint.

本文件保持为“极薄”入口：仅负责创建 Flask App 并启动服务。
业务路由与逻辑被拆分到 app/ 目录，便于多人协作开发与后期扩展。
"""

from __future__ import annotations

import os

from app import create_app


def main() -> None:
    port = int(os.environ.get("WEB_UI_PORT", 5001))
    app = create_app()
    app.run(host="127.0.0.1", port=port, debug=True)


if __name__ == "__main__":
    main()
