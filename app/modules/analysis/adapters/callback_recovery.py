"""从最新 file 投影读取 Analysis 同步回调恢复候选。"""

from __future__ import annotations

from collections.abc import Mapping
import logging

from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.analysis.ports import (
    AnalysisCallbackRecoveryCandidate,
    AnalysisCallbackRecoverySourcePort,
    AnalysisExecutionRef,
)
from app.modules.tasks.domain import TaskId
from app.services.llm_service.task_service import LLMTaskService


logger = logging.getLogger(__name__)

_ANALYSIS_BUSINESS_TYPE = "file"
_TERMINAL_PUBLIC_STATUSES = frozenset({"2", "3"})
_RECOVERABLE_CALLBACK_STATUSES = frozenset(
    {"pending", "failed", "outcome_unknown"}
)


class SQLiteAnalysisCallbackRecoverySource(AnalysisCallbackRecoverySourcePort):
    """只读取 latest 公开投影，绝不重新执行模型、RAG 或永久知识写入。"""

    def __init__(self, task_service: LLMTaskService) -> None:
        if not isinstance(task_service, LLMTaskService):
            raise TypeError("task_service 必须是 LLMTaskService")
        self._task_service = task_service

    def load_recoverable(
        self,
        file_name: str,
    ) -> AnalysisCallbackRecoveryCandidate | None:
        """读取仍为 latest 且明确允许 check-task 补发的一次执行。"""

        if not isinstance(file_name, str) or not file_name.strip():
            raise ValueError("file_name 必须是非空 str")
        normalized_file_name = file_name.strip()
        task = self._task_service.get_analysis_callback_recovery_record(
            normalized_file_name,
        )
        if task is None:
            return None
        if str(task.get("status") or "") not in _TERMINAL_PUBLIC_STATUSES:
            return None
        if task.get("callback_status") not in _RECOVERABLE_CALLBACK_STATUSES:
            return None

        execution_id = task.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise RuntimeError("可恢复文件回调缺少execution_id")
        if task.get("execution_business_type") is None:
            # 切换前的旧 file 路径没有完整的新 execution 快照；它继续由旧恢复器处理，
            # 不能被新 Guard 恢复链猜测性接管。
            return None
        if (
            task.get("execution_business_type") != _ANALYSIS_BUSINESS_TYPE
            or task.get("execution_business_key") != normalized_file_name
            or not isinstance(task.get("batch_id"), str)
            or task.get("batch_sequence") is None
        ):
            return None
        payload = task.get("result_payload")
        if not isinstance(payload, Mapping):
            raise RuntimeError("可恢复文件回调缺少公开payload")
        if payload.get("businessType") != _ANALYSIS_BUSINESS_TYPE:
            raise RuntimeError("可恢复文件回调payload businessType无效")
        data = payload.get("data")
        if not isinstance(data, Mapping) or data.get("fileName") != normalized_file_name:
            raise RuntimeError("可恢复文件回调payload与业务键不一致")
        status = data.get("status")
        if status not in _TERMINAL_PUBLIC_STATUSES:
            raise RuntimeError("可恢复文件回调payload status无效")

        candidate = AnalysisCallbackRecoveryCandidate(
            execution=AnalysisExecutionRef(
                task_id=TaskId(execution_id),
                file_name=normalized_file_name,
                batch_id=task.get("batch_id"),  # type: ignore[arg-type]
                batch_sequence=task.get("batch_sequence"),  # type: ignore[arg-type]
            ),
            payload=FrozenJsonObject.from_mapping(
                dict(payload),
                name="analysis_callback_recovery_payload",
            ),
            callback_attempts=task.get("callback_attempts"),  # type: ignore[arg-type]
        )
        logger.debug(
            "已加载文件分析同步回调恢复候选: "
            "task_id=%s file_name=%s callback_status=%s callback_attempts=%s",
            candidate.execution.task_id,
            normalized_file_name,
            task.get("callback_status"),
            candidate.callback_attempts,
        )
        return candidate


__all__ = ("SQLiteAnalysisCallbackRecoverySource",)
