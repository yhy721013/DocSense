"""共享文档处理的幂等应用用例。"""

from __future__ import annotations

import logging
import time

from app.modules.document_processing.domain import (
    DocumentProcessingError,
    DocumentProcessingRequest,
    DocumentProcessingResult,
    LineageEvent,
    ProcessingOutcome,
)
from app.modules.document_processing.ports import (
    ArtifactPublication,
    ArtifactStorePort,
    DocumentProcessorPort,
    ProcessingAcquireDecision,
    ProcessingRecordPort,
)


logger = logging.getLogger(__name__)


class PrepareDocument:
    """协调 Processor、Artifact Store 和 Processing Record。

    SQLite/文件锁内绝不执行转换、网络、OCR 或大文件复制。Application 只处理不透明
    ``ArtifactRef``，不会读取、拼接或记录宿主路径。
    """

    def __init__(
        self,
        *,
        processor: DocumentProcessorPort,
        artifact_store: ArtifactStorePort,
        records: ProcessingRecordPort,
    ) -> None:
        if not isinstance(processor, DocumentProcessorPort):
            raise TypeError("processor 必须实现 DocumentProcessorPort")
        if not isinstance(artifact_store, ArtifactStorePort):
            raise TypeError("artifact_store 必须实现 ArtifactStorePort")
        if not isinstance(records, ProcessingRecordPort):
            raise TypeError("records 必须实现 ProcessingRecordPort")
        self._processor = processor
        self._artifact_store = artifact_store
        self._records = records

    def execute(
        self,
        request: DocumentProcessingRequest,
    ) -> DocumentProcessingResult:
        if not isinstance(request, DocumentProcessingRequest):
            raise TypeError("request 必须是 DocumentProcessingRequest")
        started_at = time.monotonic()
        step_key = request.step_key

        try:
            acquired = self._records.acquire(request)
        except Exception:
            logger.exception(
                "文档处理步骤受理失败: task_id=%s step_key=%s",
                request.task_id,
                step_key[:12],
            )
            return self._non_success(
                request,
                ProcessingOutcome.OUTCOME_UNKNOWN,
                "processing_record_acquire_failed",
            )

        if acquired.decision is ProcessingAcquireDecision.SUCCEEDED:
            artifact = acquired.snapshot.artifact
            lineage = acquired.snapshot.lineage
            assert artifact is not None and lineage is not None
            try:
                intact = self._artifact_store.verify(artifact)
            except Exception:
                intact = False
                logger.exception(
                    "复用文档 Artifact 时完整性检查异常: task_id=%s "
                    "step_key=%s artifact_id=%s",
                    request.task_id,
                    step_key[:12],
                    artifact.artifact_id[:12],
                )
            if not intact:
                self._mark_unknown_best_effort(
                    request,
                    error_code="reused_artifact_integrity_failed",
                )
                return self._non_success(
                    request,
                    ProcessingOutcome.OUTCOME_UNKNOWN,
                    "reused_artifact_integrity_failed",
                )
            logger.info(
                "复用已完成的文档处理 Artifact: task_id=%s step_key=%s "
                "artifact_id=%s",
                request.task_id,
                step_key[:12],
                artifact.artifact_id[:12],
            )
            return DocumentProcessingResult(
                outcome=ProcessingOutcome.SUCCEEDED,
                step_key=step_key,
                artifact=artifact,
                lineage=lineage,
                reused=True,
            )

        if acquired.decision is ProcessingAcquireDecision.RUNNING:
            return self._non_success(
                request,
                ProcessingOutcome.SKIPPED,
                "processing_step_in_progress",
            )
        if acquired.decision is ProcessingAcquireDecision.FAILED:
            return self._non_success(
                request,
                ProcessingOutcome.FAILED,
                acquired.snapshot.error_code or "processing_step_failed",
            )
        if acquired.decision is ProcessingAcquireDecision.OUTCOME_UNKNOWN:
            return self._non_success(
                request,
                ProcessingOutcome.OUTCOME_UNKNOWN,
                acquired.snapshot.error_code or "processing_outcome_unknown",
            )

        claim_token = acquired.snapshot.claim_token
        if not claim_token:  # pragma: no cover - Port DTO 已强制该不变量
            raise RuntimeError("acquired 记录缺少 claim_token")

        try:
            candidate = self._processor.process(request)
            if (
                candidate.representation
                is not request.profile.target_representation
            ):
                self._close_candidate_best_effort(candidate, request)
                raise DocumentProcessingError(
                    "processor_representation_mismatch",
                    "Processor 输出表示与冻结 profile 不一致",
                )
        except DocumentProcessingError as exc:
            if "candidate" in locals():
                self._close_candidate_best_effort(candidate, request)
            return self._record_processing_failure(
                request,
                claim_token=claim_token,
                error_code=exc.code,
                outcome_unknown=exc.outcome_unknown,
            )
        except Exception:
            if "candidate" in locals():
                self._close_candidate_best_effort(candidate, request)
            logger.exception(
                "文档 Processor 执行异常: task_id=%s step_key=%s "
                "processor_id=%s",
                request.task_id,
                step_key[:12],
                request.profile.processor_id,
            )
            return self._record_processing_failure(
                request,
                claim_token=claim_token,
                error_code="processor_unexpected_error",
                outcome_unknown=False,
            )

        publication = ArtifactPublication(
            task_id=request.task_id,
            step_key=step_key,
            kind=candidate.kind,
            representation=candidate.representation,
            media_type=candidate.media_type,
            ordinal=candidate.ordinal,
        )
        try:
            # 内容复制和 fsync 全部发生在 Record 事务之外。
            artifact = self._artifact_store.publish(
                publication,
                candidate.content,
            )
        except DocumentProcessingError as exc:
            self._close_candidate_best_effort(candidate, request)
            return self._record_processing_failure(
                request,
                claim_token=claim_token,
                error_code=exc.code,
                outcome_unknown=exc.outcome_unknown,
            )
        except Exception:
            self._close_candidate_best_effort(candidate, request)
            logger.exception(
                "发布文档 Artifact 异常: task_id=%s step_key=%s",
                request.task_id,
                step_key[:12],
            )
            return self._record_processing_failure(
                request,
                claim_token=claim_token,
                error_code="artifact_publish_failed",
                outcome_unknown=False,
            )

        try:
            lineage = LineageEvent.create(request=request, child=artifact)
            # 这里的 complete 只执行短数据库事务：Artifact 元数据、lineage 与步骤终态
            # 必须一次提交，不能让成功步骤指向未持久化的谱系。
            self._records.complete(
                request,
                claim_token=claim_token,
                artifact=artifact,
                lineage=lineage,
            )
        except Exception:
            self._close_candidate_best_effort(candidate, request)
            logger.exception(
                "Artifact 已发布但 Processing Record 提交失败，保留文件等待恢复: "
                "task_id=%s step_key=%s artifact_id=%s",
                request.task_id,
                step_key[:12],
                artifact.artifact_id[:12],
            )
            self._mark_unknown_best_effort(
                request,
                claim_token=claim_token,
                error_code="artifact_published_record_outcome_unknown",
            )
            return self._non_success(
                request,
                ProcessingOutcome.OUTCOME_UNKNOWN,
                "artifact_published_record_outcome_unknown",
            )

        self._close_candidate_best_effort(candidate, request)
        logger.info(
            "文档处理步骤完成: task_id=%s step_key=%s artifact_id=%s "
            "bytes=%d checksum=%s duration_ms=%d",
            request.task_id,
            step_key[:12],
            artifact.artifact_id[:12],
            artifact.metadata.size_bytes,
            artifact.metadata.sha256[:12],
            int((time.monotonic() - started_at) * 1000),
        )
        return DocumentProcessingResult(
            outcome=ProcessingOutcome.SUCCEEDED,
            step_key=step_key,
            artifact=artifact,
            lineage=lineage,
            warnings=candidate.warnings,
        )

    @staticmethod
    def _close_candidate_best_effort(
        candidate: object,
        request: DocumentProcessingRequest,
    ) -> None:
        """清理 Processor scratch；清理失败不撤销已提交的 Artifact 事实。"""

        try:
            close = getattr(candidate, "close")
            close()
        except Exception:
            logger.warning(
                "文档 Processor scratch 清理失败，将由启动巡检继续处理: "
                "task_id=%s step_key=%s",
                request.task_id,
                request.step_key[:12],
                exc_info=True,
            )

    def _record_processing_failure(
        self,
        request: DocumentProcessingRequest,
        *,
        claim_token: str,
        error_code: str,
        outcome_unknown: bool,
    ) -> DocumentProcessingResult:
        outcome = (
            ProcessingOutcome.OUTCOME_UNKNOWN
            if outcome_unknown
            else ProcessingOutcome.FAILED
        )
        try:
            if outcome_unknown:
                self._records.mark_outcome_unknown(
                    request,
                    claim_token=claim_token,
                    error_code=error_code,
                )
            else:
                self._records.fail(
                    request,
                    claim_token=claim_token,
                    error_code=error_code,
                )
        except Exception:
            # 失败事实未能可靠提交时不能向上层承诺“已失败且可安全重试”。
            logger.exception(
                "文档处理失败状态持久化异常: task_id=%s step_key=%s",
                request.task_id,
                request.step_key[:12],
            )
            outcome = ProcessingOutcome.OUTCOME_UNKNOWN
            error_code = "processing_failure_record_outcome_unknown"
        return self._non_success(request, outcome, error_code)

    def _mark_unknown_best_effort(
        self,
        request: DocumentProcessingRequest,
        *,
        error_code: str,
        claim_token: str | None = None,
    ) -> None:
        try:
            self._records.mark_outcome_unknown(
                request,
                claim_token=claim_token,
                error_code=error_code,
            )
        except Exception:
            logger.exception(
                "标记文档处理 outcome_unknown 失败，需由巡检识别: "
                "task_id=%s step_key=%s",
                request.task_id,
                request.step_key[:12],
            )

    @staticmethod
    def _non_success(
        request: DocumentProcessingRequest,
        outcome: ProcessingOutcome,
        error_code: str,
    ) -> DocumentProcessingResult:
        return DocumentProcessingResult(
            outcome=outcome,
            step_key=request.step_key,
            error_code=error_code,
        )


__all__ = ["PrepareDocument"]
