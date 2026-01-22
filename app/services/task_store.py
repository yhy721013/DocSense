from __future__ import annotations

import threading
from typing import Any, Dict, Optional


class InMemoryTaskStore:
    """线程安全的任务状态存储（内存态）。

    说明：
    - 与旧版 web_ui.py 的 processing_status 行为一致，但封装为类，便于替换为 Redis/DB。
    - 仅保存“当前进程内”状态；生产环境如需持久化可实现同接口的存储。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._status: Dict[str, Dict[str, Any]] = {}

    def set(self, task_id: str, value: Dict[str, Any]) -> None:
        with self._lock:
            self._status[task_id] = dict(value)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._status.get(task_id)
            return dict(item) if isinstance(item, dict) else None

    def update(self, task_id: str, **kwargs: Any) -> None:
        with self._lock:
            if task_id not in self._status or not isinstance(self._status.get(task_id), dict):
                self._status[task_id] = {}
            self._status[task_id].update(kwargs)
