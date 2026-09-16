
from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from typing import Iterator


logger = logging.getLogger(__name__)


@contextmanager
def workspace_tempdir() -> Iterator[str]:
    """创建与业务运行目录隔离、用例间独占的临时目录。

    不把测试数据库放进项目的 ``.runtime``：该目录可能由真实进程创建、带有
    不同 ACL，也可能包含开发数据。操作系统临时目录由当前测试进程独占，并由
    标准库在退出时清理；这样既避免污染业务状态，也兼容 Windows/Linux。
    """
    with tempfile.TemporaryDirectory(
        prefix="docsense-test-",
        ignore_cleanup_errors=True,
    ) as temporary_path:
        logger.debug("已创建测试临时目录: path=%s", temporary_path)
        yield temporary_path
        logger.debug("测试临时目录即将清理: path=%s", temporary_path)
