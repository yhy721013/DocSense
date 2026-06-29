from __future__ import annotations

import logging
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import os
from collections import defaultdict

from app.services.utils.anythingllm_client import AnythingLLMClient
from app.services.core.config import load_anythingllm_config
from app.services.rag.rag_enhancer import get_rag_enhancer

from app.services.core.database import DatabaseService
from app.services.utils.callback_client import post_callback_payload
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.task_service import LLMTaskService
from app.services.llm_service.translation_service import get_translation_service
from app.services.core.prompts import (
    build_input_field_prompt,
    build_chunk_based_field_prompt,
    build_multi_chunk_based_field_prompt,
    build_table_extraction_prompt,
)

logger = logging.getLogger(__name__)


TARGET_EVIDENCE_TOP_N = 8
TABLE_EVIDENCE_TOP_N = 16
TERMS_RULE_TOP_N = 3
MAX_TARGET_CHUNKS_PER_FILE = 8
MAX_TARGET_CONTEXT_CHARS = 12000
MAX_TABLE_ROWS = 100
TERMS_RULE_CONTEXT_MAX_CHARS = 1200
PROMPT_SEND_MAX_ATTEMPTS = 2
PROMPT_SEND_RETRY_DELAY_SECONDS = 2.0
TERMS_RULE_CONTEXT_ENABLED_ENV = "WEAPONRY_TERMS_RULE_CONTEXT_ENABLED"
TERMS_WORKSPACE_NAME = os.getenv("WEAPONRY_TERMS_WORKSPACE_NAME", "weaponry-terms-rules")
TERMS_DIR = Path(os.getenv("WEAPONRY_TERMS_DIR", "terms"))
TERM_RULE_NAME_RE = re.compile(r"(term_rule_[^/\\]+?\.md)", re.IGNORECASE)


def _parse_env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    value = raw_value.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _terms_rule_context_enabled() -> bool:
    return _parse_env_bool(TERMS_RULE_CONTEXT_ENABLED_ENV, True)


@dataclass
class WeaponryRetrievalContext:
    """一次 weaponry 任务内的检索上下文。"""

    target_file_names: Set[str]
    target_doc_paths: Set[str]
    source_original_names: Optional[Dict[str, str]] = None
    single_target_original_name: str = ""
    terms_workspace_slug: Optional[str] = None
    target_workspace_term_doc_paths: Optional[List[str]] = None

    def __post_init__(self) -> None:
        if self.source_original_names is None:
            self.source_original_names = {}
        if self.target_workspace_term_doc_paths is None:
            self.target_workspace_term_doc_paths = []


# ---------------------------------------------------------------------------
# 语言检测 & 翻译
# ---------------------------------------------------------------------------



def _translate_if_needed(text: str) -> str:
    """对于所有文本，调用翻译服务翻译为中文。"""
    if not text:
        return ""
    try:
        service = get_translation_service()
        return service.translate_text_only(text, target_lang="Chinese", fast_translate=True, as_html=False)
    except Exception as e:
        logger.warning("翻译失败: %s", e)
        return ""


# ---------------------------------------------------------------------------
# source 映射
# ---------------------------------------------------------------------------

def _strip_document_metadata(text: str) -> str:
    """去除 AnythingLLM chunk 文本中的 <document_metadata>...</document_metadata> 前缀。"""
    if not text:
        return ""
    tag_end = "</document_metadata>"
    idx = text.find(tag_end)
    if idx != -1:
        return text[idx + len(tag_end):].strip()
    return text.strip()


def _normalize_source_name(name: str) -> str:
    """归一化 AnythingLLM 文档来源名，便于判断目标 PDF 与术语文件。"""
    if not name:
        return ""
    normalized = str(name).replace("\\", "/").strip()
    if "custom-documents/" in normalized:
        normalized = normalized.split("custom-documents/")[-1]
    return Path(normalized).name


def _extract_chunk_source_name(chunk: Dict[str, Any]) -> str:
    metadata = chunk.get("metadata") or {}
    file_name = metadata.get("title") or metadata.get("sourceDocument") or metadata.get("file_name")

    if not file_name:
        raw_text = chunk.get("text", "")
        if "sourceDocument:" in raw_text:
            for line in raw_text.splitlines():
                if line.startswith("sourceDocument:"):
                    file_name = line.split("sourceDocument:", 1)[1].strip()
                    break

    return _normalize_source_name(str(file_name or ""))


def _source_lookup_keys(name: str) -> Set[str]:
    """生成来源名查库映射 key，兼容 AnythingLLM 返回的多种文档标识。"""
    raw = str(name or "").replace("\\", "/").strip()
    if not raw:
        return set()

    candidates = {raw}
    if "custom-documents/" in raw:
        candidates.add("custom-documents/" + raw.split("custom-documents/", 1)[1])

    normalized = _normalize_source_name(raw)
    if normalized:
        candidates.add(normalized)

    basename = Path(raw).name
    if basename:
        candidates.add(basename)

    return {candidate.lower() for candidate in candidates if candidate}


def _add_source_original_name_mapping(
    mapping: Dict[str, str],
    aliases: List[str],
    original_name: str,
) -> None:
    if not original_name:
        return
    for alias in aliases:
        for key in _source_lookup_keys(alias):
            mapping.setdefault(key, original_name)


def _resolve_original_source_name(
    source_name: str,
    context: Optional[WeaponryRetrievalContext],
) -> str:
    """将 Mode 2 的内部来源文件名映射为 documents.original_name。"""
    normalized = _normalize_source_name(source_name)
    if _is_terms_source_name(normalized):
        return normalized

    if not source_name:
        return context.single_target_original_name if context else ""

    mapping = context.source_original_names if context and context.source_original_names else {}
    for key in _source_lookup_keys(source_name):
        original_name = mapping.get(key)
        if original_name:
            return original_name

    return normalized or str(source_name or "")


def _is_terms_source_name(source_name: str) -> bool:
    name = _normalize_source_name(source_name).lower()
    return name.startswith("term_rule_") or (name.endswith(".md") and "term_rule_" in name)


def _is_target_source(source_name: str, context: Optional[WeaponryRetrievalContext]) -> bool:
    if not context or not context.target_file_names:
        return not _is_terms_source_name(source_name)
    normalized = _normalize_source_name(source_name)
    if not normalized:
        # 术语文档会在任务开始时从目标 workspace 临时移出；剩余无来源名 chunk
        # 只能来自当前目标 PDF，保守接受以避免 AnythingLLM metadata 缺失导致误过滤。
        return True
    if normalized in context.target_file_names:
        return True
    return any(normalized in target or target in normalized for target in context.target_file_names)


def _get_score(src: Dict[str, Any]) -> float:
    try:
        return float(src.get("score", 0))
    except (TypeError, ValueError):
        return 0.0


def _vector_search_with_top_n(
    client: AnythingLLMClient,
    workspace_slug: str,
    query: str,
    *,
    top_n: int,
    user_id: int = 1,
) -> List[Dict[str, Any]]:
    """在 weaponry 内部执行带 topN 的检索，支持增强 RAG（BM25+Embedding+RRF+Rerank）。
    
    如果 RAG 增强未启用，自动降级为原始向量检索。
    """
    # 尝试使用 RAG 增强器
    try:
        enhancer = get_rag_enhancer()
        if enhancer.is_enhanced():
            logger.debug("使用增强 RAG 检索: workspace=%s, query='%s', top_n=%d", workspace_slug, query, top_n)
            results = enhancer.hybrid_search(
                client=client,
                workspace_slug=workspace_slug,
                query=query,
                top_n=top_n,
                user_id=user_id,
            )
            return results
    except Exception as e:
        logger.warning("RAG 增强器调用失败，降级为向量检索: %s", e)
    
    # 降级方案：使用原始 AnythingLLM 向量检索
    url = f"{client.config.base_url}/workspace/{workspace_slug}/vector-search"
    payload = {"query": query, "topN": top_n}
    try:
        resp = client.session.post(
            url,
            headers=client._json_headers(user_id),
            json=payload,
            timeout=client.config.timeout,
        )
        if not resp.ok:
            logger.error("向量搜索失败 workspace=%s: %s %s", workspace_slug, resp.status_code, resp.text)
            return []
        body = resp.json()
        results = body.get("results", [])
        return results if isinstance(results, list) else []
    except Exception as e:
        logger.error("向量搜索时出现异常 workspace=%s: %s", workspace_slug, e)
        return []


def _list_workspace_documents(
    client: AnythingLLMClient,
    workspace_slug: str,
    user_id: int = 1,
) -> List[Dict[str, Any]]:
    url = f"{client.config.base_url}/workspace/{workspace_slug}"
    try:
        resp = client.session.get(url, headers=client._json_headers(user_id), timeout=client.config.timeout)
        if not resp.ok:
            logger.error("获取工作区 %s 文档列表失败: %s %s", workspace_slug, resp.status_code, resp.text)
            return []
        workspace = resp.json().get("workspace")
        if isinstance(workspace, list):
            workspace = workspace[0] if workspace else None
        if not isinstance(workspace, dict):
            return []
        docs = workspace.get("documents", [])
        return docs if isinstance(docs, list) else []
    except Exception as e:
        logger.error("获取工作区 %s 文档列表异常: %s", workspace_slug, e)
        return []


def _workspace_document_path(doc: Dict[str, Any]) -> str:
    return str(doc.get("docpath") or doc.get("location") or "").replace("\\", "/")


def _workspace_document_title(doc: Dict[str, Any]) -> str:
    return _normalize_source_name(str(doc.get("title") or doc.get("name") or doc.get("filename") or _workspace_document_path(doc)))


def _term_rule_key(source_name: str) -> str:
    """Return a stable term_rule filename key from a title or AnythingLLM doc path."""
    normalized = _normalize_source_name(source_name)
    match = TERM_RULE_NAME_RE.search(normalized)
    return match.group(1).lower() if match else ""


def _workspace_term_doc_paths_by_key(docs: List[Dict[str, Any]]) -> Dict[str, str]:
    paths_by_key: Dict[str, str] = {}
    for doc in docs:
        doc_path = _workspace_document_path(doc)
        key = _term_rule_key(_workspace_document_title(doc)) or _term_rule_key(doc_path)
        if key and doc_path and key not in paths_by_key:
            paths_by_key[key] = doc_path
    return paths_by_key


def _unique_term_doc_paths(doc_paths: List[str]) -> List[str]:
    unique_paths: List[str] = []
    seen: Set[str] = set()
    for doc_path in doc_paths:
        normalized = str(doc_path or "").replace("\\", "/")
        if not normalized:
            continue
        key = _term_rule_key(normalized) or normalized
        if key in seen:
            continue
        seen.add(key)
        unique_paths.append(normalized)
    return unique_paths


def _target_document_records(kb_service: DatabaseService, architecture_id: int) -> List[Dict[str, Any]]:
    return [
        record
        for record in kb_service.list_document_records()
        if str(record.get("architecture_id")) == str(architecture_id)
    ]


def _build_terms_rule_query(field_name: str, field_desc: str) -> str:
    desc_part = f"\n字段说明：{field_desc}" if field_desc else ""
    return (
        f"请检索与字段“{field_name}”相关的术语规则、字段别名、口径说明和单位规则。{desc_part}\n"
        "只需要返回有助于理解字段含义的规则资料。"
    )


def _format_terms_rule_context(term_chunks: List[Dict[str, Any]], *, max_chars: int = TERMS_RULE_CONTEXT_MAX_CHARS) -> str:
    parts: List[str] = []
    used_sources: Set[str] = set()
    remaining = max_chars
    for chunk in sorted(term_chunks, key=_get_score, reverse=True):
        source_name = _extract_chunk_source_name(chunk)
        if not _is_terms_source_name(source_name) or source_name in used_sources:
            continue
        text = _strip_document_metadata(chunk.get("text", ""))
        if not text:
            continue
        snippet = text[: min(remaining, 900)].strip()
        if not snippet:
            continue
        parts.append(f"来源：{source_name}\n{snippet}")
        used_sources.add(source_name)
        remaining = max_chars - sum(len(part) for part in parts)
        if remaining <= 0 or len(parts) >= TERMS_RULE_TOP_N:
            break
    return "\n\n".join(parts)


def _fallback_target_file_name(context: Optional[WeaponryRetrievalContext]) -> str:
    if not context or not context.target_file_names:
        return ""
    return sorted(context.target_file_names)[0]


def _limit_chunks_for_prompt(chunks: List[str]) -> List[str]:
    limited: List[str] = []
    remaining = MAX_TARGET_CONTEXT_CHARS
    for chunk in chunks[:MAX_TARGET_CHUNKS_PER_FILE]:
        text = (chunk or "").strip()
        if not text:
            continue
        if len(text) > remaining:
            text = text[:remaining].rstrip()
        if text:
            limited.append(text)
            remaining -= len(text)
        if remaining <= 0:
            break
    return limited


def _extract_thread_slug_from_client(client: AnythingLLMClient, thread_info: Dict[str, Any]) -> Optional[str]:
    if not isinstance(thread_info, dict):
        return None
    extractor = getattr(client, "extract_thread_slug", None)
    if callable(extractor):
        try:
            slug = extractor(thread_info)
            if slug:
                return str(slug)
        except Exception as e:
            logger.warning("提取临时 Thread slug 失败: %s", e)
    for key in ("slug", "threadSlug", "thread_slug", "id"):
        value = thread_info.get(key)
        if value:
            return str(value)
    return None


def _send_prompt_to_isolated_thread(
    client: AnythingLLMClient,
    workspace_slug: str,
    parent_thread_slug: str,
    prompt: str,
    *,
    user_id: int = 1,
    mode: str = "chat",
) -> Optional[Dict[str, Any]]:
    """每次字段抽取尽量使用独立 Thread，避免字段间历史污染；失败时回退到父 Thread。"""
    create_thread = getattr(client, "create_thread", None)
    delete_thread = getattr(client, "delete_thread", None)

    for attempt in range(1, PROMPT_SEND_MAX_ATTEMPTS + 1):
        active_thread_slug = parent_thread_slug
        delete_after = False

        if callable(create_thread):
            thread_name = f"{parent_thread_slug}-field-{int(time.time() * 1000)}-{attempt}"
            try:
                thread_info = create_thread(workspace_slug, thread_name, user_id=user_id)
                child_thread_slug = _extract_thread_slug_from_client(client, thread_info or {})
                if child_thread_slug:
                    active_thread_slug = child_thread_slug
                    delete_after = True
            except Exception as e:
                logger.warning("创建字段临时 Thread 失败，回退到父 Thread: %s", e)

        try:
            result = client.send_prompt_to_thread(
                workspace_slug,
                active_thread_slug,
                prompt,
                user_id=user_id,
                mode=mode,
            )
        finally:
            if delete_after and callable(delete_thread):
                try:
                    if not delete_thread(workspace_slug, active_thread_slug, user_id=user_id):
                        logger.warning("删除字段临时 Thread %s 失败（不影响结果）", active_thread_slug)
                except Exception as e:
                    logger.warning("删除字段临时 Thread %s 异常（不影响结果）: %s", active_thread_slug, e)

        if result:
            return result

        logger.warning(
            "字段提示词调用无有效响应，准备重试: workspace=%s thread=%s attempt=%d/%d",
            workspace_slug,
            active_thread_slug,
            attempt,
            PROMPT_SEND_MAX_ATTEMPTS,
        )
        if attempt < PROMPT_SEND_MAX_ATTEMPTS:
            time.sleep(PROMPT_SEND_RETRY_DELAY_SECONDS)

    return None


def _ensure_terms_workspace(
    client: AnythingLLMClient,
    term_doc_paths: List[str],
    user_id: int = 1,
) -> Optional[str]:
    requested_doc_paths = _unique_term_doc_paths(term_doc_paths)
    if not requested_doc_paths:
        return None
    workspace_info = client.ensure_workspace(TERMS_WORKSPACE_NAME, user_id=user_id)
    if not workspace_info:
        logger.warning("无法创建或获取术语规则 workspace: %s", TERMS_WORKSPACE_NAME)
        return None
    workspace_slug = workspace_info.get("slug") or str(workspace_info.get("id") or "")
    if not workspace_slug:
        logger.warning("术语规则 workspace 缺少 slug: %s", workspace_info)
        return None

    existing_docs = _list_workspace_documents(client, workspace_slug, user_id=user_id)
    existing_paths_by_key = _workspace_term_doc_paths_by_key(existing_docs)
    missing_doc_paths = [
        doc_path
        for doc_path in requested_doc_paths
        if (_term_rule_key(doc_path) or doc_path) not in existing_paths_by_key
    ]
    if not missing_doc_paths:
        logger.info(
            "复用已有术语规则 workspace: %s term_docs=%d",
            workspace_slug,
            len(existing_paths_by_key),
        )
        return workspace_slug

    if not client.update_embeddings_batch(workspace_slug, adds=missing_doc_paths, user_id=user_id):
        logger.warning("术语规则 workspace 加入术语文档失败: %s", workspace_slug)
        return None
    logger.info(
        "术语规则 workspace 已补充缺失文档: %s adds=%d existing=%d",
        workspace_slug,
        len(missing_doc_paths),
        len(existing_paths_by_key),
    )
    return workspace_slug


def _upload_local_terms_if_needed(client: AnythingLLMClient, user_id: int = 1) -> List[str]:
    existing_paths_by_key: Dict[str, str] = {}
    existing = client.find_workspace_by_name(TERMS_WORKSPACE_NAME, user_id=user_id)
    if existing:
        slug = existing.get("slug") or str(existing.get("id") or "")
        if slug:
            existing_docs = _list_workspace_documents(client, slug, user_id=user_id)
            existing_paths_by_key = _workspace_term_doc_paths_by_key(existing_docs)

    if not TERMS_DIR.exists() or not TERMS_DIR.is_dir():
        return list(existing_paths_by_key.values())
    term_files = sorted(TERMS_DIR.glob("*.md"))
    if not term_files:
        return list(existing_paths_by_key.values())

    uploaded_paths: List[str] = []
    for term_file in term_files:
        key = _term_rule_key(term_file.name)
        if key and key in existing_paths_by_key:
            continue
        doc_info = client.upload_document(str(term_file), user_id=user_id)
        if not doc_info:
            logger.warning("上传术语文件失败，跳过: %s", term_file)
            continue
        doc_id = doc_info.get("id") or doc_info.get("docId")
        doc_path = doc_info.get("location") or doc_info.get("docpath") or f"custom-documents/{term_file.name}-{doc_id}.json"
        client.wait_for_processing(str(doc_path), retries=180, delay=1.0)
        uploaded_paths.append(str(doc_path))
    return [*existing_paths_by_key.values(), *uploaded_paths]


def _prepare_retrieval_context(
    client: AnythingLLMClient,
    kb_service: DatabaseService,
    architecture_id: int,
    workspace_slug: str,
    user_id: int = 1,
) -> WeaponryRetrievalContext:
    records = _target_document_records(kb_service, architecture_id)
    target_file_names: Set[str] = set()
    target_doc_paths: Set[str] = set()
    source_original_names: Dict[str, str] = {}
    original_names: Set[str] = set()
    for record in records:
        file_name = str(record.get("file_name") or "")
        original_name = str(record.get("original_name") or "") or file_name
        doc_path = str(record.get("doc_path") or "")
        anything_doc_id = str(record.get("anything_doc_id") or "")

        if original_name:
            original_names.add(original_name)

        for value in (file_name, original_name):
            value = _normalize_source_name(value)
            if value:
                target_file_names.add(value)

        if doc_path:
            target_doc_paths.add(doc_path.replace("\\", "/"))

        aliases = [
            file_name,
            original_name,
            doc_path,
            Path(doc_path.replace("\\", "/")).name if doc_path else "",
            anything_doc_id,
            f"{anything_doc_id}.json" if anything_doc_id else "",
            f"custom-documents/{anything_doc_id}.json" if anything_doc_id else "",
        ]
        _add_source_original_name_mapping(source_original_names, aliases, original_name)

    workspace_docs = _list_workspace_documents(client, workspace_slug, user_id=user_id)
    term_doc_paths = [
        _workspace_document_path(doc)
        for doc in workspace_docs
        if _workspace_document_path(doc) and _is_terms_source_name(_workspace_document_title(doc))
    ]

    if not term_doc_paths:
        term_doc_paths = _upload_local_terms_if_needed(client, user_id=user_id)

    terms_workspace_slug = _ensure_terms_workspace(client, term_doc_paths, user_id=user_id)

    removed_term_paths: List[str] = []
    if term_doc_paths:
        target_term_paths = [
            path
            for path in term_doc_paths
            if any(path == _workspace_document_path(doc) for doc in workspace_docs)
        ]
        if target_term_paths:
            if client.update_embeddings_batch(workspace_slug, deletes=target_term_paths, user_id=user_id):
                removed_term_paths = target_term_paths
                logger.info("已从目标 workspace 临时移除术语文档: workspace=%s count=%d", workspace_slug, len(removed_term_paths))
            else:
                logger.warning("从目标 workspace 临时移除术语文档失败: workspace=%s", workspace_slug)

    return WeaponryRetrievalContext(
        target_file_names=target_file_names,
        target_doc_paths=target_doc_paths,
        source_original_names=source_original_names,
        single_target_original_name=next(iter(original_names)) if len(original_names) == 1 else "",
        terms_workspace_slug=terms_workspace_slug,
        target_workspace_term_doc_paths=removed_term_paths,
    )


def _restore_target_workspace_terms(
    client: AnythingLLMClient,
    workspace_slug: str,
    context: Optional[WeaponryRetrievalContext],
    user_id: int = 1,
) -> None:
    if not context or not context.target_workspace_term_doc_paths:
        return
    if client.update_embeddings_batch(workspace_slug, adds=context.target_workspace_term_doc_paths, user_id=user_id):
        logger.info(
            "已恢复目标 workspace 中的术语文档: workspace=%s count=%d",
            workspace_slug,
            len(context.target_workspace_term_doc_paths),
        )
    else:
        logger.warning("恢复目标 workspace 术语文档失败: workspace=%s", workspace_slug)


def _map_source_to_analyse_data_source(source: Dict[str, Any], text_response: str = "") -> Dict[str, Any]:
    """将 AnythingLLM 的 source 对象映射为甲方 analyseDataSource 格式。

    每条记录以检索来源片段为单位组织：
    - content: LLM 解析出的内容（text_response）
    - source: 检索片段的原文文本（不同来源片段可能不一样）
    - time: 得到解析结果的时间
    - translate: 对原文片段的翻译
    """
    chunk_text = _strip_document_metadata(source.get("text", ""))
    return {
        "content": text_response,
        "source": chunk_text,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "translate": _translate_if_needed(chunk_text),
    }


def _build_analyse_data_sources(sources: List[Dict[str, Any]], text_response: str = "") -> List[Dict[str, Any]]:
    """将 sources 列表转换为 analyseDataSource 列表并按 score 降序排列。

    每个检索到的相关来源片段都作为一条独立记录。
    """
    scored = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        mapped = _map_source_to_analyse_data_source(src, text_response=text_response)
        score = 0.0
        try:
            score = float(src.get("score", 0))
        except (TypeError, ValueError):
            pass
        scored.append((score, mapped))
    scored.sort(key=lambda x: x[0], reverse=True)
    res = [item for _, item in scored]
    if not res:
        # 甲方接口要求：无来源时返回空内容对象
        return [_map_source_to_analyse_data_source({}, text_response=text_response)]
    return res


# ---------------------------------------------------------------------------
# 进度发布
# ---------------------------------------------------------------------------

def _publish_progress(
    progress_hub: LLMProgressHub,
    architecture_id: str,
    progress: float,
) -> None:
    progress_hub.publish(
        "weaponry",
        architecture_id,
        {
            "businessType": "weaponry",
            "data": {"architectureId": architecture_id, "progress": progress},
        },
    )


# ---------------------------------------------------------------------------
# 回调 payload 构建
# ---------------------------------------------------------------------------

def _build_weaponry_callback_payload(
    architecture_id: int,
    field_list: List[Dict[str, Any]],
    status: str,
    msg: str = "",
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "status": status,
        "architectureId": architecture_id,
    }
    if status == "2":
        data["weaponryTemplateFieldList"] = field_list
    return {
        "businessType": "weaponry",
        "data": data,
        "msg": msg or ("解析成功" if status == "2" else "解析失败"),
    }


# ---------------------------------------------------------------------------
# 展开字段列表，计数所有需要查询的原子字段
# ---------------------------------------------------------------------------

def _count_query_fields(field_list: List[Dict[str, Any]]) -> int:
    """统计需要 RAG 查询的任务数（INPUT 算 1，TABLE 按整表算 1）。"""
    count = 0
    for field in field_list:
        if field.get("fieldType") == "TABLE":
            count += 1
        else:
            count += 1
    return count


# ---------------------------------------------------------------------------
# 单字段 RAG 查询
# ---------------------------------------------------------------------------

def _query_input_field(
    client: AnythingLLMClient,
    workspace_slug: str,
    thread_slug: str,
    field: Dict[str, Any],
    user_id: int = 1,
    retrieval_context: Optional[WeaponryRetrievalContext] = None,
) -> Dict[str, Any]:
    """查询 INPUT 类型字段并返回填充后的字段对象。"""
    field_name = field.get("fieldName", "")
    field_desc = field.get("fieldDescription", "")

    # 步骤 1：目标 PDF 检索；术语规则辅助上下文由环境变量控制。
    prompt = build_input_field_prompt(field_name, field_desc)
    vs_results = _vector_search_with_top_n(
        client,
        workspace_slug,
        prompt,
        top_n=TARGET_EVIDENCE_TOP_N,
        user_id=user_id,
    )
    vs_results = [
        chunk
        for chunk in vs_results
        if _is_target_source(_extract_chunk_source_name(chunk), retrieval_context)
    ]

    terms_rule_context = ""
    if _terms_rule_context_enabled() and retrieval_context and retrieval_context.terms_workspace_slug:
        terms_query = _build_terms_rule_query(field_name, field_desc)
        term_results = _vector_search_with_top_n(
            client,
            retrieval_context.terms_workspace_slug,
            terms_query,
            top_n=TERMS_RULE_TOP_N,
            user_id=user_id,
        )
        terms_rule_context = _format_terms_rule_context(term_results)

    filled = dict(field)
    if not vs_results:
        logger.info("字段 [%s] 向量搜索无匹配，使用空来源", field_name)
        filled["analyseData"] = ""
        filled["analyseDataSource"] = _build_analyse_data_sources([], text_response="")
        return filled

    analyse_mode = os.getenv("WEAPONRY_ANALYSE_MODE", "1").strip()

    if analyse_mode == "2":
        # 模式 2：按文件聚合
        file_chunks = defaultdict(list)
        file_max_score = defaultdict(float)

        for chunk in vs_results:
            chunk_text = _strip_document_metadata(chunk.get("text", ""))
            if not chunk_text:
                continue
            
            file_name = _extract_chunk_source_name(chunk)
            if not file_name:
                file_name = _fallback_target_file_name(retrieval_context)

            if not file_name:
                raise ValueError("未能从文档片段中提取出明确的文件名。")
            
            file_chunks[file_name].append(chunk_text)
            score = _get_score(chunk)
            if score > file_max_score[file_name]:
                file_max_score[file_name] = score

        # 按照文件最高置信度降序排序
        sorted_files = sorted(file_chunks.keys(), key=lambda f: file_max_score[f], reverse=True)
        
        data_sources = []
        first_valid_content = ""

        for file_name in sorted_files:
            chunks = _limit_chunks_for_prompt(file_chunks[file_name])
            if not chunks:
                continue
            chunk_prompt = build_multi_chunk_based_field_prompt(
                field_name,
                chunks,
                field_desc,
                terms_rule_context=terms_rule_context,
            )
            
            result = _send_prompt_to_isolated_thread(
                client,
                workspace_slug,
                thread_slug,
                chunk_prompt,
                user_id=user_id,
                mode="chat",
            )
            
            if not result:
                continue
                
            text_response = result.get("textResponse", "").strip()
            
            if "未找到" in text_response or not text_response:
                continue

            # 组装针对文件的 data_source，此时 translate 为 content 的翻译
            original_source_name = _resolve_original_source_name(file_name, retrieval_context)
            mapped_source = {
                "content": text_response,
                "source": original_source_name,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "translate": _translate_if_needed(text_response),
            }
            data_sources.append(mapped_source)
            
            if not first_valid_content:
                first_valid_content = text_response

        if not data_sources:
            logger.info("字段 [%s] (模式2) 提取成功: 所有相关文件均未能提取出有效信息", field_name)
            filled["analyseData"] = ""
            filled["analyseDataSource"] = _build_analyse_data_sources([], text_response="")
            return filled

        preview_text = first_valid_content.replace('\n', ' ')
        if len(preview_text) > 40:
            preview_text = preview_text[:40] + "..."
            
        logger.info("字段 [%s] (模式2) 提取成功: %s (有效文件数: %d)", field_name, preview_text, len(data_sources))
        
        filled["analyseData"] = first_valid_content
        filled["analyseDataSource"] = data_sources
        return filled

    else:
        # 模式 1：按 Chunk 提问 (现存逻辑)
        sorted_chunks = sorted(vs_results, key=_get_score, reverse=True)

        data_sources = []
        first_valid_content = ""

        for chunk in sorted_chunks:
            chunk_text = _strip_document_metadata(chunk.get("text", ""))
            if not chunk_text:
                continue
            chunk_text = _limit_chunks_for_prompt([chunk_text])[0]
                
            chunk_prompt = build_chunk_based_field_prompt(
                field_name,
                chunk_text,
                field_desc,
                terms_rule_context=terms_rule_context,
            )
            
            # 步骤 2：对每个 Chunk，使用 chat 模式向模型提问
            result = _send_prompt_to_isolated_thread(
                client,
                workspace_slug,
                thread_slug,
                chunk_prompt,
                user_id=user_id,
                mode="chat",
            )
            
            if not result:
                continue
                
            text_response = result.get("textResponse", "").strip()
            
            # 如果 LLM 回答"未找到"则过滤掉
            if "未找到" in text_response or not text_response:
                continue
                
            # 组装这一条来源
            mapped_source = _map_source_to_analyse_data_source(chunk, text_response=text_response)
            data_sources.append(mapped_source)
            
            if not first_valid_content:
                first_valid_content = text_response

        if not data_sources:
            logger.info("字段 [%s] 提取成功: 所有相关 Chunk 均未能提取出有效信息", field_name)
            filled["analyseData"] = ""
            filled["analyseDataSource"] = _build_analyse_data_sources([], text_response="")
            return filled

        preview_text = first_valid_content.replace('\n', ' ')
        if len(preview_text) > 40:
            preview_text = preview_text[:40] + "..."
            
        logger.info("字段 [%s] 提取成功: %s (有效 Chunk 数: %d)", field_name, preview_text, len(data_sources))
        
        filled["analyseData"] = first_valid_content
        filled["analyseDataSource"] = data_sources
        return filled


# ---------------------------------------------------------------------------
# TABLE 类型字段处理
# ---------------------------------------------------------------------------

def _extract_table_column_defs(field: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从请求中的 tableFieldList 提取列模板定义。"""
    columns: List[Dict[str, Any]] = []
    seen_names: Set[str] = set()
    for row in field.get("tableFieldList") or []:
        if not isinstance(row, list):
            continue
        for cell in row:
            if not isinstance(cell, dict):
                continue
            column_name = str(cell.get("fieldName") or "").strip()
            if not column_name or column_name in seen_names:
                continue
            column = dict(cell)
            column["fieldName"] = column_name
            column.setdefault("fieldType", "INPUT")
            column.pop("analyseData", None)
            column.pop("analyseDataSource", None)
            columns.append(column)
            seen_names.add(column_name)
    return columns


def _build_table_retrieval_query(field: Dict[str, Any], column_defs: List[Dict[str, Any]]) -> str:
    table_name = str(field.get("fieldName") or "表格").strip()
    table_desc = str(field.get("fieldDescription") or "").strip()
    column_parts = []
    for column in column_defs:
        column_name = str(column.get("fieldName") or "").strip()
        column_desc = str(column.get("fieldDescription") or "").strip()
        if not column_name:
            continue
        if column_desc:
            column_parts.append(f"{column_name}（{column_desc}）")
        else:
            column_parts.append(column_name)
    desc_part = f"\n表格说明：{table_desc}" if table_desc else ""
    columns_part = "、".join(column_parts)
    return (
        f"请检索与表格“{table_name}”相关的多行数据。{desc_part}\n"
        f"每一行应对应一个独立对象、部件、型号或记录；需要关注的列包括：{columns_part}。"
    )


def _build_table_terms_rule_query(field: Dict[str, Any], column_defs: List[Dict[str, Any]]) -> str:
    table_name = str(field.get("fieldName") or "表格").strip()
    table_desc = str(field.get("fieldDescription") or "").strip()
    column_descs = []
    for column in column_defs:
        column_name = str(column.get("fieldName") or "").strip()
        column_desc = str(column.get("fieldDescription") or "").strip()
        if not column_name:
            continue
        column_descs.append(f"{column_name}: {column_desc}" if column_desc else column_name)
    desc_parts = []
    if table_desc:
        desc_parts.append(table_desc)
    if column_descs:
        desc_parts.append("列定义：" + "；".join(column_descs))
    return _build_terms_rule_query(table_name, "\n".join(desc_parts))


def _normalize_json_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def _clean_table_json_response(text: str) -> str:
    cleaned = str(text or "").strip()
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>")[-1]
    cleaned = cleaned.replace("<think>", "").strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*)", cleaned, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return cleaned


def _json_substring(text: str, start_char: str, end_char: str) -> str:
    start = text.find(start_char)
    end = text.rfind(end_char)
    if start == -1 or end == -1 or end <= start:
        return ""
    return text[start:end + 1]


def _load_table_json_response(text: str) -> Any:
    cleaned = _clean_table_json_response(text)
    candidates = [
        cleaned,
        _json_substring(cleaned, "[", "]"),
        _json_substring(cleaned, "{", "}"),
    ]
    seen: Set[str] = set()
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _normalize_table_cell_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        values = [_normalize_table_cell_value(item) for item in value]
        return ", ".join(item for item in values if item)
    if isinstance(value, dict):
        if not value:
            return ""
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value).strip()
    if text.lower() in {"null", "none", "nan"}:
        return ""
    if text in {"未找到", "未检索到", "无明确依据", "未知", "不详"}:
        return ""
    return text


def _lookup_table_value(row: Dict[str, Any], field_name: str) -> Any:
    if field_name in row:
        return row[field_name]
    target_key = _normalize_json_key(field_name)
    for key, value in row.items():
        if _normalize_json_key(key) == target_key:
            return value
    return None


def _parse_table_json_rows(text: str, column_defs: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    parsed = _load_table_json_response(text)
    if parsed is None:
        return []

    row_items: List[Any]
    if isinstance(parsed, list):
        row_items = parsed
    elif isinstance(parsed, dict):
        row_items = []
        for key in ("rows", "data", "items", "result", "tableRows", "tableFieldList"):
            value = parsed.get(key)
            if isinstance(value, list):
                row_items = value
                break
        if not row_items:
            row_items = [parsed]
    else:
        return []

    column_names = [str(column.get("fieldName") or "").strip() for column in column_defs]
    column_names = [name for name in column_names if name]
    parsed_rows: List[Dict[str, str]] = []

    for item in row_items:
        row: Dict[str, str] = {}
        if isinstance(item, list):
            for index, column_name in enumerate(column_names):
                value = item[index] if index < len(item) else None
                row[column_name] = _normalize_table_cell_value(value)
        elif isinstance(item, dict):
            row_key = (
                item.get("__rowKey")
                or item.get("rowKey")
                or item.get("row_key")
                or item.get("行标识")
                or item.get("主键")
            )
            row["__rowKey"] = _normalize_table_cell_value(row_key)
            for column_name in column_names:
                row[column_name] = _normalize_table_cell_value(_lookup_table_value(item, column_name))
        else:
            continue

        if any(row.get(column_name) for column_name in column_names):
            parsed_rows.append(row)
        if len(parsed_rows) >= MAX_TABLE_ROWS:
            break

    return parsed_rows


def _normalize_table_row_identity(value: str) -> str:
    text = _normalize_table_cell_value(value)
    text = re.sub(r"\s+", "", text).lower()
    return text.strip("，,;；。.")


def _table_row_identity(row: Dict[str, str], column_defs: List[Dict[str, Any]], fallback_index: int) -> str:
    explicit = _normalize_table_row_identity(row.get("__rowKey", ""))
    if explicit:
        return explicit

    preferred_tokens = ("名称", "型号", "编号", "代号", "标识", "类型")
    for column in column_defs:
        column_name = str(column.get("fieldName") or "").strip()
        if not column_name or not any(token in column_name for token in preferred_tokens):
            continue
        identity = _normalize_table_row_identity(row.get(column_name, ""))
        if identity:
            return identity

    for column in column_defs:
        column_name = str(column.get("fieldName") or "").strip()
        identity = _normalize_table_row_identity(row.get(column_name, ""))
        if identity:
            return identity

    return f"row-{fallback_index}"


def _build_table_cell_source(value: str, source_name: str) -> Dict[str, Any]:
    return {
        "content": value,
        "source": source_name,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "translate": _translate_if_needed(value),
    }


def _append_unique_source(sources: List[Dict[str, Any]], source: Dict[str, Any]) -> None:
    source_key = (source.get("content", ""), source.get("source", ""))
    for existing in sources:
        if (existing.get("content", ""), existing.get("source", "")) == source_key:
            return
    sources.append(source)


def _merge_table_rows(row_results: List[Dict[str, Any]], column_defs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    by_identity: Dict[str, Dict[str, Any]] = {}

    for index, row_result in enumerate(row_results):
        row = row_result.get("row") or {}
        if not isinstance(row, dict):
            continue
        identity = _table_row_identity(row, column_defs, index)
        item = by_identity.get(identity)
        if item is None:
            item = {"values": {}, "sources": defaultdict(list)}
            by_identity[identity] = item
            merged.append(item)

        source_name = str(row_result.get("source") or "")
        for column in column_defs:
            column_name = str(column.get("fieldName") or "").strip()
            if not column_name:
                continue
            value = _normalize_table_cell_value(row.get(column_name))
            if not value:
                continue
            if not item["values"].get(column_name):
                item["values"][column_name] = value
            _append_unique_source(
                item["sources"][column_name],
                _build_table_cell_source(value, source_name),
            )

    return merged[:MAX_TABLE_ROWS]


def _assemble_table_rows(merged_rows: List[Dict[str, Any]], column_defs: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    assembled_rows: List[List[Dict[str, Any]]] = []
    for merged_row in merged_rows:
        row: List[Dict[str, Any]] = []
        for column in column_defs:
            column_name = str(column.get("fieldName") or "").strip()
            if not column_name:
                continue
            cell = dict(column)
            value = str(merged_row.get("values", {}).get(column_name) or "")
            sources = list(merged_row.get("sources", {}).get(column_name) or [])
            cell["analyseData"] = value
            cell["analyseDataSource"] = sources if sources else _build_analyse_data_sources([], text_response="")
            row.append(cell)
        if any(str(cell.get("analyseData") or "").strip() for cell in row):
            assembled_rows.append(row)
    return assembled_rows


def _query_table_field(
    client: AnythingLLMClient,
    workspace_slug: str,
    thread_slug: str,
    field: Dict[str, Any],
    user_id: int = 1,
    on_cell_done: Optional[Any] = None,
    retrieval_context: Optional[WeaponryRetrievalContext] = None,
) -> Dict[str, Any]:
    """查询 TABLE 类型字段：按整表抽取多行结果。"""
    def _finish(filled_field: Dict[str, Any]) -> Dict[str, Any]:
        if on_cell_done:
            on_cell_done()
        return filled_field

    template_rows = field.get("tableFieldList") or []
    if not template_rows:
        filled = dict(field)
        filled["tableFieldList"] = []
        return _finish(filled)

    column_defs = _extract_table_column_defs(field)
    if not column_defs:
        filled = dict(field)
        filled["tableFieldList"] = []
        return _finish(filled)

    table_name = field.get("fieldName", "表格")
    logger.info("  -> 开始处理表格 [%s]，列数: %d", table_name, len(column_defs))

    retrieval_query = _build_table_retrieval_query(field, column_defs)
    vs_results = _vector_search_with_top_n(
        client,
        workspace_slug,
        retrieval_query,
        top_n=TABLE_EVIDENCE_TOP_N,
        user_id=user_id,
    )
    vs_results = [
        chunk
        for chunk in vs_results
        if _is_target_source(_extract_chunk_source_name(chunk), retrieval_context)
    ]

    if not vs_results:
        logger.info("表格 [%s] 向量搜索无匹配，返回空表格", table_name)
        filled = dict(field)
        filled["tableFieldList"] = []
        return _finish(filled)

    terms_rule_context = ""
    if _terms_rule_context_enabled() and retrieval_context and retrieval_context.terms_workspace_slug:
        term_results = _vector_search_with_top_n(
            client,
            retrieval_context.terms_workspace_slug,
            _build_table_terms_rule_query(field, column_defs),
            top_n=TERMS_RULE_TOP_N,
            user_id=user_id,
        )
        terms_rule_context = _format_terms_rule_context(term_results)

    file_chunks: Dict[str, List[str]] = defaultdict(list)
    file_max_score: Dict[str, float] = defaultdict(float)
    for chunk in sorted(vs_results, key=_get_score, reverse=True):
        chunk_text = _strip_document_metadata(chunk.get("text", ""))
        if not chunk_text:
            continue
        file_name = _extract_chunk_source_name(chunk) or _fallback_target_file_name(retrieval_context)
        if not file_name:
            raise ValueError("未能从表格文档片段中提取出明确的文件名。")
        file_chunks[file_name].append(chunk_text)
        file_max_score[file_name] = max(file_max_score[file_name], _get_score(chunk))

    row_results: List[Dict[str, Any]] = []
    sorted_files = sorted(file_chunks.keys(), key=lambda file_name: file_max_score[file_name], reverse=True)
    for file_name in sorted_files:
        chunks = _limit_chunks_for_prompt(file_chunks[file_name])
        if not chunks:
            continue
        prompt = build_table_extraction_prompt(
            str(table_name),
            str(field.get("fieldDescription") or ""),
            column_defs,
            chunks,
            terms_rule_context=terms_rule_context,
        )
        result = _send_prompt_to_isolated_thread(
            client,
            workspace_slug,
            thread_slug,
            prompt,
            user_id=user_id,
            mode="chat",
        )
        if not result:
            continue

        text_response = str(result.get("textResponse", "")).strip()
        rows = _parse_table_json_rows(text_response, column_defs)
        if not rows:
            if text_response and "未找到" not in text_response and text_response != "[]":
                logger.warning("表格 [%s] 的模型响应无法解析为有效行: %s", table_name, text_response[:200])
            continue

        original_source_name = _resolve_original_source_name(file_name, retrieval_context)
        for row in rows:
            row_results.append(
                {
                    "row": row,
                    "source": original_source_name,
                    "score": file_max_score[file_name],
                }
            )

    filled = dict(field)
    merged_rows = _merge_table_rows(row_results, column_defs)
    filled["tableFieldList"] = _assemble_table_rows(merged_rows, column_defs)
    logger.info("表格 [%s] 提取完成: 行数=%d", table_name, len(filled["tableFieldList"]))
    return _finish(filled)


# ---------------------------------------------------------------------------
# 主任务入口
# ---------------------------------------------------------------------------

def run_weaponry_task(
    *,
    task_service: LLMTaskService,
    kb_service: DatabaseService,
    progress_hub: LLMProgressHub,
    request_payload: Dict[str, Any],
    callback_url: str,
    callback_timeout: float,
) -> None:
    """后台线程入口：执行 weaponry 解析任务。"""

    params = request_payload.get("params", {})
    architecture_id = params.get("architectureId")
    architecture_id_str = str(architecture_id)
    field_list: List[Dict[str, Any]] = params.get("weaponryTemplateFieldList", [])

    try:
        # ─── 阶段 1：查找 Workspace ───
        task_service.update_task_progress(
            "weaponry", architecture_id_str,
            progress=0.05, message="正在查找知识库", status="1",
        )
        _publish_progress(progress_hub, architecture_id_str, 0.05)

        workspace_slug = kb_service.get_workspace_slug(architecture_id)
        if not workspace_slug:
            logger.warning("architectureId=%s 无对应 Workspace，标记失败", architecture_id)
            _fail_task(
                task_service, progress_hub, architecture_id, architecture_id_str,
                callback_url, callback_timeout,
                msg=f"architectureId={architecture_id} 对应的知识库不存在",
            )
            return

        # ─── 阶段 2：创建临时 Thread ───
        task_service.update_task_progress(
            "weaponry", architecture_id_str,
            progress=0.10, message="正在创建检索会话",
        )
        _publish_progress(progress_hub, architecture_id_str, 0.10)

        client = AnythingLLMClient(load_anythingllm_config())
        thread_name = f"weaponry-{architecture_id}-{int(time.time() * 1000)}"
        thread_info = client.create_thread(workspace_slug, thread_name, user_id=1)
        if not thread_info:
            _fail_task(
                task_service, progress_hub, architecture_id, architecture_id_str,
                callback_url, callback_timeout,
                msg="创建检索会话失败",
            )
            return
        thread_slug = client.extract_thread_slug(thread_info) or thread_info.get("id")
        if not thread_slug:
            _fail_task(
                task_service, progress_hub, architecture_id, architecture_id_str,
                callback_url, callback_timeout,
                msg="获取检索会话标识失败",
            )
            return

        retrieval_context = _prepare_retrieval_context(
            client,
            kb_service,
            architecture_id,
            workspace_slug,
            user_id=1,
        )

        # ─── 阶段 3：逐字段查询 ───
        total_query_fields = _count_query_fields(field_list)
        completed_fields = 0

        def _update_field_progress():
            nonlocal completed_fields
            completed_fields += 1
            # 字段查询占总进度的 0.15 ~ 0.90 区间
            progress = 0.15 + (completed_fields / max(total_query_fields, 1)) * 0.75
            task_service.update_task_progress(
                "weaponry", architecture_id_str,
                progress=progress,
                message=f"正在提取字段 ({completed_fields}/{total_query_fields})",
            )
            _publish_progress(progress_hub, architecture_id_str, progress)

        result_fields: List[Dict[str, Any]] = []
        try:
            for field in field_list:
                field_type = field.get("fieldType", "INPUT")
                field_name = field.get("fieldName", "unknown")
                logger.info("正在处理字段: %s (%s)", field_name, field_type)

                if field_type == "TABLE":
                    filled = _query_table_field(
                        client,
                        workspace_slug,
                        thread_slug,
                        field,
                        user_id=1,
                        on_cell_done=_update_field_progress,
                        retrieval_context=retrieval_context,
                    )
                else:
                    filled = _query_input_field(
                        client,
                        workspace_slug,
                        thread_slug,
                        field,
                        user_id=1,
                        retrieval_context=retrieval_context,
                    )
                    _update_field_progress()

                result_fields.append(filled)
        finally:
            _restore_target_workspace_terms(client, workspace_slug, retrieval_context, user_id=1)

        # ─── 阶段 4：删除 Thread ───
        task_service.update_task_progress(
            "weaponry", architecture_id_str,
            progress=0.92, message="正在清理检索会话",
        )
        _publish_progress(progress_hub, architecture_id_str, 0.92)

        if not client.delete_thread(workspace_slug, thread_slug, user_id=1):
            logger.warning("删除 Thread %s 失败（不影响结果）", thread_slug)

        logger.info("武器装备提取任务完成: architectureId=%s", architecture_id)

        # ─── 阶段 5：组装回调并发送 ───
        callback_payload = _build_weaponry_callback_payload(
            architecture_id, result_fields, status="2", msg="解析成功",
        )
        task_service.mark_business_result(
            "weaponry", architecture_id_str,
            callback_payload, status="2", message="解析完成",
        )
        _publish_progress(progress_hub, architecture_id_str, 1.0)

        if callback_url:
            if post_callback_payload(
                callback_url,
                callback_payload,
                timeout=callback_timeout,
                callback_context={"businessType": "weaponry", "architectureId": architecture_id},
            ):
                task_service.mark_callback_success("weaponry", architecture_id_str)
                logger.info("回调结果提交成功: architectureId=%s", architecture_id)
            else:
                task_service.mark_callback_failed("weaponry", architecture_id_str, "callback failed")
                logger.warning("回调结果提交失败: architectureId=%s", architecture_id)

    except Exception as e:
        logger.exception("武器装备提取任务异常: architectureId=%s, error=%s", architecture_id, e)
        _fail_task(
            task_service, progress_hub, architecture_id, architecture_id_str,
            callback_url, callback_timeout,
            msg=f"解析异常: {e}",
        )


def _fail_task(
    task_service: LLMTaskService,
    progress_hub: LLMProgressHub,
    architecture_id: int,
    architecture_id_str: str,
    callback_url: str,
    callback_timeout: float,
    msg: str = "解析失败",
) -> None:
    """统一的任务失败处理。"""
    callback_payload = _build_weaponry_callback_payload(
        architecture_id, [], status="3", msg=msg,
    )
    task_service.mark_business_result(
        "weaponry", architecture_id_str,
        callback_payload, status="3", message=msg,
    )
    _publish_progress(progress_hub, architecture_id_str, 1.0)

    if callback_url:
        if post_callback_payload(
            callback_url,
            callback_payload,
            timeout=callback_timeout,
            callback_context={"businessType": "weaponry", "architectureId": architecture_id},
        ):
            task_service.mark_callback_success("weaponry", architecture_id_str)
        else:
            task_service.mark_callback_failed("weaponry", architecture_id_str, "callback failed")
