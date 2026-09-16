"""文档处理终态的显式恢复与对账用例。"""

from __future__ import annotations

import logging
import time
from typing import Callable

from app.modules.document_processing.domain import (
    ArtifactRef,
    DocumentProcessingError,
    DocumentProcessingRequest,
    LineageEvent,
)
from app.modules.document_processing.ports import (
    ArtifactStorePort,
    ProcessingRecoveryPort,
)


logger = logging.getLogger(__name__)


class ReconcileProcessingRecord:
    """把人工或供应商已经确认的事实安全写回 Processing Record。

    本用例刻意不实现“unknown 自动重跑”。调用方必须先根据供应商任务身份、
    保留的 scratch 或已发布 Artifact 完成对账，再选择确认失败或恢复成功。
    """

    def __init__(
        self,
        *,
        artifact_store: ArtifactStorePort,
        recovery: ProcessingRecoveryPort,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(artifact_store, ArtifactStorePort):
            raise TypeError("artifact_store 必须实现 ArtifactStorePort")
        if not isinstance(recovery, ProcessingRecoveryPort):
            raise TypeError("recovery 必须实现 ProcessingRecoveryPort")
        self._artifact_store = artifact_store
        self._recovery = recovery
        self._clock = clock

    def quarantine_stale_running(
        self,
        *,
        older_than_seconds: float,
        limit: int = 100,
    ) -> tuple[str, ...]:
        """将疑似进程崩溃遗留的 running 隔离为 unknown，等待逐项对账。"""

        if isinstance(older_than_seconds, bool) or not isinstance(
            older_than_seconds, (int, float)
        ):
            raise TypeError("older_than_seconds 必须是数字")
        if older_than_seconds <= 0:
            raise ValueError("older_than_seconds 必须大于 0")
        return self._recovery.quarantine_stale_running(
            stale_before_epoch=self._clock() - float(older_than_seconds),
            limit=limit,
        )

    def confirm_failed(
        self,
        request: DocumentProcessingRequest,
        *,
        confirmed_error_code: str,
    ) -> None:
        """供应商或本地现场已确认失败后，解除 unknown/running 卡点。"""

        self._recovery.resolve_failed(
            request,
            error_code=confirmed_error_code,
        )

    def recover_succeeded(
        self,
        request: DocumentProcessingRequest,
        *,
        artifact: ArtifactRef,
    ) -> LineageEvent:
        """验证已发布 Artifact 后，原子修复成功记录与谱系。"""

        try:
            intact = self._artifact_store.verify(artifact)
        except Exception as exc:
            logger.exception(
                "恢复成功事实前 Artifact 校验异常: task_id=%s step_key=%s "
                "artifact_id=%s",
                request.task_id,
                request.step_key[:12],
                artifact.artifact_id[:12],
            )
            raise DocumentProcessingError(
                "recovery_artifact_verification_failed",
                "恢复用 Artifact 无法完成完整性校验",
                outcome_unknown=True,
            ) from exc
        if not intact:
            raise DocumentProcessingError(
                "recovery_artifact_integrity_failed",
                "恢复用 Artifact 不存在或完整性校验失败",
                outcome_unknown=True,
            )
        lineage = LineageEvent.create(request=request, child=artifact)
        self._recovery.recover_completed(
            request,
            artifact=artifact,
            lineage=lineage,
        )
        return lineage


__all__ = ["ReconcileProcessingRecord"]
