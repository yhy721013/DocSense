"""Analysis 当前进程资源生命周期的活跃权适配器。"""

from __future__ import annotations

import threading

from app.modules.analysis.ports import (
    AnalysisExecutionRef,
    AnalysisResourceActivityPort,
)


class InMemoryAnalysisResourceActivityAdapter(AnalysisResourceActivityPort):
    """SQLite 单实例运行模式下的进程内资源生命周期活跃权。

    该适配器保护同一 DocSense 进程中的业务 Worker 与资源维护线程，覆盖资源记录创建、
    Callback 等待、RAG close 和生命周期审计的完整窗口。它绝不宣称具备跨进程租约语义；
    未来多实例部署必须替换为共享 owner、lease 与 fencing 实现。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active_execution_ids: set[str] = set()

    def acquire(self, execution: AnalysisExecutionRef) -> None:
        self._validate_execution(execution)
        execution_id = execution.task_id.value
        with self._lock:
            if execution_id in self._active_execution_ids:
                raise RuntimeError("同一 execution 不得重复取得资源生命周期活跃权")
            self._active_execution_ids.add(execution_id)

    def release(self, execution: AnalysisExecutionRef) -> None:
        self._validate_execution(execution)
        with self._lock:
            self._active_execution_ids.discard(execution.task_id.value)

    def is_active(self, execution: AnalysisExecutionRef) -> bool:
        self._validate_execution(execution)
        with self._lock:
            return execution.task_id.value in self._active_execution_ids

    @staticmethod
    def _validate_execution(execution: AnalysisExecutionRef) -> None:
        if not isinstance(execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")


__all__ = ("InMemoryAnalysisResourceActivityAdapter",)
