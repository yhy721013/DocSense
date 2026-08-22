"""从 Task Control v2 与完整结果快照重建 Analysis 同步 Callback 候选。"""

from __future__ import annotations

import logging

from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.analysis.ports import (
    AnalysisCallbackRecoveryCandidate,
    AnalysisResultSnapshotStorePort,
)
from app.modules.tasks.domain import TaskBusinessRef
from app.modules.tasks.ports import TaskReadPort


logger = logging.getLogger(__name__)
_RECOVERABLE_CALLBACK_STATUSES = frozenset({"pending", "failed", "outcome_unknown"})
_TERMINAL_PUBLIC_STATUSES = frozenset({"2", "3"})


class SQLiteAnalysisV2CallbackRecoverySource:
    """只读 latest v2 Task；绝不重跑 Analysis 或从摘要猜测公开 payload。"""

    def __init__(
        self,
        *,
        task_reader: TaskReadPort,
        results: AnalysisResultSnapshotStorePort,
    ) -> None:
        if not isinstance(task_reader, TaskReadPort):
            raise TypeError("task_reader 必须实现 TaskReadPort")
        if not isinstance(results, AnalysisResultSnapshotStorePort):
            raise TypeError("results 必须实现 AnalysisResultSnapshotStorePort")
        self._tasks = task_reader
        self._results = results

    def load_recoverable(self, file_name: str) -> AnalysisCallbackRecoveryCandidate | None:
        if not isinstance(file_name, str) or not file_name.strip():
            raise ValueError("file_name 必须是非空 str")
        normalized = file_name.strip()
        business_ref = TaskBusinessRef("file", normalized)
        task = self._tasks.get_latest(business_ref)
        if task is None:
            return None
        if task.public_status not in _TERMINAL_PUBLIC_STATUSES:
            return None
        if task.callback_status not in _RECOVERABLE_CALLBACK_STATUSES:
            return None
        result = self._results.get(task.task_id)
        if result is None:
            raise RuntimeError("终态 Analysis Task 缺少完整结果快照")
        if result.business_ref != business_ref:
            raise RuntimeError("Analysis 结果快照与 latest 业务身份不一致")
        self._validate_payload(result.payload, file_name=normalized, status=task.public_status)
        logger.debug(
            "已从 v2 控制面加载 Analysis Callback 恢复候选: "
            "task_id=%s file_name=%s callback_status=%s",
            task.task_id,
            normalized,
            task.callback_status,
        )
        return AnalysisCallbackRecoveryCandidate(
            execution=self._execution_from_result(task, result),
            payload=result.payload,
            callback_attempts=task.callback_attempts,
        )

    @staticmethod
    def _execution_from_result(task, result):  # type: ignore[no-untyped-def]
        from app.modules.analysis.ports import AnalysisExecutionRef

        # 批次身份由结果 Store 与根 execution 同表连接后返回；恢复用例不解码 Input，
        # 更不会让旧 Runner 或当前环境补造 Authority/批次字段。
        return AnalysisExecutionRef(
            task.task_id,
            task.business_ref.business_key,
            result.batch_id,
            result.batch_sequence,
        )

    @staticmethod
    def _validate_payload(payload: FrozenJsonObject, *, file_name: str, status: str) -> None:
        if not isinstance(payload, FrozenJsonObject):
            raise TypeError("Analysis 结果快照 payload 类型错误")
        if set(key for key, _ in payload.items) != {"businessType", "data", "msg"}:
            raise RuntimeError("Analysis Callback 快照顶层字段不合法")
        if payload.get("businessType") != "file":
            raise RuntimeError("Analysis Callback 快照 businessType 不一致")
        data = payload.get("data")
        if not isinstance(data, FrozenJsonObject):
            raise RuntimeError("Analysis Callback 快照 data 必须是对象")
        if data.get("fileName") != file_name or data.get("status") != status:
            raise RuntimeError("Analysis Callback 快照与 latest 公开终态不一致")


__all__ = ["SQLiteAnalysisV2CallbackRecoverySource"]
