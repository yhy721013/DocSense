from __future__ import annotations

import json
import hashlib
import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

import fitz

from app.ports import (
    CollectionSpec,
    DocumentRagFactory,
    DocumentRagSession,
    KnowledgeDocumentMetadata,
    KnowledgeIndexDocumentReleasedError,
    KnowledgeIndexError,
    KnowledgeIndexFactory,
    KnowledgeIndexRetentionRequiredError,
    KnowledgeOperationContext,
    PreparedDocumentRef,
    RagExecutionTrace,
    RagLifecycleEvent,
    RagOperationError,
    RagPromptKind,
    build_document_idempotency_key,
    normalize_rag_prompt,
)
from app.services.core.config import (
    ANALYSIS_DATA_STANDARD_MODE_LEGACY,
    ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    ANALYSIS_DATA_STANDARD_MODES,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
    ANALYSIS_FILENAME_CONSTRAINT_MODES,
    ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
    ANALYSIS_IDENTITY_RESELECT_MODE_OFF,
    ANALYSIS_IDENTITY_RESELECT_MODES,
    load_ocr_config,
)
from app.services.core.architecture_tree import (
    ArchitectureNodeProfile,
    ArchitectureTreeIndex,
    ArchitectureTreeIndexCache,
    ArchitectureTreeValidationError,
    build_architecture_tree_index,
)
from app.services.utils.ocr_preprocessor import prepare_analysis_file_for_upload

from app.services.utils.callback_client import post_callback_payload
from app.services.utils.file_downloader import download_to_temp_file
from app.services.utils.mhtml_normalizer import extract_text_from_mhtml, is_mhtml_file, normalize_file_for_llm
from app.services.utils.word_extractor import extract_text_from_word
from app.services.core.progress_hub import LLMProgressHub
from app.services.core.prompts import (
    ANALYSIS_ENUM_FIELD_MAX_ITEMS,
    ANALYSIS_ENUM_ITEM_MAX_CHARS,
    ANALYSIS_KEYWORD_MAX_COUNT,
    ANALYSIS_KEYWORD_MAX_CHARS,
    ANALYSIS_KEYWORD_MIN_COUNT,
    ANALYSIS_SUMMARY_MAX_CHARS,
    ANALYSIS_SUMMARY_TYPE_CHAR_RANGES,
    UNKNOWN_SOURCE_VALUE,
    build_architecture_classification_prompt,
    build_architecture_repair_prompt,
    build_architecture_reselect_prompt,
    build_data_standard_classification_prompt,
    build_file_analysis_prompt,
    build_file_extraction_prompt,
    build_json_repair_prompt,
    data_standard_candidate_remark,
)
from app.services.llm_service.architecture_recall_service import (
    ArchitecturePromptBudgetError,
    ArchitectureRecallDecision,
    ArchitectureRecallError,
    DocumentArchitectureSignals,
    build_document_architecture_signals,
    recall_architecture_candidates,
)
from app.services.llm_service.task_service import (
    LLMTaskService,
    TaskExecutionConflictError,
    TaskStateConflictError,
)
from app.services.llm_service.translation_service import get_translation_service

# 阶段 1F-1 兼容层：纯 Domain 规则已迁移，旧 Service 仅继续承载文件、RAG、翻译、
# 任务和回调等外部副作用。使用各模块显式导出的兼容面，确保现有内部调用与测试导入不变。
from app.modules.analysis.domain.callback_payloads import *  # noqa: F401,F403
from app.modules.analysis.domain.classification_rules import *  # noqa: F401,F403
from app.modules.analysis.domain.errors import *  # noqa: F401,F403
from app.modules.analysis.domain.models import *  # noqa: F401,F403
from app.modules.analysis.domain.ranges import *  # noqa: F401,F403
from app.modules.analysis.domain.result_mapping import (
    _sanitize_related_technologies_with_diagnostics,
)
from app.modules.analysis.domain.result_mapping import *  # noqa: F401,F403


logger = logging.getLogger(__name__)

# 结果映射已经迁入无副作用 Domain。旧执行链仍保留少量历史告警，便于运维人员观察
# “过滤后继续成功”的 relatedTechnology 质量问题；告警适配不参与任何字段决策。
_domain_map_analysis_result = map_analysis_result


def _log_related_technology_compatibility_warnings(
        parsed_result: Dict[str, Any],
        *,
        original_text: str,
) -> None:
    """在旧 Service 边界保留已冻结的所属技术质量告警。

    Domain 只负责确定性规范化，不能自行写日志。此函数根据迁移前同一输入/输出语义记录
    诊断信息，不修改结果、不抛出业务异常，也不触发外部 I/O。
    """

    parsed_file_item = parsed_result.get("fileDataItem")
    if not isinstance(parsed_file_item, dict):
        parsed_file_item = parsed_result.get("文件解析详细数据")
    if not isinstance(parsed_file_item, dict):
        parsed_file_item = {}
    raw_related_technology = (
        _first_non_empty_value(
            parsed_file_item,
            "relatedTechnology",
            "所属技术",
        )
        or _first_non_empty_value(
            parsed_result,
            "relatedTechnology",
            "所属技术",
        )
    )
    raw_related_technology_evidence = (
        _first_non_empty_value(
            parsed_file_item,
            "relatedTechnologyEvidence",
            "所属技术证据",
        )
        or _first_non_empty_value(
            parsed_result,
            "relatedTechnologyEvidence",
            "所属技术证据",
        )
    )
    diagnostics = _sanitize_related_technologies_with_diagnostics(
        raw_related_technology,
        raw_evidence=raw_related_technology_evidence,
        original_text=original_text,
    )
    if diagnostics.non_chinese_count:
        logger.warning(
            "文件分析所属技术不是中文名称，已丢弃并继续成功: rejected_count=%d",
            diagnostics.non_chinese_count,
        )
    if not diagnostics.chinese_count:
        return

    if not diagnostics.evidence_text_present:
        logger.warning(
            "文件分析所属技术缺少可核验正文，已保留规范化结果并继续成功: "
            "retained_count=%d",
            diagnostics.chinese_count,
        )
        return

    if diagnostics.missing_evidence_count:
        logger.warning(
            "文件分析所属技术缺少可核验原文术语映射，已丢弃并继续成功: "
            "rejected_count=%d accepted_count=%d",
            diagnostics.missing_evidence_count,
            diagnostics.accepted_before_limit,
        )
    if diagnostics.overflow_count:
        logger.warning(
            "文件分析所属技术数量超过上限，已保留前序合格项并继续成功: "
            "actual=%d maximum=%d",
            diagnostics.accepted_before_limit,
            ANALYSIS_ENUM_FIELD_MAX_ITEMS,
        )


@wraps(_domain_map_analysis_result)
def map_analysis_result(
        parsed_result: Dict[str, Any],
        request_params: Dict[str, Any],
        original_text: str = "",
        resolved_architecture_id: Any = None,
) -> Dict[str, Any]:
    """兼容旧导入路径，并把领域纯结果投影回旧 Service 的日志边界。"""

    mapped_result = _domain_map_analysis_result(
        parsed_result,
        request_params,
        original_text=original_text,
        resolved_architecture_id=resolved_architecture_id,
    )
    _log_related_technology_compatibility_warnings(
        parsed_result,
        original_text=original_text,
    )
    return mapped_result




def _log_analysis_content_warnings(
        parsed_result: Dict[str, Any],
        mapped_result: Dict[str, Any],
        *,
        file_name: str,
) -> None:
    """记录普通内容字段违约，但不改变文件任务的成功状态。"""
    parsed_file_item = parsed_result.get("fileDataItem")
    if not isinstance(parsed_file_item, dict):
        parsed_file_item = parsed_result.get("文件解析详细数据")
    if not isinstance(parsed_file_item, dict):
        parsed_file_item = {}
    mapped_file_item = mapped_result.get("fileDataItem")
    if not isinstance(mapped_file_item, dict):
        mapped_file_item = {}

    model_summary = _resolve_field(
        parsed_result,
        parsed_file_item,
        "summary",
        "摘要",
    )
    summary = _as_text(mapped_file_item.get("summary"))
    if not model_summary:
        logger.warning(
            "文件分析摘要未生成合格内容，已保留标题回退并继续成功: "
            "file_name=%s fallback_chars=%d",
            file_name,
            len(summary),
        )
    if len(summary) > ANALYSIS_SUMMARY_MAX_CHARS:
        logger.warning(
            "文件分析摘要超过通用长度上限，任务继续成功: "
            "file_name=%s actual_chars=%d maximum=%d",
            file_name,
            len(summary),
            ANALYSIS_SUMMARY_MAX_CHARS,
        )
    for prefix, (minimum, maximum) in ANALYSIS_SUMMARY_TYPE_CHAR_RANGES.items():
        if not summary.startswith(prefix):
            continue
        if len(summary) < minimum or len(summary) > maximum:
            logger.warning(
                "文件分析摘要未满足材料类型长度范围，任务继续成功: "
                "file_name=%s material_prefix=%s actual_chars=%d minimum=%d maximum=%d",
                file_name,
                prefix,
                len(summary),
                minimum,
                maximum,
            )
        break

    keyword_count = len(_split_delimited_items(mapped_file_item.get("keyword")))
    if keyword_count < MIN_KEYWORD_COUNT:
        logger.warning(
            "文件分析关键词数量不足，任务继续成功: "
            "file_name=%s actual=%d minimum=%d",
            file_name,
            keyword_count,
            MIN_KEYWORD_COUNT,
        )


def enrich_with_translations(
        mapped_result: Dict[str, Any],
        file_path: str,
        enable_full_translation: bool = False,
) -> Dict[str, Any]:
    """
    为映射结果添加翻译内容

    :param mapped_result: map_analysis_result 返回的映射结果
    :param file_path: 原始文件路径
    :param enable_full_translation: 是否启用全文翻译（否则只翻译摘要）
    :return: 更新后的映射结果
    """
    try:
        translation_service = get_translation_service()

        # 检查是否需要翻译
        file_item = mapped_result.get("fileDataItem", {})
        original_text = file_item.get("originalText", "")
        summary = file_item.get("summary", "")

        if not original_text and not summary:
            return mapped_result

        if enable_full_translation:
            # 全文翻译模式：翻译整个文档
            logger.info(
                "开始全文翻译文档: file_name=%s",
                Path(file_path).name,
            )

            # 翻译服务不再保存任务级可变回调；进度仍由调用本函数的 Worker 在翻译前后
            # 按既有 0.65/0.95 节点发布，避免并发任务彼此覆盖回调归属。
            bilingual_html_content, monolingual_html_content = translation_service.translate_document(
                file_path=file_path,
                target_lang="Chinese",
                translate_all=0,
                use_minerU= True,
            )

            mapped_result["fileDataItem"]["documentTranslationOne"] = monolingual_html_content
            mapped_result["fileDataItem"]["documentTranslationTwo"] = bilingual_html_content

        else:
            # 快速模式：只翻译摘要
            if summary:
                logger.info("开始翻译文档摘要: summary_chars=%d", len(summary))
                translated_summary = translation_service.translate_text_only(summary)
                mapped_result["fileDataItem"]["documentTranslationOne"] = translated_summary
                mapped_result["fileDataItem"]["documentTranslationTwo"] = summary+"\n"+translated_summary

        return mapped_result

    except Exception as e:
        logger.warning(
            "文档翻译失败，返回未翻译结果: error_type=%s",
            type(e).__name__,
        )
        return mapped_result




def _publish_progress(progress_hub: LLMProgressHub, file_name: str, progress: float) -> None:
    progress_hub.publish(
        "file",
        file_name,
        {"businessType": "file", "data": {"fileName": file_name, "progress": progress}},
    )


def _read_original_text(file_path: str) -> str:
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".json", ".csv"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".pdf":
        with fitz.open(path) as doc:
            return "\n".join(page.get_text() for page in doc)
    if suffix == ".docx":
        return extract_text_from_word(str(path))
    if is_mhtml_file(str(path)):
        return extract_text_from_mhtml(str(path))
    return ""


def _prepare_analysis_upload_file(file_path: str) -> str:
    """返回单文件 RAG 实际使用的原文件或 OCR 增强文件路径。

    阶段 9 的 Document RAG Session 严格处理一份目标文档，因此这里不再沿用旧 Pipeline 的
    文件列表语义。路径不存在时保持原值交给 Gateway 统一产生可审计的上传阶段错误。
    """
    path = Path(file_path)
    if not path.exists():
        return str(path)

    upload_path = prepare_analysis_file_for_upload(str(path), load_ocr_config())
    upload_path_obj = Path(upload_path)
    if not upload_path_obj.exists():
        return str(path)

    return str(upload_path_obj)




def _log_architecture_constraint_decision(
    *,
    execution_id: str,
    file_name: str,
    filename_constraint_mode: str,
    profile: _JaneClassificationProfile,
    decision: _ArchitectureConstraintDecision,
    data_standard_mode: str = ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    data_standard_profile: _DataStandardClassificationProfile | None = None,
) -> None:
    if not decision.reason_code:
        return
    standard_profile = (
        data_standard_profile
        or _DataStandardClassificationProfile()
    )
    payload = {
        "executionId": execution_id,
        "fileName": file_name,
        "constraintMode": filename_constraint_mode,
        "dataStandardMode": data_standard_mode,
        "standardNumber": standard_profile.standard_number,
        "standardTitle": standard_profile.title,
        "standardDocumentKind": standard_profile.document_kind,
        "standardIdentityConfirmed": standard_profile.identity_confirmed,
        "standardIdentityConflict": standard_profile.identity_conflict,
        "standardEvidenceSources": list(standard_profile.evidence_sources),
        "scopeKind": profile.scope_kind,
        "extractedTitle": profile.title,
        "primaryIdentifier": profile.primary_identifier,
        "filenameIdentityKind": profile.filename_identity_kind,
        "filenameIdentifiers": list(profile.filename_identifiers),
        "trustedFilenameIdentifiers": list(
            profile.trusted_filename_identifiers
        ),
        "titleIdentifiers": list(profile.title_identifiers),
        "recallIdentityEnabled": profile.recall_identity_enabled,
        "identityConfirmed": profile.identity_confirmed,
        "identityConflict": profile.identity_conflict,
        "qualifier": profile.qualifier,
        "matchedScopeParentId": decision.matched_scope_parent_id,
        "preConstraintArchitectureId": decision.pre_architecture_id,
        "postConstraintArchitectureId": decision.post_architecture_id,
        "constraintReasonCode": decision.reason_code,
        "treeGap": decision.tree_gap,
    }
    logger.info(
        "analysis_architecture_constraint=%s",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def _phase_attempt_count(session: DocumentRagSession, start_count: int) -> int:
    return max(0, len(session.trace.attempts) - start_count)


def _elapsed_ms(started_at: float, *, floor: int = 0) -> int:
    return max(floor, int(math.ceil((time.perf_counter() - started_at) * 1000.0)))


def _recall_audit_fields(
        decision: ArchitectureRecallDecision,
        *,
        prompt_chars: int,
) -> Dict[str, Any]:
    return {
        "tree_fingerprint": decision.tree_fingerprint,
        "query_digest": decision.query_digest,
        "base_top64": list(decision.base_leaf_ids),
        "final_candidates": list(decision.prompt_candidates),
        "channel_rankings": {
            ranking.channel: list(ranking.node_ids)
            for ranking in decision.channel_rankings
        },
        "rrf_scores": dict(decision.rrf_scores),
        "protected_reasons": dict(decision.protected_reasons),
        "prompt_chars": prompt_chars,
        "recall_elapsed_ms": int(math.ceil(decision.elapsed_ms)),
    }


def _direct_recall_audit_fields(
        *,
        tree_index: ArchitectureTreeIndex,
        signals: DocumentArchitectureSignals,
        candidates: Iterable[ArchitectureNodeProfile],
        prompt_chars: int,
        recall_elapsed_ms: int,
        channel_name: str,
) -> Dict[str, Any]:
    nodes = tuple(candidates)
    return {
        "tree_fingerprint": tree_index.fingerprint,
        "query_digest": _architecture_signal_digest(signals),
        "base_top64": [node.id for node in nodes if node.is_leaf][:64],
        "final_candidates": [_node_prompt_projection(node) for node in nodes],
        "channel_rankings": {channel_name: [node.id for node in nodes]},
        "rrf_scores": {},
        "protected_reasons": (
            {nodes[0].id: ["single_candidate"]}
            if len(nodes) == 1 and channel_name == "direct"
            else {}
        ),
        "prompt_chars": prompt_chars,
        "recall_elapsed_ms": recall_elapsed_ms,
    }


def _safe_task_error(error: BaseException, *, fallback: str) -> str:
    """生成有界任务错误，避免把 Prompt、正文或外部响应写入普通日志和回调状态。"""
    if isinstance(
        error,
        (AnalysisContractError, KnowledgeIndexError, RagOperationError, ValueError),
    ):
        message = str(error)
    else:
        message = f"{fallback}（{type(error).__name__}）"
    return " ".join((message or fallback).split())[:500]


def _record_lease_resources(
        task_service: LLMTaskService,
        execution_id: str,
        trace: RagExecutionTrace,
        prepared_document: PreparedDocumentRef | None = None,
) -> None:
    """把 Trace 中已经可定位的资源立即写入跨进程租约。

    上传阶段失败时可能还没有完整 ``PreparedDocumentRef``，但生命周期中的上传位置仍可
    用于人工巡检。空字段不会覆盖租约中此前已经记录的更完整引用。
    """
    external_location = ""
    if prepared_document is not None:
        external_location = prepared_document.external_location
    else:
        for event in reversed(trace.lifecycle_events):
            if event.operation == "document_upload" and event.external_ref:
                external_location = event.external_ref
                break
    task_service.rag_resource_leases.record_resources(
        execution_id=execution_id,
        context_ref=trace.context_ref or "",
        conversation_ref=trace.conversation_ref or "",
        document_ref=(prepared_document.document_ref if prepared_document else ""),
        external_location=external_location,
    )


def _submit_callback(
    *,
    task_service: LLMTaskService,
    file_name: str,
    execution_id: str,
    original_name: str,
    callback_url: str,
    callback_timeout: float,
    callback_payload: Dict[str, Any],
) -> None:
    """在业务终态落库后执行可选回调，并精确推进回调状态机。"""
    task_service.require_current_execution(
        "file",
        file_name,
        execution_id,
        allowed_statuses=("2", "3"),
    )
    if not callback_url:
        try:
            task_service.mark_callback_skipped(
                "file",
                file_name,
                execution_id=execution_id,
            )
        except Exception:
            logger.critical(
                "未配置回调地址，但无法将任务标记为无需回调: file_name=%s",
                file_name,
                exc_info=True,
            )
        return
    claim = task_service.claim_callback_delivery(
        "file",
        file_name,
        timeout=callback_timeout,
        execution_id=execution_id,
    )
    if claim is None:
        logger.info(
            "文件分析回调已有发送租约，当前 worker 不重复提交: "
            "file_name=%s execution_id=%s",
            file_name,
            execution_id,
        )
        return
    callback_claim_id, _ = claim
    callback_context = {
        "businessType": "file",
        "fileName": file_name,
        "originalFileName": original_name,
    }
    try:
        succeeded = post_callback_payload(
            callback_url,
            callback_payload,
            timeout=callback_timeout,
            callback_context=callback_context,
        )
    except Exception as exc:  # 回调异常不能改写已经确定的业务成功或失败结果。
        callback_error = _safe_task_error(exc, fallback="callback failed")
        try:
            task_service.mark_callback_failed(
                "file",
                file_name,
                callback_error,
                execution_id=execution_id,
                claim_id=callback_claim_id,
            )
        except Exception:
            logger.critical(
                "文件分析回调发生异常后，无法将任务标记为回调失败: file_name=%s",
                file_name,
                exc_info=True,
            )
        logger.exception(
            "文件分析回调发生异常: file_name=%s error_type=%s",
            file_name,
            type(exc).__name__,
        )
        return
    try:
        if succeeded:
            task_service.mark_callback_success(
                "file",
                file_name,
                execution_id=execution_id,
                claim_id=callback_claim_id,
            )
            logger.info("文件分析回调提交成功: file_name=%s", file_name)
        else:
            task_service.mark_callback_failed(
                "file",
                file_name,
                "callback failed",
                execution_id=execution_id,
                claim_id=callback_claim_id,
            )
            logger.warning("文件分析回调提交失败: file_name=%s", file_name)
    except Exception:
        logger.critical(
            "文件分析回调已执行但结果状态无法提交: file_name=%s callback_succeeded=%s",
            file_name,
            succeeded,
            exc_info=True,
        )


def _finalize_file_failure(
    *,
    task_service: LLMTaskService,
    progress_hub: LLMProgressHub,
    file_name: str,
    execution_id: str,
    original_name: str,
    stage: str,
    error_message: str,
    callback_url: str,
    callback_timeout: float,
) -> None:
    """以失败语义终结任务；任务库不可写时禁止绕过状态落库发送外部回调。"""
    callback_payload = build_file_callback_payload(file_name, {}, status="3")
    try:
        task_service.mark_business_result(
            "file",
            file_name,
            callback_payload,
            status="3",
            message=f"解析失败（{stage}）：{error_message}",
            execution_id=execution_id,
        )
        _publish_progress(progress_hub, file_name, 1.0)
    except (TaskExecutionConflictError, TaskStateConflictError):
        logger.warning(
            "文件分析失败终结被CAS拒绝，停止进度与回调: "
            "file_name=%s execution_id=%s stage=%s",
            file_name,
            execution_id,
            stage,
        )
        return
    except Exception:  # SQLite 整体不可写时，回调也不能对外宣称已有可追踪的业务终态。
        logger.critical(
            "文件分析失败状态无法持久化，停止回调: file_name=%s stage=%s",
            file_name,
            stage,
            exc_info=True,
        )
        return
    try:
        _submit_callback(
            task_service=task_service,
            file_name=file_name,
            execution_id=execution_id,
            original_name=original_name,
            callback_url=callback_url,
            callback_timeout=callback_timeout,
            callback_payload=callback_payload,
        )
    except Exception:
        # 回调状态落库失败不能阻止审计成功后的资源补偿。调用方会继续执行 close，运维
        # 可以根据任务终态和 critical 日志重放回调状态修复。
        logger.critical(
            "文件分析失败回调状态无法提交: file_name=%s stage=%s",
            file_name,
            stage,
            exc_info=True,
        )


def _close_audited_session(
        *,
        task_service: LLMTaskService,
        session: DocumentRagSession,
        interaction_id: int,
        execution_id: str,
        audited_trace: RagExecutionTrace,
        retain_document: bool,
) -> None:
    """关闭已审计 Session，并原子追加关闭事件与 cleanup 结果。

    外部关闭已经发生但追加审计失败时，业务结果不回滚；资源租约保持 ``audited``，使巡检
    能发现“外部可能已关闭、关闭证据尚未提交”的异常，而不是错误标记为完全 closed。
    """
    try:
        cleanup = session.close(retain_document=retain_document)
    except Exception:
        logger.critical(
            "RAG 会话关闭调用发生异常: interaction_id=%s execution_id=%s "
            "retain_document=%s",
            interaction_id,
            execution_id,
            retain_document,
            exc_info=True,
        )
        return
    closed_trace = session.trace
    initial_event_count = len(audited_trace.lifecycle_events)
    close_events = closed_trace.lifecycle_events[initial_event_count:]
    cleanup_status = "deleted" if cleanup.success else "failed"
    cleanup_error = "" if cleanup.success else cleanup.error_message
    try:
        if not close_events:
            raise RuntimeError("RAG Session 关闭后未生成生命周期事件")
        task_service.append_llm_interaction_lifecycle_events(
            interaction_id,
            close_events,
            cleanup_status=cleanup_status,
            cleanup_error=cleanup_error,
        )
    except Exception:
        logger.critical(
            "RAG 会话已关闭，但无法追加关闭审计: interaction_id=%s execution_id=%s",
            interaction_id,
            execution_id,
            exc_info=True,
        )
        return
    try:
        if cleanup.success:
            task_service.rag_resource_leases.mark_closed(
                execution_id=execution_id,
            )
        else:
            task_service.rag_resource_leases.record_cleanup_failure(
                execution_id=execution_id,
                error_message=cleanup_error,
            )
    except Exception:
        logger.critical(
            "RAG 会话关闭后，无法结束资源租约: interaction_id=%s execution_id=%s",
            interaction_id,
            execution_id,
            exc_info=True,
        )


def _store_prepared_analysis_document(
        *,
        knowledge_index_factory: KnowledgeIndexFactory,
        execution_id: str,
        file_name: str,
        original_name: str,
        mapped_result: Dict[str, Any],
        architecture_list: Iterable[Dict[str, Any]],
        prepared_document: PreparedDocumentRef,
) -> None:
    """把 RAG 已上传的同一文档转交永久知识库，不读取源文件也不二次上传。"""
    logger.info(
        "开始将已上传文档转交永久知识库: file_name=%s execution_id=%s",
        file_name,
        execution_id,
    )
    result_architecture_id = int(mapped_result["architectureId"])
    logger.info(
        "文件分析结果已确定分类: execution_id=%s result_architecture_id=%s",
        execution_id,
        result_architecture_id,
    )
    storage_architecture_id = resolve_storage_architecture_id(
        result_architecture_id,
        architecture_list,
    )
    logger.info(
        "永久知识库存储分类已确定: execution_id=%s storage_architecture_id=%s",
        execution_id,
        storage_architecture_id,
    )
    if storage_architecture_id is None or storage_architecture_id < 1:
        raise AnalysisContractError("无法确定永久知识库存储分类")
    if storage_architecture_id != result_architecture_id:
        logger.info(
            "文件知识库存储分类归并: file_name=%s result_architecture_id=%s "
            "result_architecture_name=%s storage_architecture_id=%s storage_architecture_name=%s",
            file_name,
            result_architecture_id,
            _architecture_name_by_id(result_architecture_id, architecture_list),
            storage_architecture_id,
            _architecture_name_by_id(storage_architecture_id, architecture_list),
        )
    attributes = {
        key: mapped_result.get(key, "")
        for key in ("country", "channel", "maturity", "security", "format")
    }
    metadata = KnowledgeDocumentMetadata(
        file_name=file_name,
        original_name=original_name,
        # 此名称来自 RAG Gateway 实际提交给 AnythingLLM 的不可变上传副本，而不是
        # 下载文件或业务哈希名。MHTML/PDF、OCR/Markdown 等预处理后的来源映射必须以
        # 它为准，才能在 weaponry 回调中稳定回填业务原始名。
        ingested_file_name=prepared_document.ingested_file_name,
        attributes=attributes,
    )
    logger.info(
        "永久知识库文档元数据已构建: file_name=%s has_ingested_file_name=%s "
        "attribute_key_count=%d",
        file_name,
        bool(metadata.ingested_file_name),
        len(attributes),
    )
    operation_context = KnowledgeOperationContext(
        execution_id=execution_id,
        business_type="file",
        business_key=file_name,
    )
    idempotency_key = build_document_idempotency_key(
        file_name=file_name,
        architecture_id=storage_architecture_id,
        content_sha256=prepared_document.content_sha256,
    )
    logger.info(
        "永久知识库写入幂等键已生成: execution_id=%s key_length=%d",
        execution_id,
        len(idempotency_key),
    )
    try:
        logger.debug("开始创建永久知识库任务对象: execution_id=%s", execution_id)
        with knowledge_index_factory.create() as knowledge_index:
            logger.debug("永久知识库任务对象创建完成: execution_id=%s", execution_id)
            collection = knowledge_index.ensure_collection(
                CollectionSpec(
                    architecture_id=storage_architecture_id,
                    name=f"architectureId-{storage_architecture_id}",
                )
            )
            logger.info(
                "永久知识集合已确认: execution_id=%s architecture_id=%s",
                execution_id,
                collection.architecture_id,
            )
            logger.debug("开始写入永久知识库文档: execution_id=%s", execution_id)
            knowledge_index.store_prepared_document(
                collection,
                prepared_document,
                metadata,
                operation_context=operation_context,
                idempotency_key=idempotency_key,
            )
            logger.debug("永久知识库文档写入调用完成: execution_id=%s", execution_id)
        logger.debug("永久知识库任务对象已正常关闭: execution_id=%s", execution_id)
    except Exception as exc:
        logger.exception(
            "写入永久知识库时发生异常: file_name=%s execution_id=%s error_type=%s",
            file_name,
            execution_id,
            type(exc).__name__,
        )
        raise
    logger.info(
        "文件分析文档所有权已转交永久知识库: file_name=%s execution_id=%s "
        "architecture_id=%s storage_architecture_id=%s",
        file_name,
        execution_id,
        result_architecture_id,
        storage_architecture_id,
    )


def _execute_file_analysis_task(
    *,
    task_service: LLMTaskService,
    progress_hub: LLMProgressHub,
    request_payload: Dict[str, Any],
    download_root: str,
    callback_url: str,
    callback_timeout: float,
    document_rag_factory: DocumentRagFactory,
    knowledge_index_factory: KnowledgeIndexFactory,
    execution_id: str,
    analysis_classification_mode: str = "topk_two_stage",
    analysis_filename_constraint_mode: str = (
        ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY
    ),
    analysis_data_standard_mode: str = ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    analysis_identity_reselect_mode: str = ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
) -> None:
    """按审计硬前置契约执行单文件分析和永久知识库转交。

    关键顺序固定为：准备文件 → 隔离 RAG → 领域契约 → 原子审计 → 永久知识库 → 翻译与
    业务结果 → 回调 → 审计化关闭。任何审计失败都保留外部现场且绝不调用 ``close``；审计
    成功后的失败则按永久知识库是否已经接管文档决定删除或保留全局实体。
    """
    if not isinstance(document_rag_factory, DocumentRagFactory):
        raise TypeError("document_rag_factory 必须实现 DocumentRagFactory")
    if not isinstance(knowledge_index_factory, KnowledgeIndexFactory):
        raise TypeError("knowledge_index_factory 必须实现 KnowledgeIndexFactory")
    classification_mode = _normalize_analysis_classification_mode(
        analysis_classification_mode
    )
    filename_constraint_mode = _normalize_analysis_filename_constraint_mode(
        analysis_filename_constraint_mode
    )
    data_standard_mode = _normalize_analysis_data_standard_mode(
        analysis_data_standard_mode
    )
    identity_reselect_mode = _normalize_analysis_identity_reselect_mode(
        analysis_identity_reselect_mode
    )
    # 三种运行模式都必须先持久化模型可见候选，审计故障时禁止创建远端 Session。
    # legacy 仍发送完整小树，但同样受全局 128 候选与 32K Prompt 硬门禁约束。
    recall_audit_enabled = True
    params = request_payload["params"][0]
    file_name = _as_text(params.get("fileName"))
    requested_original_name = _as_business_original_file_name(
        params.get("originalFileName"),
    )
    original_name = requested_original_name or file_name
    if not requested_original_name:
        # 不改变既有接口的可选参数约束，但明确记录此类请求无法提供业务原始名；后续
        # weaponry 只能稳定回填哈希名，绝不能把预处理生成的文件名伪装成业务原始名。
        logger.warning(
            "文件分析请求缺少originalFileName，来源展示将回退为业务哈希名: file_name=%s",
            file_name,
        )
    file_path = _as_text(params.get("filePath"))
    execution_id = _as_text(execution_id)
    if not execution_id:
        raise ValueError("execution_id不能为空")
    task_service.require_current_execution(
        "file",
        file_name,
        execution_id,
        allowed_statuses=("0", "1"),
    )
    workflow_started_at = time.perf_counter()
    analysis_prompt = ""
    original_text = ""
    tree_index: ArchitectureTreeIndex | None = None
    recall_decision: ArchitectureRecallDecision | None = None
    recall_audit_fields: Dict[str, Any] | None = None
    recall_audit_finalized = False
    visible_candidates: tuple[Dict[str, Any], ...] = ()
    visible_ids: set[int] = set()
    resolved_direct_architecture_id: int | None = None
    data_standard_profile = _DataStandardClassificationProfile()
    data_standard_scope_guard_active = False
    data_standard_scope_ids: tuple[int, ...] = ()
    data_standard_remark_overrides: dict[int, str] = {}
    jane_profile = _JaneClassificationProfile()
    equipment_identity_profile = _EquipmentIdentityReselectProfile()
    scope_resolution = _ArchitectureScopeResolution()
    constraint_decision: _ArchitectureConstraintDecision | None = None
    data_standard_general_fallback_applied = False

    def persist_initial_recall_audit(fields: Dict[str, Any]) -> None:
        task_service.upsert_architecture_recall_decision(
            execution_id=execution_id,
            **fields,
        )

    def fail_before_remote_session(
            *,
            stage: str,
            error: BaseException,
            fields: Dict[str, Any],
    ) -> None:
        nonlocal recall_audit_finalized
        error_message = _safe_task_error(error, fallback="领域分类前置处理失败")
        try:
            persist_initial_recall_audit(fields)
            task_service.finalize_architecture_recall_decision(
                execution_id=execution_id,
                returned_architecture_id=None,
                returned_rank=None,
                total_elapsed_ms=_elapsed_ms(
                    workflow_started_at,
                    floor=int(fields["recall_elapsed_ms"]),
                ),
                failure_stage=stage,
                error_message=error_message,
            )
            recall_audit_finalized = True
        except Exception as audit_exc:
            error_message = _safe_task_error(
                audit_exc,
                fallback="领域召回审计失败",
            )
            logger.exception(
                "领域召回审计失败，禁止创建远端 Session: "
                "file_name=%s execution_id=%s stage=%s",
                file_name,
                execution_id,
                stage,
            )
        _finalize_file_failure(
            task_service=task_service,
            progress_hub=progress_hub,
            file_name=file_name,
            execution_id=execution_id,
            original_name=original_name,
            stage=stage,
            error_message=error_message,
            callback_url=callback_url,
            callback_timeout=callback_timeout,
        )

    architecture_index_started_at = time.perf_counter()
    try:
        ranges = build_effective_analysis_ranges(params)
        tree_index = validate_analysis_architecture_ranges(params)
    except ArchitectureTreeValidationError as exc:
        fields = {
            "tree_fingerprint": "",
            "query_digest": hashlib.sha256(b"").hexdigest(),
            "base_top64": [],
            "final_candidates": [],
            "channel_rankings": {},
            "rrf_scores": {},
            "protected_reasons": {},
            "prompt_chars": 0,
            "recall_elapsed_ms": _elapsed_ms(
                architecture_index_started_at
            ),
        }
        fail_before_remote_session(
            stage="architecture_index",
            error=exc,
            fields=fields,
        )
        return
    architecture_index_elapsed_seconds = (
        time.perf_counter() - architecture_index_started_at
    )

    logger.info(
        "开始执行文件分析任务: file_name=%s execution_id=%s",
        file_name,
        execution_id,
    )
    try:
        task_service.update_task_progress(
            "file",
            file_name,
            progress=0.15,
            message="正在下载文件",
            status="1",
            execution_id=execution_id,
        )
        _publish_progress(progress_hub, file_name, 0.15)
        downloaded_path = download_to_temp_file(
            file_path,
            file_name,
            download_root,
            timeout=60,
        )
        task_service.update_task_progress(
            "file",
            file_name,
            progress=0.35,
            message="正在执行文档解析",
            execution_id=execution_id,
        )
        _publish_progress(progress_hub, file_name, 0.35)

        llm_file_path = downloaded_path
        try:
            llm_file_path = normalize_file_for_llm(downloaded_path)
        except Exception as exc:  # 归一化是增强能力，原文件仍是合法的降级输入。
            logger.warning(
                "MHTML 归一化失败，降级使用原文件: file_name=%s error_type=%s",
                file_name,
                type(exc).__name__,
            )
        llm_file_path = _prepare_analysis_upload_file(llm_file_path)
        # 正文只读取一次，并在任何 Factory/Session 创建前同时提供给召回和 mapper。
        original_text = _read_original_text(llm_file_path)
    except Exception as exc:
        error_message = _safe_task_error(exc, fallback="文件预处理失败")
        logger.exception(
            "文件分析预处理失败: file_name=%s execution_id=%s",
            file_name,
            execution_id,
        )
        _finalize_file_failure(
            task_service=task_service,
            progress_hub=progress_hub,
            file_name=file_name,
            execution_id=execution_id,
            original_name=original_name,
            stage="preparation",
            error_message=error_message,
            callback_url=callback_url,
            callback_timeout=callback_timeout,
        )
        return

    architecture_list = ranges["architectureList"]
    data_standard_profile = _build_data_standard_classification_profile(
        file_name=file_name,
        original_name=original_name,
        original_text=original_text,
    )
    data_standard_scope_guard_active = (
        data_standard_mode == ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD
        and classification_mode != "legacy"
        and data_standard_profile.identity_confirmed
        and data_standard_profile.document_kind == "standard_body"
    )
    jane_profile = _build_jane_classification_profile(
        file_name=file_name,
        original_name=original_name,
        original_text=original_text,
    )
    scope_guard_active = (
        filename_constraint_mode
        == ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
        and jane_profile.active
        and not data_standard_scope_guard_active
    )
    # 召回强证据收窄与 Jane 最终作用域约束是两个独立边界：普通非 Jane 文档在
    # scope_guard 模式下也只能让原文件名/可信标题参与 exact 与装备 family 规则，
    # 正文、章节和 Fleetlist 仍保留在 query_text 中参与 lexical/tree 召回；但这里
    # 不会激活下游 Jane 硬约束，最终分类仍由模型在可见候选内决定。
    recall_strong_evidence_only = (
        filename_constraint_mode
        == ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
        or data_standard_scope_guard_active
    )
    recall_file_name, recall_original_name = _jane_recall_filename_signals(
        file_name=file_name,
        original_name=original_name,
        profile=jane_profile,
        scope_guard_active=scope_guard_active,
    )
    signals = _build_analysis_architecture_signals(
        file_name=recall_file_name,
        original_name=recall_original_name,
        original_text=original_text,
        title_override=(
            data_standard_profile.title
            if data_standard_scope_guard_active
            else jane_profile.title
            if scope_guard_active
            else ""
        ),
    )
    signal_digest = _architecture_signal_digest(signals)
    # 领域树已在下载前完成索引。将其实际耗时折入既有 recall_elapsed_ms，同时排除
    # 中间的下载与正文读取耗时，保持审计指标原有语义。
    index_started_at = (
        time.perf_counter() - architecture_index_elapsed_seconds
    )

    if scope_guard_active:
        scope_resolution = _resolve_jane_architecture_scope(
            jane_profile,
            original_text=original_text,
            tree_index=tree_index,
        )

    try:
        if data_standard_scope_guard_active:
            (
                data_standard_scope_ids,
                data_standard_remark_overrides,
            ) = _data_standard_candidate_scope(
                tree_index=tree_index,
                architecture_list=architecture_list,
            )
            if not data_standard_scope_ids:
                raise ArchitectureRecallError(
                    "已确认标准正文，但 architectureList 中没有可用的数据标准叶节点"
                )
        if classification_mode == "legacy":
            analysis_prompt = normalize_rag_prompt(build_file_analysis_prompt(params))
            if (
                    len(tree_index.nodes) > 128
                    or len(analysis_prompt) > MAX_ANALYSIS_PROMPT_CHARS
            ):
                raise ArchitecturePromptBudgetError(
                    "legacy 完整领域树候选必须不超过 128 个且 Prompt "
                    "必须不超过 32000 字符"
                )
            visible_candidates = tuple(
                _node_prompt_projection(node) for node in tree_index.nodes
            )
            visible_ids = {node.id for node in tree_index.nodes}
            recall_audit_fields = _direct_recall_audit_fields(
                tree_index=tree_index,
                signals=signals,
                candidates=tree_index.nodes,
                prompt_chars=len(analysis_prompt),
                recall_elapsed_ms=_elapsed_ms(index_started_at),
                channel_name="legacy",
            )
        else:
            if len(tree_index.nodes) == 1:
                direct_node = tree_index.nodes[0]
                visible_candidates = (_node_prompt_projection(direct_node),)
                visible_ids = {direct_node.id}
                resolved_direct_architecture_id = direct_node.id
                _validate_data_standard_leaf_requirement(
                    direct_node.id,
                    architecture_list,
                )
            else:
                # 召回服务先以宽松估算上限返回实际候选；真实 Prompt 随后执行 32K 硬门禁。
                recall_decision = recall_architecture_candidates(
                    tree_index,
                    signals,
                    prompt_char_limit=2_000_000,
                    prompt_overhead_chars=0,
                    strong_evidence_only=recall_strong_evidence_only,
                    strong_identity_enabled=(
                        jane_profile.recall_identity_enabled
                        if scope_guard_active
                        else True
                    ),
                    # Jane 标题+正文类型别名是既有的双源特例，不能因普通非 Jane
                    # 文档启用召回强证据收窄而被意外激活。
                    jane_title_type_alias_enabled=scope_guard_active,
                    preferred_parent_reasons=(
                        scope_resolution.preferred_parent_reasons
                        if scope_guard_active
                        else None
                    ),
                    candidate_scope_ids=(
                        data_standard_scope_ids
                        if data_standard_scope_guard_active
                        else None
                    ),
                    candidate_scope_reason=(
                        "data-standard-scope"
                        if data_standard_scope_guard_active
                        else ""
                    ),
                    candidate_remark_overrides=(
                        data_standard_remark_overrides
                        if data_standard_scope_guard_active
                        else None
                    ),
                )
                visible_candidates = recall_decision.prompt_candidates
                visible_ids = set(recall_decision.final_candidate_ids)
                if len(visible_candidates) == 1:
                    resolved_direct_architecture_id = _validate_topk_architecture_id(
                        visible_candidates[0]["id"],
                        visible_ids=visible_ids,
                        tree_index=tree_index,
                        architecture_list=architecture_list,
                    )

            if resolved_direct_architecture_id is not None:
                direct_node = tree_index.require(resolved_direct_architecture_id)
                include_standard_fields = _is_architecture_in_standard_range(
                    resolved_direct_architecture_id,
                    architecture_list,
                    ranges["architectureStandardList"],
                )
                analysis_prompt = normalize_rag_prompt(
                    build_file_extraction_prompt(
                        params,
                        resolved_architecture_id=resolved_direct_architecture_id,
                        resolved_architecture_path_name=direct_node.semantic_path,
                        resolved_architecture_path_node_names=(
                            _architecture_path_keyword_names(
                                tree_index,
                                resolved_direct_architecture_id,
                            )
                        ),
                        resolved_architecture_node_type=(
                            "leaf" if direct_node.is_leaf else "parent"
                        ),
                        include_data_standard_fields=include_standard_fields,
                    )
                )
            elif classification_mode == "topk_two_stage":
                analysis_prompt = normalize_rag_prompt(
                    (
                        build_data_standard_classification_prompt(
                            params,
                            visible_candidates,
                            standard_context=_data_standard_prompt_context(
                                data_standard_profile
                            ),
                        )
                        if data_standard_scope_guard_active
                        else build_architecture_classification_prompt(
                            params,
                            visible_candidates,
                            classification_context=(
                                _jane_classification_prompt_context(
                                    jane_profile,
                                    scope_resolution,
                                )
                                if scope_guard_active
                                else None
                            ),
                        )
                    )
                )
            else:
                limited_params = dict(params)
                limited_params["architectureList"] = list(visible_candidates)
                scope_contract = ""
                if data_standard_scope_guard_active:
                    scope_contract = (
                        "\n【数据标准作用域分类补充规则】\n"
                        "服务端已确认该文件是标准正文；只能在下方数据标准叶节点中分类。"
                        "专业类别必须由标准标题或范围支持；普通目录中的“术语和定义”不能"
                        "单独决定分类。不属于五个专业主题时选择“通用要求”。\n"
                        "服务端标准画像："
                        + json.dumps(
                            _data_standard_prompt_context(
                                data_standard_profile
                            ),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                elif scope_guard_active:
                    scope_contract = (
                        "\n【简氏作用域分类补充规则】\n"
                        "按全文主要对象和覆盖粒度分类；class 文档的首舰号只标识舰级，"
                        "Fleetlist 成员不能单独决定最终分类；Flight、Block、批次限定词"
                        "优先于基础型号；只有全文主要描述明细类别时才选择明细叶子。\n"
                        "服务端首页画像："
                        + json.dumps(
                            _jane_classification_prompt_context(
                                jane_profile,
                                scope_resolution,
                            ),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                analysis_prompt = normalize_rag_prompt(
                    build_file_analysis_prompt(limited_params)
                    + "\n【topk_single 受限候选补充合同】\n"
                    + "下方 JSON 是本次完整且唯一可选的模型候选，nodeType 必须保留语义。"
                    + "证据足以支持 leaf 时优先叶子；叶子证据不足但能可靠确定 parent 时，"
                    + "允许返回候选中最深的 parent。此规则替代上文只允许叶子的旧规则。\n"
                    + scope_contract
                    + json.dumps(
                        list(visible_candidates),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )

            if len(visible_candidates) > 128:
                raise ArchitecturePromptBudgetError("领域模型候选数量超过 128 个")
            if len(analysis_prompt) > MAX_ANALYSIS_PROMPT_CHARS:
                raise ArchitecturePromptBudgetError(
                    f"模型 Prompt 共 {len(analysis_prompt)} 字符，超过 32000 字符上限"
                )

            if recall_decision is not None:
                recall_audit_fields = _recall_audit_fields(
                    recall_decision,
                    prompt_chars=len(analysis_prompt),
                )
            else:
                direct_nodes = tuple(
                    tree_index.require(candidate["id"])
                    for candidate in visible_candidates
                )
                recall_audit_fields = _direct_recall_audit_fields(
                    tree_index=tree_index,
                    signals=signals,
                    candidates=direct_nodes,
                    prompt_chars=len(analysis_prompt),
                    recall_elapsed_ms=_elapsed_ms(index_started_at),
                    channel_name="direct",
                )
    except ArchitectureTreeValidationError as exc:
        fields = {
            "tree_fingerprint": tree_index.fingerprint,
            "query_digest": signal_digest,
            "base_top64": [],
            "final_candidates": [],
            "channel_rankings": {},
            "rrf_scores": {},
            "protected_reasons": {},
            "prompt_chars": 0,
            "recall_elapsed_ms": _elapsed_ms(index_started_at),
        }
        fail_before_remote_session(
            stage="architecture_index",
            error=exc,
            fields=fields,
        )
        return
    except ArchitecturePromptBudgetError as exc:
        if recall_decision is not None:
            fields = _recall_audit_fields(
                recall_decision,
                prompt_chars=len(analysis_prompt),
            )
        else:
            auditable_nodes = tree_index.nodes if len(tree_index.nodes) <= 128 else ()
            fields = _direct_recall_audit_fields(
                tree_index=tree_index,
                signals=signals,
                candidates=auditable_nodes,
                prompt_chars=len(analysis_prompt),
                recall_elapsed_ms=_elapsed_ms(index_started_at),
                channel_name="legacy" if classification_mode == "legacy" else "direct",
            )
        fail_before_remote_session(
            stage="architecture_prompt_budget",
            error=exc,
            fields=fields,
        )
        return
    except ArchitectureRecallError as exc:
        fields = {
            "tree_fingerprint": tree_index.fingerprint,
            "query_digest": signal_digest,
            "base_top64": [],
            "final_candidates": [],
            "channel_rankings": {},
            "rrf_scores": {},
            "protected_reasons": {},
            "prompt_chars": 0,
            "recall_elapsed_ms": _elapsed_ms(index_started_at),
        }
        fail_before_remote_session(
            stage="architecture_recall",
            error=exc,
            fields=fields,
        )
        return
    except ArchitectureContractError as exc:
        direct_nodes = tuple(
            tree_index.require(candidate["id"])
            for candidate in visible_candidates
        )
        fields = _direct_recall_audit_fields(
            tree_index=tree_index,
            signals=signals,
            candidates=direct_nodes,
            prompt_chars=len(analysis_prompt),
            recall_elapsed_ms=_elapsed_ms(index_started_at),
            channel_name="direct",
        )
        fail_before_remote_session(
            stage="architecture_contract",
            error=exc,
            fields=fields,
        )
        return

    if (
        identity_reselect_mode != ANALYSIS_IDENTITY_RESELECT_MODE_OFF
        and classification_mode == "topk_two_stage"
        and filename_constraint_mode
        == ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
        and resolved_direct_architecture_id is None
    ):
        try:
            equipment_identity_profile = (
                _build_equipment_identity_reselect_profile(
                    requested_original_name=requested_original_name,
                    original_text=original_text,
                    tree_index=tree_index,
                    visible_ids=visible_ids,
                    jane_active=jane_profile.active,
                    data_standard_active=data_standard_scope_guard_active,
                )
            )
        except Exception:
            equipment_identity_profile = _EquipmentIdentityReselectProfile(
                reason_code="identity_profile_error"
            )
            logger.exception(
                "装备双证据身份画像失败，保留普通分类链路: "
                "file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
    logger.info(
        "装备身份受限重选门禁已评估: execution_id=%s mode=%s active=%s "
        "reason=%s identifier=%s target_parent_id=%s candidate_count=%d",
        execution_id,
        identity_reselect_mode,
        equipment_identity_profile.active,
        equipment_identity_profile.reason_code,
        equipment_identity_profile.identifier,
        equipment_identity_profile.target_parent_id,
        len(equipment_identity_profile.candidate_ids),
    )

    if recall_audit_enabled and recall_audit_fields is None:
        raise RuntimeError("领域召回未生成可审计决策")
    if recall_audit_enabled:
        try:
            # 该写入是远端 Session 创建的硬前置；失败时下面的 Factory 代码不会执行。
            persist_initial_recall_audit(recall_audit_fields)
        except Exception as exc:
            error_message = _safe_task_error(exc, fallback="领域召回审计失败")
            logger.exception(
                "领域召回审计失败，禁止创建远端 Session: "
                "file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                execution_id=execution_id,
                original_name=original_name,
                stage="architecture_recall",
                error_message=error_message,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
            return

    task_service.require_current_execution(
        "file",
        file_name,
        execution_id,
        allowed_statuses=("0", "1"),
    )
    with document_rag_factory.create() as document_rag:
        try:
            task_service.rag_resource_leases.begin(
                execution_id=execution_id,
                business_type="file",
                business_key=file_name,
            )
        except Exception as exc:
            # Factory 进入只创建本地 HTTP 对象图，不创建远端资源。租约登记仍严格发生在
            # open_isolated_session 之前；登记失败时立即退出租约，不会产生无法追踪的资源。
            lease_error = _safe_task_error(exc, fallback="RAG 资源租约登记失败")
            logger.exception(
                "RAG 资源租约登记失败，未创建外部资源: file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
            if recall_audit_enabled:
                try:
                    task_service.finalize_architecture_recall_decision(
                        execution_id=execution_id,
                        returned_architecture_id=None,
                        returned_rank=None,
                        total_elapsed_ms=_elapsed_ms(
                            workflow_started_at,
                            floor=int(recall_audit_fields["recall_elapsed_ms"]),
                        ),
                        failure_stage="architecture_contract",
                        error_message=lease_error,
                    )
                    recall_audit_finalized = True
                except Exception:
                    logger.critical(
                        "资源租约失败后无法终结召回审计: execution_id=%s",
                        execution_id,
                        exc_info=True,
                    )
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                execution_id=execution_id,
                original_name=original_name,
                stage="resource_lease",
                error_message=lease_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
            return
        session: DocumentRagSession | None = None
        prepared_document: PreparedDocumentRef | None = None
        final_prompt = analysis_prompt
        workflow_failure_stage = "architecture_contract"
        try:
            session = document_rag.open_isolated_session(
                context_name=f"llm-file-{execution_id}",
                conversation_name=f"analysis-{Path(file_name).stem}",
            )
            _record_lease_resources(
                task_service,
                execution_id,
                session.trace,
            )
            if (
                    classification_mode == "topk_two_stage"
                    and resolved_direct_architecture_id is None
            ):
                classification_started = len(session.trace.attempts)
                rag_result = session.analyse(
                    llm_file_path,
                    analysis_prompt,
                    prompt_kind=RagPromptKind.ARCHITECTURE_CLASSIFICATION,
                    require_sources=True,
                    max_attempts=MAX_ANALYSIS_PHASE_CALLS,
                )
                prepared_document = rag_result.prepared_document
                _record_lease_resources(
                    task_service,
                    execution_id,
                    rag_result.trace,
                    prepared_document,
                )
                parsed_classification = _parse_strict_json_object(rag_result.text)
                architecture_id: int | None = None
                try:
                    parsed_classification, architecture_id = (
                        _parse_topk_classification_result(
                            rag_result.text,
                            visible_ids=visible_ids,
                            tree_index=tree_index,
                            architecture_list=architecture_list,
                        )
                    )
                except ArchitectureContractError as contract_error:
                    force_standard = isinstance(
                        contract_error,
                        DataStandardParentContractError,
                    )
                    architecture_id = (
                        None
                        if data_standard_scope_guard_active
                        else _visible_data_standard_fallback_id(
                            visible_ids=visible_ids,
                            architecture_list=architecture_list,
                            force=force_standard,
                            context_values=(original_text, original_name),
                        )
                    )
                    if architecture_id is None:
                        attempts_used = _phase_attempt_count(
                            session,
                            classification_started,
                        )
                        if attempts_used >= MAX_ANALYSIS_PHASE_CALLS:
                            if not data_standard_scope_guard_active:
                                raise ArchitectureContractError(
                                    "分类阶段实际模型调用预算已耗尽，无法 repair"
                                ) from contract_error
                            architecture_id = (
                                _visible_data_standard_fallback_id(
                                    visible_ids=visible_ids,
                                    architecture_list=architecture_list,
                                    force=True,
                                    context_values=(
                                        original_text,
                                        original_name,
                                    ),
                                )
                            )
                            if architecture_id is None:
                                raise ArchitectureContractError(
                                    "标准正文分类预算耗尽，且候选中不存在通用要求叶节点"
                                ) from contract_error
                            data_standard_general_fallback_applied = True
                        if architecture_id is None:
                            final_prompt = _normalize_bounded_analysis_prompt(
                                build_architecture_repair_prompt(
                                    parsed_classification
                                    or {"architectureId": None},
                                    visible_candidates,
                                    str(contract_error),
                                )
                            )
                            repaired_result = session.ask(
                                final_prompt,
                                prompt_kind=RagPromptKind.ARCHITECTURE_REPAIR,
                                require_sources=True,
                                max_attempts=(
                                    MAX_ANALYSIS_PHASE_CALLS - attempts_used
                                ),
                            )
                            try:
                                _repaired, architecture_id = (
                                    _parse_topk_classification_result(
                                        repaired_result.text,
                                        visible_ids=visible_ids,
                                        tree_index=tree_index,
                                        architecture_list=architecture_list,
                                    )
                                )
                            except ArchitectureContractError as repair_error:
                                architecture_id = (
                                    _visible_data_standard_fallback_id(
                                        visible_ids=visible_ids,
                                        architecture_list=architecture_list,
                                        force=True,
                                        context_values=(
                                            original_text,
                                            original_name,
                                        ),
                                    )
                                    if data_standard_scope_guard_active
                                    else None
                                )
                                if architecture_id is None:
                                    raise ArchitectureContractError(
                                        "标准正文分类 repair 后仍无法确定类别，且候选中"
                                        "不存在通用要求叶节点"
                                    ) from repair_error
                                data_standard_general_fallback_applied = True

                if architecture_id is None:
                    raise ArchitectureContractError("无法确定领域分类")
                initial_architecture_id = architecture_id
                identity_gate_decision = _decide_identity_reselect_gate(
                    architecture_id,
                    profile=equipment_identity_profile,
                    tree_index=tree_index,
                )
                identity_reselect_architecture_id: int | None = None
                identity_reselect_outcome = identity_gate_decision.reason_code
                classification_attempts_used = _phase_attempt_count(
                    session,
                    classification_started,
                )
                if identity_gate_decision.should_reselect:
                    if classification_attempts_used != 1:
                        identity_reselect_outcome = "skip_call_budget"
                    else:
                        try:
                            scoped_candidates = tuple(
                                _node_prompt_projection(tree_index.require(node_id))
                                for node_id in equipment_identity_profile.candidate_ids
                            )
                            reselect_prompt = _normalize_bounded_analysis_prompt(
                                build_architecture_reselect_prompt(
                                    {"architectureId": initial_architecture_id},
                                    {
                                        "identifier": (
                                            equipment_identity_profile.identifier
                                        ),
                                        "matchedParentId": (
                                            equipment_identity_profile.target_parent_id
                                        ),
                                        "matchedParentPath": (
                                            equipment_identity_profile.target_parent_path
                                        ),
                                        "evidenceSources": list(
                                            equipment_identity_profile.evidence_sources
                                        ),
                                    },
                                    scoped_candidates,
                                )
                            )
                        except Exception:
                            identity_reselect_outcome = "prompt_build_failed"
                            logger.exception(
                                "装备身份受限重选 Prompt 构造失败，保留初次分类: "
                                "file_name=%s execution_id=%s",
                                file_name,
                                execution_id,
                            )
                        else:
                            reselect_conversation_ready = (
                                session.start_fresh_conversation(
                                    conversation_name=(
                                        "analysis-identity-reselect-"
                                        f"{Path(file_name).stem}"
                                    ),
                                    failure_is_fatal=False,
                                )
                            )
                            _record_lease_resources(
                                task_service,
                                execution_id,
                                session.trace,
                                prepared_document,
                            )
                            if not reselect_conversation_ready:
                                identity_reselect_outcome = (
                                    "conversation_unavailable_keep_initial"
                                )
                            else:
                                final_prompt = reselect_prompt
                                reselect_result = session.ask_optional(
                                    final_prompt,
                                    prompt_kind=(
                                        RagPromptKind.ARCHITECTURE_RESELECT
                                    ),
                                    require_sources=True,
                                    max_attempts=1,
                                )
                                _record_lease_resources(
                                    task_service,
                                    execution_id,
                                    session.trace,
                                    prepared_document,
                                )
                                if reselect_result is None:
                                    identity_reselect_outcome = (
                                        "query_failed_keep_initial"
                                    )
                                else:
                                    try:
                                        identity_reselect_architecture_id = (
                                            _parse_architecture_reselect_result(
                                                reselect_result.text,
                                                scoped_ids=set(
                                                    equipment_identity_profile.candidate_ids
                                                ),
                                                tree_index=tree_index,
                                                architecture_list=architecture_list,
                                            )
                                        )
                                    except ArchitectureContractError:
                                        identity_reselect_outcome = (
                                            "invalid_result_keep_initial"
                                        )
                                        logger.warning(
                                            "装备身份受限重选结果不合法，保留初次分类: "
                                            "file_name=%s execution_id=%s",
                                            file_name,
                                            execution_id,
                                        )
                                    else:
                                        if identity_reselect_architecture_id is None:
                                            identity_reselect_outcome = (
                                                "null_result_keep_initial"
                                            )
                                        elif (
                                            identity_reselect_mode
                                            == ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE
                                        ):
                                            architecture_id = (
                                                identity_reselect_architecture_id
                                            )
                                            identity_reselect_outcome = (
                                                "enforce_applied"
                                            )
                                        else:
                                            identity_reselect_outcome = (
                                                "shadow_kept_initial"
                                            )
                logger.info(
                    "装备身份受限重选完成: execution_id=%s mode=%s relation=%s "
                    "gate_reason=%s initial_architecture_id=%s "
                    "reselect_architecture_id=%s pre_constraint_architecture_id=%s "
                    "classification_attempts=%d outcome=%s",
                    execution_id,
                    identity_reselect_mode,
                    identity_gate_decision.relation,
                    identity_gate_decision.reason_code,
                    initial_architecture_id,
                    identity_reselect_architecture_id,
                    architecture_id,
                    classification_attempts_used,
                    identity_reselect_outcome,
                )
                constraint_decision = (
                    _decide_topk_deterministic_architecture_constraint(
                        architecture_id,
                        file_name=file_name,
                        original_name=original_name,
                        visible_ids=visible_ids,
                        tree_index=tree_index,
                        architecture_list=architecture_list,
                        filename_constraint_mode=filename_constraint_mode,
                        data_standard_mode=data_standard_mode,
                        data_standard_profile=data_standard_profile,
                        jane_profile=jane_profile,
                        scope_resolution=scope_resolution,
                    )
                )
                if data_standard_general_fallback_applied:
                    constraint_decision = _ArchitectureConstraintDecision(
                        pre_architecture_id=architecture_id,
                        post_architecture_id=architecture_id,
                        reason_code="data_standard_general_fallback",
                        matched_scope_parent_id=None,
                        tree_gap=False,
                    )
                architecture_id = constraint_decision.post_architecture_id
                selected_node = tree_index.require(architecture_id)
                include_standard_fields = _is_architecture_in_standard_range(
                    architecture_id,
                    architecture_list,
                    ranges["architectureStandardList"],
                )
                extraction_prompt = _normalize_bounded_analysis_prompt(
                    build_file_extraction_prompt(
                        params,
                        resolved_architecture_id=architecture_id,
                        resolved_architecture_path_name=selected_node.semantic_path,
                        resolved_architecture_path_node_names=(
                            _architecture_path_keyword_names(
                                tree_index,
                                architecture_id,
                            )
                        ),
                        resolved_architecture_node_type=(
                            "leaf" if selected_node.is_leaf else "parent"
                        ),
                        include_data_standard_fields=include_standard_fields,
                    )
                )
                workflow_failure_stage = "analysis_extraction"
                session.start_fresh_conversation(
                    conversation_name=(
                        f"analysis-extraction-{Path(file_name).stem}"
                    ),
                )
                _record_lease_resources(
                    task_service,
                    execution_id,
                    session.trace,
                    prepared_document,
                )
                # 只有第二线程已经创建成功，抽取 Prompt 才成为审计中的最后实际请求。
                # 创建失败时 final_prompt 继续指向分类或分类 repair Prompt。
                final_prompt = extraction_prompt
                extraction_started = len(session.trace.attempts)
                extraction_result = session.ask(
                    final_prompt,
                    prompt_kind=RagPromptKind.ANALYSIS_EXTRACTION,
                    require_sources=True,
                    max_attempts=MAX_ANALYSIS_PHASE_CALLS,
                )
                parsed_result = _parse_strict_json_object(extraction_result.text)
                if parsed_result is None:
                    attempts_used = _phase_attempt_count(session, extraction_started)
                    if attempts_used >= MAX_ANALYSIS_PHASE_CALLS:
                        raise AnalysisContractError(
                            "字段抽取阶段实际模型调用预算已耗尽，无法 JSON repair"
                        )
                    final_prompt = _normalize_bounded_analysis_prompt(
                        build_json_repair_prompt(extraction_result.text)
                    )
                    repaired_result = session.ask(
                        final_prompt,
                        prompt_kind=RagPromptKind.JSON_REPAIR,
                        require_sources=True,
                        max_attempts=MAX_ANALYSIS_PHASE_CALLS - attempts_used,
                    )
                    parsed_result = _parse_strict_json_object(repaired_result.text)
                    if parsed_result is None:
                        raise AnalysisContractError(
                            "JSON 修复后仍不是严格 JSON 对象"
                        )
            elif resolved_direct_architecture_id is not None:
                architecture_id = resolved_direct_architecture_id
                workflow_failure_stage = "analysis_extraction"
                extraction_started = len(session.trace.attempts)
                rag_result = session.analyse(
                    llm_file_path,
                    analysis_prompt,
                    prompt_kind=RagPromptKind.ANALYSIS_EXTRACTION,
                    require_sources=True,
                    max_attempts=MAX_ANALYSIS_PHASE_CALLS,
                )
                prepared_document = rag_result.prepared_document
                _record_lease_resources(
                    task_service,
                    execution_id,
                    rag_result.trace,
                    prepared_document,
                )
                parsed_result = _parse_strict_json_object(rag_result.text)
                if parsed_result is None:
                    attempts_used = _phase_attempt_count(session, extraction_started)
                    if attempts_used >= MAX_ANALYSIS_PHASE_CALLS:
                        raise AnalysisContractError(
                            "字段抽取阶段实际模型调用预算已耗尽，无法 JSON repair"
                        )
                    final_prompt = _normalize_bounded_analysis_prompt(
                        build_json_repair_prompt(rag_result.text)
                    )
                    repaired_result = session.ask(
                        final_prompt,
                        prompt_kind=RagPromptKind.JSON_REPAIR,
                        require_sources=True,
                        max_attempts=MAX_ANALYSIS_PHASE_CALLS - attempts_used,
                    )
                    parsed_result = _parse_strict_json_object(repaired_result.text)
                    if parsed_result is None:
                        raise AnalysisContractError(
                            "JSON 修复后仍不是严格 JSON 对象"
                        )
            else:
                workflow_failure_stage = "analysis_extraction"
                combined_started = len(session.trace.attempts)
                rag_result = session.analyse(
                    llm_file_path,
                    analysis_prompt,
                    prompt_kind=RagPromptKind.ANALYSIS,
                    require_sources=True,
                    max_attempts=MAX_ANALYSIS_PHASE_CALLS,
                )
                prepared_document = rag_result.prepared_document
                _record_lease_resources(
                    task_service,
                    execution_id,
                    rag_result.trace,
                    prepared_document,
                )
                parsed_result = _parse_strict_json_object(rag_result.text)
                if parsed_result is None:
                    attempts_used = _phase_attempt_count(session, combined_started)
                    if attempts_used >= MAX_ANALYSIS_PHASE_CALLS:
                        raise AnalysisContractError(
                            "combined 阶段实际模型调用预算已耗尽，无法 JSON repair"
                        )
                    final_prompt = _normalize_bounded_analysis_prompt(
                        build_json_repair_prompt(rag_result.text)
                    )
                    repaired_result = session.ask(
                        final_prompt,
                        prompt_kind=RagPromptKind.JSON_REPAIR,
                        require_sources=True,
                        max_attempts=MAX_ANALYSIS_PHASE_CALLS - attempts_used,
                    )
                    parsed_result = _parse_strict_json_object(repaired_result.text)
                    if parsed_result is None:
                        raise AnalysisContractError(
                            "JSON 修复后仍不是严格 JSON 对象"
                        )

                workflow_failure_stage = "architecture_contract"
                try:
                    if classification_mode == "legacy":
                        architecture_id = _resolve_analysis_architecture_id(
                            parsed_result,
                            params,
                        )
                        architecture_id = _validate_topk_architecture_id(
                            architecture_id,
                            visible_ids=visible_ids,
                            tree_index=tree_index,
                            architecture_list=architecture_list,
                        )
                    else:
                        if "architectureId" not in parsed_result:
                            raise ArchitectureContractError("architectureId 缺失")
                        architecture_id = _validate_topk_architecture_id(
                            parsed_result.get("architectureId"),
                            visible_ids=visible_ids,
                            tree_index=tree_index,
                            architecture_list=architecture_list,
                        )
                except ArchitectureContractError as contract_error:
                    force_standard = isinstance(
                        contract_error,
                        DataStandardParentContractError,
                    )
                    if classification_mode == "legacy":
                        architecture_id = (
                            _general_data_standard_leaf_id(architecture_list)
                            if force_standard
                            else _match_gjb_architecture_candidate(
                                parsed_result,
                                params,
                                original_text,
                                architecture_list,
                            )
                        )
                    else:
                        architecture_id = (
                            None
                            if data_standard_scope_guard_active
                            else _visible_data_standard_fallback_id(
                                visible_ids=visible_ids,
                                architecture_list=architecture_list,
                                force=force_standard,
                                context_values=(
                                    original_text,
                                    original_name,
                                ),
                            )
                        )
                    if architecture_id is None:
                        attempts_used = _phase_attempt_count(
                            session,
                            combined_started,
                        )
                        if attempts_used >= MAX_ANALYSIS_PHASE_CALLS:
                            if not data_standard_scope_guard_active:
                                raise ArchitectureContractError(
                                    "combined 阶段实际模型调用预算已耗尽，无法 "
                                    "architecture repair"
                                ) from contract_error
                            architecture_id = (
                                _visible_data_standard_fallback_id(
                                    visible_ids=visible_ids,
                                    architecture_list=architecture_list,
                                    force=True,
                                    context_values=(
                                        original_text,
                                        original_name,
                                    ),
                                )
                            )
                            if architecture_id is None:
                                raise ArchitectureContractError(
                                    "标准正文分类预算耗尽，且候选中不存在通用要求叶节点"
                                ) from contract_error
                            data_standard_general_fallback_applied = True
                        if architecture_id is None:
                            final_prompt = _normalize_bounded_analysis_prompt(
                                build_architecture_repair_prompt(
                                    parsed_result,
                                    visible_candidates,
                                    str(contract_error),
                                )
                            )
                            repaired_result = session.ask(
                                final_prompt,
                                prompt_kind=RagPromptKind.ARCHITECTURE_REPAIR,
                                require_sources=True,
                                max_attempts=(
                                    MAX_ANALYSIS_PHASE_CALLS - attempts_used
                                ),
                            )
                            if classification_mode == "legacy":
                                architecture_id = (
                                    _validate_architecture_repair_result(
                                        repaired_result.text,
                                        params,
                                    )
                                )
                                architecture_id = _validate_topk_architecture_id(
                                    architecture_id,
                                    visible_ids=visible_ids,
                                    tree_index=tree_index,
                                    architecture_list=architecture_list,
                                )
                            else:
                                try:
                                    _repaired, architecture_id = (
                                        _parse_topk_classification_result(
                                            repaired_result.text,
                                            visible_ids=visible_ids,
                                            tree_index=tree_index,
                                            architecture_list=architecture_list,
                                        )
                                    )
                                except ArchitectureContractError as repair_error:
                                    architecture_id = (
                                        _visible_data_standard_fallback_id(
                                            visible_ids=visible_ids,
                                            architecture_list=architecture_list,
                                            force=True,
                                            context_values=(
                                                original_text,
                                                original_name,
                                            ),
                                        )
                                        if data_standard_scope_guard_active
                                        else None
                                    )
                                    if architecture_id is None:
                                        raise ArchitectureContractError(
                                            "标准正文分类 repair 后仍无法确定类别，且候选中"
                                            "不存在通用要求叶节点"
                                        ) from repair_error
                                    data_standard_general_fallback_applied = True

            if constraint_decision is None:
                constraint_decision = (
                    _decide_topk_deterministic_architecture_constraint(
                        architecture_id,
                        file_name=file_name,
                        original_name=original_name,
                        visible_ids=visible_ids,
                        tree_index=tree_index,
                        architecture_list=architecture_list,
                        filename_constraint_mode=filename_constraint_mode,
                        data_standard_mode=data_standard_mode,
                        data_standard_profile=data_standard_profile,
                        jane_profile=jane_profile,
                        scope_resolution=scope_resolution,
                    )
                )
            if data_standard_general_fallback_applied:
                constraint_decision = _ArchitectureConstraintDecision(
                    pre_architecture_id=architecture_id,
                    post_architecture_id=architecture_id,
                    reason_code="data_standard_general_fallback",
                    matched_scope_parent_id=None,
                    tree_gap=False,
                )
            architecture_id = constraint_decision.post_architecture_id
            _log_architecture_constraint_decision(
                execution_id=execution_id,
                file_name=file_name,
                filename_constraint_mode=filename_constraint_mode,
                profile=jane_profile,
                decision=constraint_decision,
                data_standard_mode=data_standard_mode,
                data_standard_profile=data_standard_profile,
            )
            if len(session.trace.attempts) > MAX_ANALYSIS_MODEL_CALLS:
                raise AnalysisContractError("文件分析实际模型调用超过 4 次")
            mapped_result = map_analysis_result(
                parsed_result,
                params,
                original_text=original_text,
                resolved_architecture_id=architecture_id,
            )
            _log_analysis_content_warnings(
                parsed_result,
                mapped_result,
                file_name=file_name,
            )
            returned_rank = next(
                index + 1
                for index, candidate in enumerate(visible_candidates)
                if candidate["id"] == architecture_id
            )
            if recall_audit_enabled:
                task_service.finalize_architecture_recall_decision(
                    execution_id=execution_id,
                    returned_architecture_id=architecture_id,
                    returned_rank=returned_rank,
                    total_elapsed_ms=_elapsed_ms(
                        workflow_started_at,
                        floor=int(recall_audit_fields["recall_elapsed_ms"]),
                    ),
                )
                recall_audit_finalized = True
        except Exception as exc:
            trace = exc.trace if isinstance(exc, RagOperationError) else (
                session.trace if session is not None else None
            )
            if trace is None:
                raise
            try:
                _record_lease_resources(
                    task_service,
                    execution_id,
                    trace,
                    prepared_document,
                )
            except Exception:
                logger.critical(
                    "文件分析失败后无法更新资源租约: file_name=%s execution_id=%s",
                    file_name,
                    execution_id,
                    exc_info=True,
                )
            error_message = _safe_task_error(exc, fallback="RAG 或业务契约失败")
            failure_stage = (
                "architecture_prompt_budget"
                if isinstance(exc, ArchitecturePromptBudgetError)
                else workflow_failure_stage
            )
            if recall_audit_enabled and not recall_audit_finalized:
                try:
                    task_service.finalize_architecture_recall_decision(
                        execution_id=execution_id,
                        returned_architecture_id=None,
                        returned_rank=None,
                        total_elapsed_ms=_elapsed_ms(
                            workflow_started_at,
                            floor=int(recall_audit_fields["recall_elapsed_ms"]),
                        ),
                        failure_stage=failure_stage,
                        error_message=error_message,
                    )
                    recall_audit_finalized = True
                except Exception as recall_audit_exc:
                    error_message = _safe_task_error(
                        recall_audit_exc,
                        fallback="领域召回终结审计失败",
                    )
                    logger.critical(
                        "文件分析失败后无法终结领域召回审计: "
                        "file_name=%s execution_id=%s",
                        file_name,
                        execution_id,
                        exc_info=True,
                    )
            try:
                audit_result = task_service.create_llm_interaction_with_trace(
                    business_type="file",
                    business_key=file_name,
                    execution_id=execution_id,
                    prompt=final_prompt,
                    trace=trace,
                    status="failed",
                    error_message=error_message,
                )
            except Exception as audit_exc:
                audit_error = _safe_task_error(audit_exc, fallback="交互审计失败")
                try:
                    task_service.rag_resource_leases.mark_audit_result(
                        execution_id=execution_id,
                        interaction_id=None,
                        error_message=audit_error,
                    )
                except Exception:
                    logger.critical(
                        "交互审计失败后资源租约状态也无法更新: execution_id=%s",
                        execution_id,
                        exc_info=True,
                    )
                logger.critical(
                    "文件分析交互审计失败，保留全部 RAG 现场: file_name=%s execution_id=%s",
                    file_name,
                    execution_id,
                    exc_info=True,
                )
                _finalize_file_failure(
                    task_service=task_service,
                    progress_hub=progress_hub,
                    file_name=file_name,
                    execution_id=execution_id,
                    original_name=original_name,
                    stage="audit",
                    error_message=audit_error,
                    callback_url=callback_url,
                    callback_timeout=callback_timeout,
                )
                return

            try:
                task_service.rag_resource_leases.mark_audit_result(
                    execution_id=execution_id,
                    interaction_id=audit_result.interaction_id,
                )
            except Exception as lease_exc:
                lease_error = _safe_task_error(
                    lease_exc,
                    fallback="资源租约审计状态更新失败",
                )
                logger.critical(
                    "失败交互已审计但资源租约推进失败: interaction_id=%s execution_id=%s",
                    audit_result.interaction_id,
                    execution_id,
                    exc_info=True,
                )
                _finalize_file_failure(
                    task_service=task_service,
                    progress_hub=progress_hub,
                    file_name=file_name,
                    execution_id=execution_id,
                    original_name=original_name,
                    stage="resource_lease",
                    error_message=lease_error,
                    callback_url=callback_url,
                    callback_timeout=callback_timeout,
                )
                if session is not None:
                    _close_audited_session(
                        task_service=task_service,
                        session=session,
                        interaction_id=audit_result.interaction_id,
                        execution_id=execution_id,
                        audited_trace=trace,
                        retain_document=False,
                    )
                return
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                execution_id=execution_id,
                original_name=original_name,
                stage=failure_stage,
                error_message=error_message,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
            if session is not None:
                _close_audited_session(
                    task_service=task_service,
                    session=session,
                    interaction_id=audit_result.interaction_id,
                    execution_id=execution_id,
                    audited_trace=trace,
                    retain_document=False,
                )
            else:
                # open_isolated_session 失败时，Gateway 已把内部回滚写入初始 trace；没有
                # 可供业务层再次 close 的 Session。原子审计入口已经按回滚事件写入 cleanup
                # 终态；这里只在回滚成功时终结资源租约，失败时继续保留待恢复记录。
                rollback_failed = any(
                    event.operation == "context_rollback" and not event.success
                    for event in trace.lifecycle_events
                )
                if not rollback_failed:
                    task_service.rag_resource_leases.mark_closed(
                        execution_id=execution_id,
                    )
                else:
                    logger.critical(
                        "隔离 Session 打开回滚失败，资源租约保持待恢复: "
                        "interaction_id=%s execution_id=%s",
                        audit_result.interaction_id,
                        execution_id,
                    )
            return

        successful_trace = session.trace
        try:
            audit_result = task_service.create_llm_interaction_with_trace(
                business_type="file",
                business_key=file_name,
                execution_id=execution_id,
                prompt=final_prompt,
                trace=successful_trace,
                status="succeeded",
            )
        except Exception as audit_exc:
            audit_error = _safe_task_error(audit_exc, fallback="交互审计失败")
            try:
                task_service.rag_resource_leases.mark_audit_result(
                    execution_id=execution_id,
                    interaction_id=None,
                    error_message=audit_error,
                )
            except Exception:
                logger.critical(
                    "成功结果审计失败后资源租约状态也无法更新: execution_id=%s",
                    execution_id,
                    exc_info=True,
                )
            logger.critical(
                "文件分析成功结果审计失败，禁止永久入库并保留现场: "
                "file_name=%s execution_id=%s",
                file_name,
                execution_id,
                exc_info=True,
            )
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                execution_id=execution_id,
                original_name=original_name,
                stage="audit",
                error_message=audit_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
            return

        try:
            task_service.rag_resource_leases.mark_audit_result(
                execution_id=execution_id,
                interaction_id=audit_result.interaction_id,
            )
        except Exception as lease_exc:
            lease_error = _safe_task_error(lease_exc, fallback="资源租约审计状态更新失败")
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                execution_id=execution_id,
                original_name=original_name,
                stage="resource_lease",
                error_message=lease_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
            _close_audited_session(
                task_service=task_service,
                session=session,
                interaction_id=audit_result.interaction_id,
                execution_id=execution_id,
                audited_trace=successful_trace,
                retain_document=False,
            )
            return

        retain_document = False
        knowledge_store_succeeded = False
        try:
            task_service.require_current_execution(
                "file",
                file_name,
                execution_id,
                allowed_statuses=("0", "1"),
            )
        except (TaskExecutionConflictError, TaskStateConflictError):
            logger.warning(
                "永久知识库写入前执行身份已失效，清理本次RAG资源: "
                "file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
            _close_audited_session(
                task_service=task_service,
                session=session,
                interaction_id=audit_result.interaction_id,
                execution_id=execution_id,
                audited_trace=successful_trace,
                retain_document=False,
            )
            return
        try:
            _store_prepared_analysis_document(
                knowledge_index_factory=knowledge_index_factory,
                execution_id=execution_id,
                file_name=file_name,
                original_name=original_name,
                mapped_result=mapped_result,
                architecture_list=architecture_list,
                prepared_document=prepared_document,
            )
            retain_document = True
            knowledge_store_succeeded = True
        except KnowledgeIndexDocumentReleasedError as knowledge_exc:
            # 只有该类型能证明 Gateway 已解绑永久集合并提交补偿成功状态，此时允许 RAG
            # Session 永久删除未转交的全局文档。
            logger.exception(
                "永久知识库写入失败且已完成文档释放补偿: file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
            knowledge_error = _safe_task_error(knowledge_exc, fallback="永久知识库写入失败")
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                execution_id=execution_id,
                original_name=original_name,
                stage="knowledge_index",
                error_message=knowledge_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
        except KnowledgeIndexRetentionRequiredError as knowledge_exc:
            retain_document = True
            logger.exception(
                "永久知识库写入状态需人工恢复，保留全局文档: file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
            knowledge_error = _safe_task_error(knowledge_exc, fallback="永久知识库需要恢复")
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                execution_id=execution_id,
                original_name=original_name,
                stage="knowledge_index_recovery",
                error_message=knowledge_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
        except Exception as knowledge_exc:
            # 未分类异常无法证明永久集合没有接管文档。安全策略必须保留全局实体，等待
            # 协调记录对账；错误删除会破坏永久知识库中可能已经提交的引用。
            retain_document = True
            logger.exception(
                "永久知识库写入发生未分类异常，保留全局文档: "
                "file_name=%s execution_id=%s error_type=%s",
                file_name,
                execution_id,
                type(knowledge_exc).__name__,
            )
            knowledge_error = _safe_task_error(knowledge_exc, fallback="永久知识库写入状态不确定")
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                execution_id=execution_id,
                original_name=original_name,
                stage="knowledge_index_unknown",
                error_message=knowledge_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
        if not knowledge_store_succeeded:
            _close_audited_session(
                task_service=task_service,
                session=session,
                interaction_id=audit_result.interaction_id,
                execution_id=execution_id,
                audited_trace=successful_trace,
                retain_document=retain_document,
            )
            return

        try:
            task_service.update_task_progress(
                "file",
                file_name,
                progress=0.65,
                message="正在翻译文档",
                status="1",
                execution_id=execution_id,
            )
            _publish_progress(progress_hub, file_name, 0.65)
            enriched_result = enrich_with_translations(
                mapped_result,
                downloaded_path,
                params.get("enableFullTranslation", True),
            )
            task_service.update_task_progress(
                "file",
                file_name,
                progress=0.95,
                message="翻译完成，准备回调",
                status="1",
                execution_id=execution_id,
            )
            _publish_progress(progress_hub, file_name, 0.95)
            callback_payload = build_file_callback_payload(
                file_name,
                enriched_result,
                status="2",
            )
            task_service.mark_business_result(
                "file",
                file_name,
                callback_payload,
                status="2",
                message="解析完成",
                execution_id=execution_id,
            )
            _publish_progress(progress_hub, file_name, 1.0)
            _submit_callback(
                task_service=task_service,
                file_name=file_name,
                execution_id=execution_id,
                original_name=original_name,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
                callback_payload=callback_payload,
            )
            logger.info(
                "文件分析任务完成: file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
        except (TaskExecutionConflictError, TaskStateConflictError):
            logger.warning(
                "文件分析知识库转交后执行身份已失效，不覆盖当前任务或发送回调: "
                "file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
        except Exception as exc:
            post_transfer_error = _safe_task_error(exc, fallback="知识库转交后业务处理失败")
            logger.exception(
                "文件分析在文档所有权转交后失败: file_name=%s execution_id=%s",
                file_name,
                execution_id,
            )
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                execution_id=execution_id,
                original_name=original_name,
                stage="post_transfer",
                error_message=post_transfer_error,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )
        finally:
            _close_audited_session(
                task_service=task_service,
                session=session,
                interaction_id=audit_result.interaction_id,
                execution_id=execution_id,
                audited_trace=successful_trace,
                retain_document=True,
            )


def run_file_analysis_task(
    *,
    task_service: LLMTaskService,
    progress_hub: LLMProgressHub,
    request_payload: Dict[str, Any],
    download_root: str,
    callback_url: str,
    callback_timeout: float,
    document_rag_factory: DocumentRagFactory,
    knowledge_index_factory: KnowledgeIndexFactory,
    execution_id: str,
    analysis_classification_mode: str = "topk_two_stage",
    analysis_filename_constraint_mode: str = (
        ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY
    ),
    analysis_data_standard_mode: str = ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    analysis_identity_reselect_mode: str = ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
) -> None:
    """提供后台线程的最终异常边界，并委托阶段 9 单文件状态机。

    状态机内部已经处理所有创建 Session 后的异常。本边界主要覆盖 Factory 进入失败、依赖
    契约错误等尚未创建外部资源的异常，确保后台线程不会让任务永久停留在处理中。若未来
    在内部增加新的外部副作用，必须先把相应审计和补偿加入状态机，不能依赖本兜底处理。
    """
    try:
        _execute_file_analysis_task(
            task_service=task_service,
            progress_hub=progress_hub,
            request_payload=request_payload,
            download_root=download_root,
            callback_url=callback_url,
            callback_timeout=callback_timeout,
            document_rag_factory=document_rag_factory,
            knowledge_index_factory=knowledge_index_factory,
            execution_id=execution_id,
            analysis_classification_mode=analysis_classification_mode,
            analysis_filename_constraint_mode=analysis_filename_constraint_mode,
            analysis_data_standard_mode=analysis_data_standard_mode,
            analysis_identity_reselect_mode=analysis_identity_reselect_mode,
        )
    except (TaskExecutionConflictError, TaskStateConflictError):
        logger.warning(
            "文件分析worker执行身份已失效，停止且不写入当前任务: execution_id=%s",
            execution_id,
        )
        return
    except Exception as exc:
        params_list = request_payload.get("params", [])
        params = params_list[0] if params_list and isinstance(params_list[0], dict) else {}
        file_name = _as_text(params.get("fileName"))
        original_name = (
            _as_business_original_file_name(params.get("originalFileName"))
            or file_name
        )
        error_message = _safe_task_error(exc, fallback="文件分析编排失败")
        failure_stage = "orchestration"

        # Factory create/__enter__ 和无法提供 trace 的 Session 打开异常发生在召回
        # 决策已经写入之后。最终异常边界必须补齐该审计终态，不能把一条未终结决策
        # 永久留在库中，也不能用笼统 orchestration 隐藏稳定领域阶段。
        if file_name:
            try:
                task = task_service.require_current_execution(
                    "file",
                    file_name,
                    execution_id,
                )
            except TaskExecutionConflictError:
                logger.warning(
                    "文件分析兜底检测到执行已被替换，停止终结新任务: "
                    "file_name=%s execution_id=%s",
                    file_name,
                    execution_id,
                )
                return
            if task and _as_text(task.get("status")) in {"2", "3"}:
                # 正常/失败业务终态已经提交后，Factory 退出阶段仅可能剩下本地
                # Transport 关闭等资源告警。不得覆盖终态或再发送一份相反 callback。
                logger.critical(
                    "文件分析 Factory 退出异常，但业务任务已有终态，保持原结果: "
                    "file_name=%s status=%s error_type=%s",
                    file_name,
                    task.get("status"),
                    type(exc).__name__,
                    exc_info=True,
                )
                return
            if execution_id:
                try:
                    recall_audit = task_service.get_architecture_recall_decision(
                        execution_id
                    )
                except Exception:
                    recall_audit = None
                    logger.critical(
                        "文件分析兜底无法读取领域召回审计: execution_id=%s",
                        execution_id,
                        exc_info=True,
                    )
                if recall_audit and not recall_audit.get("finalized_at"):
                    failure_stage = "architecture_contract"
                    try:
                        task_service.finalize_architecture_recall_decision(
                            execution_id=execution_id,
                            returned_architecture_id=None,
                            returned_rank=None,
                            total_elapsed_ms=int(
                                recall_audit.get("recall_elapsed_ms") or 0
                            ),
                            failure_stage=failure_stage,
                            error_message=error_message,
                        )
                    except Exception as audit_exc:
                        error_message = _safe_task_error(
                            audit_exc,
                            fallback="领域召回终结审计失败",
                        )
                        logger.critical(
                            "文件分析兜底无法终结领域召回审计: "
                            "execution_id=%s",
                            execution_id,
                            exc_info=True,
                        )
        logger.exception(
            "文件分析后台线程未处理异常: file_name=%s error_type=%s",
            file_name,
            type(exc).__name__,
        )
        if file_name:
            _finalize_file_failure(
                task_service=task_service,
                progress_hub=progress_hub,
                file_name=file_name,
                execution_id=execution_id,
                original_name=original_name,
                stage=failure_stage,
                error_message=error_message,
                callback_url=callback_url,
                callback_timeout=callback_timeout,
            )


def run_file_analysis_batch_task(
    *,
    task_service: LLMTaskService,
    progress_hub: LLMProgressHub,
    request_payload: Dict[str, Any],
    download_root: str,
    callback_url: str,
    callback_timeout: float,
    document_rag_factory: DocumentRagFactory,
    knowledge_index_factory: KnowledgeIndexFactory,
    execution_ids: Mapping[str, str],
    analysis_classification_mode: str = "topk_two_stage",
    analysis_filename_constraint_mode: str = (
        ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY
    ),
    analysis_data_standard_mode: str = ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    analysis_identity_reselect_mode: str = ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
) -> None:
    """按请求顺序执行批量分析，并保证每个文件分别进入两类 Factory 租约。"""
    params_list = request_payload.get("params", [])
    for params in params_list:
        if not isinstance(params, dict):
            continue
        file_name = _as_text(params.get("fileName"))
        if file_name and not _as_text(execution_ids.get(file_name)):
            raise ValueError(f"批量文件任务缺少execution_id: {file_name}")

    for index, params in enumerate(params_list):
        if not isinstance(params, dict):
            continue
        file_name = _as_text(params.get("fileName"))
        if not file_name:
            continue
        execution_id = _as_text(execution_ids[file_name])
        if index > 0:
            task_service.update_task_progress(
                "file",
                file_name,
                progress=0.0,
                message="准备开始解析",
                status="1",
                execution_id=execution_id,
            )
            _publish_progress(progress_hub, file_name, 0.0)
        run_file_analysis_task(
            task_service=task_service,
            progress_hub=progress_hub,
            request_payload={"businessType": "file", "params": [params]},
            download_root=download_root,
            callback_url=callback_url,
            callback_timeout=callback_timeout,
            document_rag_factory=document_rag_factory,
            knowledge_index_factory=knowledge_index_factory,
            execution_id=execution_id,
            analysis_classification_mode=analysis_classification_mode,
            analysis_filename_constraint_mode=analysis_filename_constraint_mode,
            analysis_data_standard_mode=analysis_data_standard_mode,
            analysis_identity_reselect_mode=analysis_identity_reselect_mode,
        )
