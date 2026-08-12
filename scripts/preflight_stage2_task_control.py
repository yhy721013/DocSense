#!/usr/bin/env python3
"""阶段 2 旧 Task SQLite 只读预检命令行入口。

预检算法及其生产运行时契约位于 Tasks SQLite Adapter。本文件仅保留稳定的运维命令入口，
避免 Bootstrap 与离线脚本各自维护一套状态域和阻塞规则。
"""

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.tasks.adapters.sqlite.legacy_preflight import (
    PreflightInputError,
    _file_set_snapshot,
    inspect_old_database,
    main,
)


__all__ = ["PreflightInputError", "inspect_old_database", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
