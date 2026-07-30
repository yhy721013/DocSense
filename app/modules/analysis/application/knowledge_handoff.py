"""文件分析的永久知识库转交与翻译降级协作器。"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable

from app.modules.analysis.domain.errors import AnalysisContractError
from app.modules.analysis.domain.result_mapping import resolve_storage_architecture_id
from app.modules.analysis.domain.task_inputs import (
    AnalysisTaskInputV1,
    AnalysisTaskInputV3,
    FrozenJsonObject,
)
from app.modules.analysis.ports import (
    AnalysisExecutionRef,
    AnalysisKnowledgeDocumentMetadata,
    AnalysisKnowledgePort,
    AnalysisKnowledgeWriteOutcome,
    AnalysisKnowledgeWriteRequest,
    AnalysisKnowledgeWriteResult,
    AnalysisTranslationKind,
    AnalysisTranslationOutcome,
    AnalysisTranslationPort,
    AnalysisTranslationRequest,
    PreparedAnalysisDocument,
)

from .workflow_models import (
    AnalysisApplicationContractError,
    _AnalysisKnownFailure,
    _AnalysisWorkflowPlan,
    _RagWorkflowState,
)


# 保持拆分前的日志分类，避免日志采集和既有检索规则因模块路径变化而失效。
logger = logging.getLogger("app.modules.analysis.application.run_analysis")


class _AnalysisKnowledgeHandoff:
    """在模型审计完成后处理不可逆知识库写入和可降级翻译。"""

    def __init__(
        self,
        knowledge: AnalysisKnowledgePort,
        translation: AnalysisTranslationPort,
    ) -> None:
        if not isinstance(knowledge, AnalysisKnowledgePort):
            raise TypeError("knowledge 必须实现 AnalysisKnowledgePort")
        if not isinstance(translation, AnalysisTranslationPort):
            raise TypeError("translation 必须实现 AnalysisTranslationPort")
        self._knowledge = knowledge
        self._translation = translation

    def persist_knowledge(
        self,
        *,
        execution: AnalysisExecutionRef,
        snapshot: AnalysisTaskInputV1,
        plan: _AnalysisWorkflowPlan,
        state: _RagWorkflowState,
        mapped_result: dict[str, Any],
        on_result: Callable[[AnalysisKnowledgeWriteRequest, AnalysisKnowledgeWriteResult], None]
        | None = None,
    ) -> None:
        """在审计成功后转交同一 RAG 文档，并按三态结果决定是否保留现场。"""

        if state.session is None or not state.session.document_bound:
            raise AnalysisApplicationContractError("永久知识库写入前缺少已绑定 RAG 文档")
        architecture_id = mapped_result.get("architectureId")
        if isinstance(architecture_id, bool) or not isinstance(architecture_id, int):
            raise AnalysisContractError("映射结果缺少有效 architectureId")
        storage_architecture_id = resolve_storage_architecture_id(
            architecture_id,
            plan.ranges["architectureList"],
        )
        if storage_architecture_id is None or storage_architecture_id < 1:
            raise AnalysisContractError("无法确定永久知识库存储分类")
        attributes = {
            key: mapped_result.get(key, "")
            for key in ("country", "channel", "maturity", "security", "format")
        }
        idempotency_key = self.knowledge_idempotency_key(
            file_name=snapshot.file_name,
            architecture_id=storage_architecture_id,
            content_sha256=state.session.content_sha256,
        )
        request = AnalysisKnowledgeWriteRequest(
            execution=execution,
            architecture_id=storage_architecture_id,
            idempotency_key=idempotency_key,
            document=state.session,
            metadata=AnalysisKnowledgeDocumentMetadata(
                file_name=snapshot.file_name,
                original_file_name=self.knowledge_original_file_name(
                    snapshot=snapshot,
                    legacy_original_name=plan.original_name,
                ),
                attributes=FrozenJsonObject.from_mapping(attributes, name="knowledge_attributes"),
            ),
        )
        result = self._knowledge.persist(request)
        if result.execution != execution or result.idempotency_key != idempotency_key:
            raise AnalysisApplicationContractError("永久知识库结果与当前 execution 不一致")
        if on_result is not None:
            if not callable(on_result):
                raise TypeError("on_result 必须可调用或为 None")
            try:
                # 无论三态为何，外部调用已经返回，资源事实必须先于后续进度、终态或
                # RAG close 落库。写入失败时保留现场，不能猜测性执行清理。
                on_result(request, result)
            except Exception:
                state.preserve_scene = True
                raise
        if result.outcome is AnalysisKnowledgeWriteOutcome.COMMITTED:
            state.retain_document = True
            return
        if result.outcome is AnalysisKnowledgeWriteOutcome.NOT_APPLIED:
            state.retain_document = False
            raise _AnalysisKnownFailure("knowledge_index", result.detail_code)
        state.retain_document = True
        state.preserve_scene = True
        raise _AnalysisKnownFailure("knowledge_index_unknown", result.detail_code)

    @staticmethod
    def knowledge_original_file_name(
        *,
        snapshot: AnalysisTaskInputV1,
        legacy_original_name: str,
    ) -> str:
        """解析永久知识展示名，同时保持旧快照的恢复兼容语义。

        V3 任务沿用受理时冻结且已经过合同校验的展示标题，使永久知识
        ``original_name`` 与首次上传的 ``metadata.title`` 严格一致。旧 V1/V2
        快照没有该事实，继续使用历史 plan 值。
        """

        if not isinstance(snapshot, AnalysisTaskInputV1):
            raise TypeError("snapshot 必须是 AnalysisTaskInputV1")
        if not isinstance(legacy_original_name, str) or not legacy_original_name.strip():
            raise ValueError("legacy_original_name 必须是非空字符串")
        if isinstance(snapshot, AnalysisTaskInputV3):
            return snapshot.rag_naming.display_title
        return legacy_original_name

    def enrich_translations(
        self,
        *,
        execution: AnalysisExecutionRef,
        snapshot: AnalysisTaskInputV1,
        prepared: PreparedAnalysisDocument,
        mapped_result: dict[str, Any],
    ) -> None:
        """保持翻译失败降级为空展示字段的旧语义，不让它覆盖知识库已提交事实。"""

        file_item = mapped_result.get("fileDataItem")
        if not isinstance(file_item, dict):
            raise AnalysisApplicationContractError("映射结果缺少 fileDataItem")
        enable_full_translation = bool(
            snapshot.raw_params.to_dict().get("enableFullTranslation", True)
        )
        if enable_full_translation:
            if prepared.prepared_artifact is not None:
                request = AnalysisTranslationRequest(
                    execution=execution,
                    kind=AnalysisTranslationKind.DOCUMENT,
                    # 生产文件 Adapter 必须提供该引用；RAG、正文读取和全文翻译共享
                    # 同一份 prepared Artifact，Translation Application 不接收路径。
                    prepared_artifact=prepared.prepared_artifact,
                )
            else:
                # 两级 OCR 均明确失败时，生产 Adapter 会让 RAG 使用原 PDF，同时不提供
                # prepared 文本 Artifact。这里沿用既有兼容请求形态，生产 Translation
                # Adapter 会稳定返回可降级失败，公开翻译字段保持为空。
                request = AnalysisTranslationRequest(
                    execution=execution,
                    kind=AnalysisTranslationKind.DOCUMENT,
                    source_path=prepared.processing_path,
                )
        else:
            summary = file_item.get("summary", "")
            if not isinstance(summary, str) or not summary:
                return
            request = AnalysisTranslationRequest(
                execution=execution,
                kind=AnalysisTranslationKind.SUMMARY,
                text=summary,
            )
        try:
            result = self._translation.translate(request)
            if result.execution != execution or result.kind is not request.kind:
                raise AnalysisApplicationContractError("翻译结果与当前 execution 不一致")
            if result.outcome is AnalysisTranslationOutcome.SUCCEEDED:
                file_item["documentTranslationOne"] = result.document_translation_one
                file_item["documentTranslationTwo"] = result.document_translation_two
                return
            logger.warning(
                "文件分析翻译可降级失败，保留空展示字段: task_id=%s kind=%s error_code=%s",
                execution.task_id,
                request.kind.value,
                result.error_code,
            )
        except Exception as error:
            # 旧链路也把翻译异常降级为未翻译结果。异常只记录类型，避免把正文或模型响应
            # 写入普通日志。
            logger.warning(
                "文件分析翻译发生可降级异常，保留空展示字段: task_id=%s kind=%s error_type=%s",
                execution.task_id,
                request.kind.value,
                type(error).__name__,
                exc_info=True,
            )

    @staticmethod
    def knowledge_idempotency_key(
        *,
        file_name: str,
        architecture_id: int,
        content_sha256: str,
    ) -> str:
        """复刻既有永久知识幂等键算法，避免导入供应商 Port 到 Application。"""

        canonical = f"{file_name}\0{architecture_id}\0{content_sha256.casefold()}"
        return f"document:v1:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
