"""将既有长期知识库 Gateway 适配为 Analysis 三态写入 Port。"""

from __future__ import annotations

import logging

from app.shared.domain.knowledge_workspace import permanent_architecture_workspace_name
from app.modules.analysis.ports.knowledge import (
    AnalysisKnowledgePort,
    AnalysisKnowledgeWriteOutcome,
    AnalysisKnowledgeWriteRequest,
    AnalysisKnowledgeWriteResult,
)
from app.ports import (
    CollectionSpec,
    KnowledgeDocumentMetadata,
    KnowledgeIndexConflictError,
    KnowledgeIndexDocumentReleasedError,
    KnowledgeIndexFactory,
    KnowledgeIndexRecoveryRequiredError,
    KnowledgeIndexRetentionRequiredError,
    KnowledgeOperationContext,
    PreparedDocumentRef,
)


logger = logging.getLogger(__name__)


class LegacyAnalysisKnowledgeAdapter(AnalysisKnowledgePort):
    """复用已验证的 ``store_prepared_document``，但显式暴露三态结果。

    Adapter 不重新读取或上传源文件。RAG 返回的文档四元组是唯一允许转交长期知识库的
    证据；任何未分类异常都保守地返回 ``OUTCOME_UNKNOWN``，由后续 1F-6 资源恢复保留
    现场，而不是误删可能已被永久集合接管的全局文档。
    """

    def __init__(self, knowledge_index_factory: KnowledgeIndexFactory) -> None:
        if not isinstance(knowledge_index_factory, KnowledgeIndexFactory):
            raise TypeError("knowledge_index_factory 必须实现 KnowledgeIndexFactory")
        self._knowledge_index_factory = knowledge_index_factory

    def persist(
        self,
        request: AnalysisKnowledgeWriteRequest,
    ) -> AnalysisKnowledgeWriteResult:
        """写入或复用长期知识文档，并严格区分可判定与未知的外部结果。"""

        if not isinstance(request, AnalysisKnowledgeWriteRequest):
            raise TypeError("request 必须是 AnalysisKnowledgeWriteRequest")
        document = request.document
        prepared_document = PreparedDocumentRef(
            document_ref=document.document_ref,
            external_location=document.document_location,
            content_sha256=document.content_sha256,
            ingested_file_name=document.ingested_file_name,
            structured_source_key=document.structured_source_key,
        )
        metadata = KnowledgeDocumentMetadata(
            file_name=request.metadata.file_name,
            original_name=request.metadata.original_file_name,
            ingested_file_name=document.ingested_file_name,
            attributes=request.metadata.attributes.to_dict(),
        )
        operation_context = KnowledgeOperationContext(
            execution_id=request.execution.task_id.value,
            business_type="file",
            business_key=request.execution.file_name,
        )
        # 永久 Workspace 名称必须由跨业务共享的纯规则生成，确保 Analysis 首次入库、
        # Reassign 目标准备和故障恢复不会各自维护不同前缀。
        workspace_name = permanent_architecture_workspace_name(request.architecture_id)
        collection_spec = CollectionSpec(
            architecture_id=request.architecture_id,
            name=workspace_name,
        )

        logger.info(
            "开始将文件分析文档转交永久知识库: task_id=%s architecture_id=%d "
            "workspace_name=%s",
            request.execution.task_id,
            request.architecture_id,
            workspace_name,
        )
        try:
            with self._knowledge_index_factory.create() as knowledge_index:
                collection = knowledge_index.ensure_collection(collection_spec)
                indexed = knowledge_index.store_prepared_document(
                    collection,
                    prepared_document,
                    metadata,
                    operation_context=operation_context,
                    idempotency_key=request.idempotency_key,
                )
            # 远端调用返回并不代表本地已经得到可持久恢复的成功证据。结果校验必须仍在
            # try 范围内：若远端已提交但 SDK 返回对象不完整，只能判定为结果未知。
            external_ref = str(
                getattr(indexed, "external_location", "") or ""
            ).strip()
            if not external_ref:
                logger.critical(
                    "文件分析永久知识库成功结果缺少外部引用，结果保持未知: "
                    "task_id=%s architecture_id=%d workspace_name=%s",
                    request.execution.task_id,
                    request.architecture_id,
                    workspace_name,
                )
                return AnalysisKnowledgeWriteResult(
                    execution=request.execution,
                    idempotency_key=request.idempotency_key,
                    outcome=AnalysisKnowledgeWriteOutcome.OUTCOME_UNKNOWN,
                    detail_code="knowledge_success_result_invalid",
                )
        except KnowledgeIndexDocumentReleasedError:
            logger.warning(
                "文件分析永久知识库未接管且补偿已确认: task_id=%s "
                "architecture_id=%d workspace_name=%s",
                request.execution.task_id,
                request.architecture_id,
                workspace_name,
                exc_info=True,
            )
            return AnalysisKnowledgeWriteResult(
                execution=request.execution,
                idempotency_key=request.idempotency_key,
                outcome=AnalysisKnowledgeWriteOutcome.NOT_APPLIED,
                detail_code="knowledge_document_released",
            )
        except (KnowledgeIndexRetentionRequiredError, KnowledgeIndexRecoveryRequiredError):
            logger.critical(
                "文件分析永久知识库写入结果需保留现场: task_id=%s "
                "architecture_id=%d workspace_name=%s",
                request.execution.task_id,
                request.architecture_id,
                workspace_name,
                exc_info=True,
            )
            return AnalysisKnowledgeWriteResult(
                execution=request.execution,
                idempotency_key=request.idempotency_key,
                outcome=AnalysisKnowledgeWriteOutcome.OUTCOME_UNKNOWN,
                detail_code="knowledge_retention_required",
            )
        except KnowledgeIndexConflictError:
            # 幂等键对应了不同业务事实属于本地合同损坏，不能伪装成普通“未写入”。
            logger.exception(
                "文件分析永久知识库幂等事实冲突: task_id=%s "
                "architecture_id=%d workspace_name=%s",
                request.execution.task_id,
                request.architecture_id,
                workspace_name,
            )
            raise
        except Exception:
            logger.critical(
                "文件分析永久知识库写入出现未分类异常，结果保持未知: "
                "task_id=%s architecture_id=%d workspace_name=%s",
                request.execution.task_id,
                request.architecture_id,
                workspace_name,
                exc_info=True,
            )
            return AnalysisKnowledgeWriteResult(
                execution=request.execution,
                idempotency_key=request.idempotency_key,
                outcome=AnalysisKnowledgeWriteOutcome.OUTCOME_UNKNOWN,
                detail_code="knowledge_write_outcome_unknown",
            )

        logger.info(
            "文件分析文档已转交永久知识库: task_id=%s architecture_id=%d "
            "workspace_name=%s",
            request.execution.task_id,
            request.architecture_id,
            workspace_name,
        )
        return AnalysisKnowledgeWriteResult(
            execution=request.execution,
            idempotency_key=request.idempotency_key,
            outcome=AnalysisKnowledgeWriteOutcome.COMMITTED,
            external_ref=external_ref,
        )


__all__ = ("LegacyAnalysisKnowledgeAdapter",)
