"""通用本地后台组件的跨进程单实例文件锁。"""

from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
import threading
from typing import BinaryIO


logger = logging.getLogger(__name__)


class FileProcessSingletonGuard:
    """使用操作系统文件锁保护一个本地后台组件的进程所有权。

    锁文件只是稳定的协调地址，不保存任务事实，也不会在释放时删除。真正的互斥由
    Windows ``msvcrt`` 或 POSIX ``flock`` 持有；进程异常退出后，内核会自动释放锁，
    因而遗留锁文件不会造成永久假死。

    ``component_name`` 和 ``event_logger`` 仅用于输出业务可读日志。通用实现不导入
    report、weaponry 等业务模块，业务薄适配器可以注入自己的日志命名空间。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        component_name: str = "本地后台组件",
        event_logger: logging.Logger | None = None,
    ) -> None:
        normalized_name = str(component_name or "").strip()
        if not normalized_name:
            raise ValueError("component_name 不能为空")
        if event_logger is not None and not isinstance(event_logger, logging.Logger):
            raise TypeError("event_logger 必须是 logging.Logger 或 None")
        self._path = Path(path).expanduser().resolve()
        self._component_name = normalized_name
        self._logger = event_logger or logger
        self._state_lock = threading.RLock()
        self._handle: BinaryIO | None = None
        self._owner_pid: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> bool:
        """非阻塞获取所有权；同一对象重复获取幂等，被占用时返回 ``False``。"""

        with self._state_lock:
            current_pid = os.getpid()
            if self._handle is not None:
                if self._owner_pid != current_pid:
                    raise RuntimeError("单实例文件锁对象不得跨 fork 继续使用")
                return True
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._path.open("a+b")
            try:
                self._ensure_lock_byte(handle)
                self._lock_nonblocking(handle)
            except OSError as exc:
                handle.close()
                if exc.errno in {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EDEADLK,
                    errno.EPERM,
                }:
                    self._logger.error(
                        "%s 单实例锁已被占用: path=%s pid=%d",
                        self._component_name,
                        self._path,
                        current_pid,
                    )
                    return False
                raise
            self._handle = handle
            self._owner_pid = current_pid
            self._logger.info(
                "已取得 %s 单实例进程锁: path=%s pid=%d",
                self._component_name,
                self._path,
                current_pid,
            )
            return True

    def release(self) -> None:
        """幂等释放内核锁并关闭文件描述符。"""

        with self._state_lock:
            handle = self._handle
            owner_pid = self._owner_pid
            self._handle = None
            self._owner_pid = None
        if handle is None:
            return
        try:
            if owner_pid == os.getpid():
                self._unlock(handle)
            else:
                self._logger.critical(
                    "检测到%s跨 fork 的单实例锁释放请求，仅关闭子进程句柄: "
                    "path=%s owner_pid=%s current_pid=%s",
                    self._component_name,
                    self._path,
                    owner_pid,
                    os.getpid(),
                )
        finally:
            handle.close()
        self._logger.info(
            "已释放 %s 单实例进程锁: path=%s pid=%d",
            self._component_name,
            self._path,
            os.getpid(),
        )

    @staticmethod
    def _ensure_lock_byte(handle: BinaryIO) -> None:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)

    @staticmethod
    def _lock_nonblocking(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = ["FileProcessSingletonGuard"]
