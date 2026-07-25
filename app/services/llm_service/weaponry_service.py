from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set
import os
from collections import defaultdict

from app.services.utils.anythingllm_client import AnythingLLMClient
from app.services.core.config import load_anythingllm_config

from app.services.core.database import DatabaseService
from app.services.utils.callback_client import post_callback_payload
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.task_service import LLMTaskService
from app.services.llm_service.translation_service import get_translation_service
from app.modules.weaponry.domain import (
    AuxiliaryGuidance,
    DeprecatedWeaponryModeError,
    MergedTableRow,
    ParsedTableRow,
    RetrievalColumn,
    RetrievalField,
    SelectedEvidence,
    SourceNameMapping,
    TableRowResult,
    WeaponryAnalyseDataSource,
    WeaponryDomainValidationError,
    WeaponryFieldSpecification,
    assemble_table_rows,
    build_input_extraction_prompt,
    build_retrieval_query,
    build_table_extraction_prompt,
    is_terms_source_name as _domain_is_terms_source_name,
    normalize_evidence_rows,
    merge_table_rows,
    normalize_architecture_id_value,
    normalize_evidence_text,
    normalize_source_name as _domain_normalize_source_name,
    normalize_table_cell_value as _domain_normalize_table_cell_value,
    parse_table_json_rows,
    resolve_legacy_extraction_strategy,
    source_lookup_keys as _domain_source_lookup_keys,
)

logger = logging.getLogger(__name__)


TARGET_EVIDENCE_TOP_N = 8
TABLE_EVIDENCE_TOP_N = 16
TERMS_RULE_TOP_N = 3
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
    source_file_names: Optional[Dict[str, str]] = None
    single_target_original_name: str = ""
    single_target_file_name: str = ""
    terms_workspace_slug: Optional[str] = None
    target_workspace_term_doc_paths: Optional[List[str]] = None

    def __post_init__(self) -> None:
        if self.source_original_names is None:
            self.source_original_names = {}
        if self.source_file_names is None:
            self.source_file_names = {}
        if self.target_workspace_term_doc_paths is None:
            self.target_workspace_term_doc_paths = []


class WeaponrySelectedDocumentError(ValueError):
    """指定范围的知识库文档无法形成确定检索快照时抛出。"""


class WeaponrySelectedDocumentNotFoundError(WeaponrySelectedDocumentError):
    """请求文件尚未进入本地知识库映射时抛出。"""


class WeaponrySelectedDocumentAmbiguityError(WeaponrySelectedDocumentError):
    """同一请求无法唯一定位一份外部知识库文档时抛出。"""


@dataclass(frozen=True)
class WeaponrySelectedDocument:
    """一次 weaponry 任务中被显式选中的不可变文档快照。

    ``filePathList`` 只携带哈希文件名，不能携带来源分类或 AnythingLLM 外部位置。
    因此必须在路由受理阶段将其解析为此快照，再持久化并交给后台线程使用，避免文件
    重分类、重新解析或删除后，异步任务重新查询到另一份同名文档。该对象只描述内部
    任务输入，绝不加入 HTTP 请求或回调字段。
    """

    file_name: str
    original_name: str
    source_architecture_id: int
    doc_path: str
    ingested_file_name: str
    anything_doc_id: str = ""

    def __post_init__(self) -> None:
        """校验可用于临时 workspace 绑定和来源映射的最小身份集。"""
        file_name = str(self.file_name or "").strip()
        if not file_name:
            raise ValueError("file_name不能为空")
        # 业务原始名必须在请求受理后原样流转。仅借助 strip 判断空值，不能修改实际
        # 保存值；否则 analyseDataSource.source 无法严格回显 originalFileName 原值。
        requested_original_name = str(self.original_name or "")
        original_name = (
            requested_original_name
            if requested_original_name.strip()
            else file_name
        )
        if (
            isinstance(self.source_architecture_id, bool)
            or not isinstance(self.source_architecture_id, int)
            or self.source_architecture_id < 1
        ):
            raise ValueError("source_architecture_id必须是正整数")
        doc_path = str(self.doc_path or "").strip()
        if not doc_path:
            raise ValueError("doc_path不能为空")
        ingested_file_name = (
            str(self.ingested_file_name or "")
            .replace("\\", "/")
            .rsplit("/", 1)[-1]
            .strip()
        )
        if not ingested_file_name or ingested_file_name in {".", ".."}:
            raise ValueError("ingested_file_name必须是有效文件名")

        object.__setattr__(self, "file_name", file_name)
        object.__setattr__(self, "original_name", original_name)
        object.__setattr__(self, "doc_path", doc_path)
        object.__setattr__(
            self,
            "anything_doc_id",
            str(self.anything_doc_id or "").strip(),
        )
        object.__setattr__(self, "ingested_file_name", ingested_file_name)

    def to_task_snapshot(self) -> Dict[str, Any]:
        """转换为可严格 JSON 持久化的任务快照，不包含供应商响应原文。"""
        return {
            "file_name": self.file_name,
            "original_name": self.original_name,
            "source_architecture_id": self.source_architecture_id,
            "doc_path": self.doc_path,
            "anything_doc_id": self.anything_doc_id,
            "ingested_file_name": self.ingested_file_name,
        }

    @classmethod
    def from_task_snapshot(
        cls,
        value: Mapping[str, Any],
    ) -> "WeaponrySelectedDocument":
        """从任务库读取内部快照，并重新执行边界校验。"""
        if not isinstance(value, Mapping):
            raise TypeError("weaponry任务文档快照必须是Mapping")
        raw_architecture_id = value.get("source_architecture_id")
        if isinstance(raw_architecture_id, bool):
            raise ValueError("source_architecture_id必须是正整数")
        try:
            source_architecture_id = int(raw_architecture_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("source_architecture_id必须是正整数") from exc
        return cls(
            file_name=str(value.get("file_name") or ""),
            original_name=str(value.get("original_name") or ""),
            source_architecture_id=source_architecture_id,
            doc_path=str(value.get("doc_path") or ""),
            anything_doc_id=str(value.get("anything_doc_id") or ""),
            ingested_file_name=str(value.get("ingested_file_name") or ""),
        )

    def to_document_record(self) -> Dict[str, Any]:
        """适配既有检索与溯源辅助函数所需的最小本地文档结构。"""
        return {
            "file_name": self.file_name,
            "original_name": self.original_name,
            "architecture_id": self.source_architecture_id,
            "doc_path": self.doc_path,
            "anything_doc_id": self.anything_doc_id,
            "ingested_file_name": self.ingested_file_name,
        }


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
        logger.warning("字段内容翻译失败: error_type=%s", type(e).__name__)
        return ""


# ---------------------------------------------------------------------------
# source 映射
# ---------------------------------------------------------------------------

def _strip_document_metadata(text: str) -> str:
    """兼容入口：转发到 1D 领域层的供应商包装清理规则。"""

    return normalize_evidence_text(str(text or ""))


def _normalize_source_name(name: str) -> str:
    """兼容入口：转发到供应商无关来源名纯规则。"""

    return _domain_normalize_source_name(str(name or ""))


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
    """兼容入口：调用确定性别名规则后返回遗留调用方所需集合。"""

    return set(_domain_source_lookup_keys(str(name or "")))


def _add_source_name_mapping(
    mapping: Dict[str, str],
    aliases: List[str],
    target_name: str,
) -> None:
    if not target_name:
        return
    for alias in aliases:
        for key in _source_lookup_keys(alias):
            mapping.setdefault(key, target_name)


def _resolve_original_source_name(
    source_name: str,
    context: Optional[WeaponryRetrievalContext],
) -> str:
    """将 Mode 2 的内部来源文件名映射为 documents.original_name。"""
    mapping = SourceNameMapping(
        original_names=tuple(
            (context.source_original_names or {}).items()
            if context
            else ()
        ),
        fallback_original_name=(
            context.single_target_original_name if context else ""
        ),
    )
    return mapping.resolve_original_name(str(source_name or ""))


def _resolve_hashed_source_name(
    source_name: str,
    context: Optional[WeaponryRetrievalContext],
) -> str:
    """将 AnythingLLM 来源标识映射为 documents.file_name（哈希文件名）。"""
    mapping = SourceNameMapping(
        file_names=tuple(
            (context.source_file_names or {}).items()
            if context
            else ()
        ),
        fallback_file_name=(context.single_target_file_name if context else ""),
    )
    return mapping.resolve_file_name(str(source_name or ""))


def _is_terms_source_name(source_name: str) -> bool:
    return _domain_is_terms_source_name(str(source_name or ""))


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
    """通过公开 Client 接口执行带结果上限的向量检索。

    业务层只表达查询文本和结果数量，不再了解 AnythingLLM URL、Header、Session 或
    ``topN`` 请求字段。兼容 Facade 负责把 ``top_n`` 转换为供应商协议，并维持失败时
    返回空列表的既有语义。
    """
    try:
        return client.vector_search(
            workspace_slug,
            query,
            user_id=user_id,
            top_n=top_n,
        )
    except Exception as exc:
        # 兼容测试 Fake 或尚未迁移实现抛出异常的场景，避免单字段检索中断整个任务。
        logger.error(
            "武器装备字段的向量检索失败: query_chars=%d top_n=%d error_type=%s",
            len(query or ""),
            top_n,
            type(exc).__name__,
        )
        return []


def _list_workspace_documents(
    client: AnythingLLMClient,
    workspace_slug: str,
    user_id: int = 1,
) -> List[Dict[str, Any]]:
    """通过公开 Client 接口读取工作区文档列表。

    AnythingLLM 的 ``workspace`` 包装结构和文档字段别名均由适配层处理，本函数只保留
    weaponry 后续术语文档识别所需的兼容字典。
    """
    try:
        return client.list_workspace_documents(workspace_slug, user_id=user_id)
    except Exception as exc:
        # Facade 正常情况下会返回空列表；此处继续保护测试 Fake 与迁移期替代实现。
        logger.error(
            "读取工作区文档列表失败: error_type=%s",
            type(exc).__name__,
        )
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


def _all_knowledge_document_records(
    kb_service: DatabaseService,
) -> List[Dict[str, Any]]:
    """读取全部本地知识库文档，并统一校验数据库服务返回契约。"""
    all_records = kb_service.list_document_records()
    # 数据库服务公开契约要求始终返回列表。此处保留边界校验，可以把未来实现回归或测试
    # 替身错误转换为带有明确上下文的异常，避免再次暴露难以定位的“NoneType 不可迭代”。
    # 不使用 ``all_records or []`` 静默降级，因为空列表与违反返回契约具有不同业务含义。
    if not isinstance(all_records, list):
        raise TypeError(
            "文档记录查询返回契约错误: "
            f"期望 list，实际为 {type(all_records).__name__}"
        )
    if any(not isinstance(record, dict) for record in all_records):
        raise TypeError("文档记录查询返回契约错误: 列表元素必须是dict")
    return all_records


def _target_document_records(
    kb_service: DatabaseService,
    architecture_id: int,
) -> List[Dict[str, Any]]:
    """返回空 ``filePathList`` 语义下当前类别的全部文档。"""
    records = [
        record
        for record in _all_knowledge_document_records(kb_service)
        if str(record.get("architecture_id")) == str(architecture_id)
    ]
    return records


def _document_record_path(record: Dict[str, Any]) -> str:
    doc_path = str(record.get("doc_path") or "").strip()
    if doc_path:
        return doc_path
    anything_doc_id = str(record.get("anything_doc_id") or "").strip()
    return f"custom-documents/{anything_doc_id}.json" if anything_doc_id else ""


def resolve_weaponry_selected_documents(
    kb_service: DatabaseService,
    selected_file_names: Sequence[str],
) -> tuple[WeaponrySelectedDocument, ...]:
    """将非空 ``filePathList`` 解析为跨分类的不可变知识库文档快照。

    甲方请求中只提供文件名，数据库却允许同名文件存在于多个分类。因此这里绝不能
    按数据库默认顺序任选一条记录：找不到返回“未解析”，存在多条记录或多个业务文件
    指向同一外部位置时返回“无法唯一溯源”。两类歧义都必须在后台线程启动前拒绝。
    """
    if isinstance(selected_file_names, (str, bytes)) or not isinstance(
        selected_file_names,
        Sequence,
    ):
        raise TypeError("selected_file_names必须是字符串序列")

    records_by_file_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in _all_knowledge_document_records(kb_service):
        file_name = str(record.get("file_name") or "").strip()
        if file_name:
            records_by_file_name[file_name].append(record)

    selected_documents: List[WeaponrySelectedDocument] = []
    selected_doc_paths: Dict[str, str] = {}
    for raw_file_name in selected_file_names:
        file_name = str(raw_file_name or "").strip()
        if not file_name:
            raise WeaponrySelectedDocumentError("filePathList中包含无效文件名")

        candidates = records_by_file_name.get(file_name, [])
        if not candidates:
            raise WeaponrySelectedDocumentNotFoundError(
                f"文件 {file_name} 尚未解析，无法用于知识谱系解析"
            )
        if len(candidates) != 1:
            raise WeaponrySelectedDocumentAmbiguityError(
                f"文件 {file_name} 在多个知识库分类中存在记录，无法唯一确定引用版本"
            )

        record = candidates[0]
        raw_architecture_id = record.get("architecture_id")
        if isinstance(raw_architecture_id, bool):
            raise WeaponrySelectedDocumentError(
                f"文件 {file_name} 的知识库分类记录无效"
            )
        try:
            source_architecture_id = int(raw_architecture_id)
        except (TypeError, ValueError) as exc:
            raise WeaponrySelectedDocumentError(
                f"文件 {file_name} 的知识库分类记录无效"
            ) from exc

        doc_path = _document_record_path(record)
        if not doc_path:
            raise WeaponrySelectedDocumentError(
                f"文件 {file_name} 缺少知识库文档位置"
            )
        existing_file_name = selected_doc_paths.get(doc_path)
        if existing_file_name and existing_file_name != file_name:
            raise WeaponrySelectedDocumentAmbiguityError(
                "选中文件指向同一知识库文档位置，无法唯一溯源"
            )
        selected_doc_paths[doc_path] = file_name
        selected_documents.append(
            WeaponrySelectedDocument(
                file_name=file_name,
                original_name=str(record.get("original_name") or file_name),
                source_architecture_id=source_architecture_id,
                doc_path=doc_path,
                anything_doc_id=str(record.get("anything_doc_id") or ""),
                ingested_file_name=str(record.get("ingested_file_name") or ""),
            )
        )

    logger.info(
        "已解析知识谱系跨分类选中文档快照: file_count=%d source_architecture_count=%d",
        len(selected_documents),
        len({item.source_architecture_id for item in selected_documents}),
    )
    return tuple(selected_documents)


def _load_persisted_weaponry_selected_documents(
    task_service: LLMTaskService,
    architecture_id: int,
    execution_id: str,
) -> tuple[WeaponrySelectedDocument, ...]:
    """按当前任务执行身份恢复已受理的跨分类文档快照。

    当前路由会把快照直接传给本进程后台线程；该恢复入口用于未来可靠调度器或进程重启后
    按持久化任务执行。读取时必须同时校验 ``execution_id``，避免同一类别新任务覆盖旧
    任务后，旧执行意外拿到新一轮的文档范围。
    """
    normalized_execution_id = str(execution_id or "").strip()
    if not normalized_execution_id:
        # 不能以当前 architectureId 查询结果兜底：同一类别的新任务可能已覆盖旧任务，
        # 此时兜底会让旧执行拿到新一轮选文范围，破坏任务隔离。
        raise ValueError("指定文件范围任务缺少execution_id")
    snapshots = task_service.get_weaponry_task_document_snapshots(
        architecture_id=architecture_id,
        execution_id=normalized_execution_id,
    )
    if not snapshots:
        raise ValueError("指定文件范围任务缺少已受理文档快照")
    selected_documents = tuple(
        WeaponrySelectedDocument.from_task_snapshot(snapshot)
        for snapshot in snapshots
    )
    logger.info(
        "已从任务库恢复知识谱系选中文档快照: architecture_id=%s execution_id=%s "
        "file_count=%d",
        architecture_id,
        normalized_execution_id,
        len(selected_documents),
    )
    return selected_documents


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


def _normalize_chunks_for_prompt(chunks: List[str]) -> List[str]:
    """清理旧链路 Chunk，但保留每条完整正文和全部已选 Evidence。

    这里不再执行单条、总量或单文档配额裁剪。记录数量与字符数便于联调时识别供应商
    上下文窗口错误，但日志不输出任何业务正文。
    """

    normalized = list(normalize_evidence_rows(chunks))
    logger.info(
        "武器谱 Prompt Evidence 已完整保留: row_count=%s total_chars=%s",
        len(normalized),
        sum(len(item) for item in normalized),
    )
    return normalized


def _legacy_prompt_evidence(
    document_key: str,
    rows: Sequence[str],
) -> tuple[SelectedEvidence, ...]:
    """把旧模式 2 已选中的行映射为新 Prompt 所需的强类型 Evidence。

    该适配只服务尚未切换的遗留执行链，不宣称已经通过生产 relevance profile。稳定的
    ``legacy-mode2-unprofiled`` 标识会阻止这些对象与未来 1D-3B 的生产 Selection 结果混用。
    """

    normalized_document_key = str(document_key or "").strip()
    if not normalized_document_key:
        raise WeaponryDomainValidationError("旧链路 Prompt 缺少 document_key")
    return tuple(
        SelectedEvidence(
            candidate_id=f"legacy-{index}",
            document_key=normalized_document_key,
            text=row,
            provider_rank=index,
            provider_score=1.0,
            score_profile_id="legacy-mode2-unprofiled",
            score_mode="score",
            original_index=index - 1,
        )
        for index, row in enumerate(rows, 1)
    )


def _legacy_guidance(value: str) -> tuple[AuxiliaryGuidance, ...]:
    """把遗留术语文本封装为通用辅助语境，Domain 不感知 workspace。"""

    text = str(value or "").strip()
    if not text:
        return ()
    return (AuxiliaryGuidance(guidance_id="legacy-terms-rules", text=text),)


def _require_file_aggregate_strategy() -> None:
    """拒绝已废弃模式 1；本函数不在字段间动态选择算法。"""

    raw_mode = os.getenv("WEAPONRY_ANALYSE_MODE")
    try:
        resolve_legacy_extraction_strategy(raw_mode)
    except DeprecatedWeaponryModeError:
        logger.error(
            "检测到已废弃的武器谱模式配置，任务拒绝执行: configured_mode=1"
        )
        raise
    except WeaponryDomainValidationError:
        logger.error(
            "检测到非法武器谱模式配置，任务拒绝执行: configured_mode_chars=%d",
            len(raw_mode or ""),
        )
        raise


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
            logger.warning("提取临时会话标识失败: error_type=%s", type(e).__name__)
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
                logger.warning(
                    "创建字段临时会话失败，改用父会话: error_type=%s",
                    type(e).__name__,
                )

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
                        logger.warning("删除字段临时会话失败，不影响已得到的字段结果")
                except Exception as e:
                    logger.warning(
                        "删除字段临时会话发生异常，不影响已得到的字段结果: error_type=%s",
                        type(e).__name__,
                    )

        if result:
            return result

        logger.warning(
            "字段提示词未得到有效响应，准备重试: attempt=%d/%d",
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
        logger.warning("无法创建或获取术语规则工作区")
        return None
    workspace_slug = workspace_info.get("slug") or str(workspace_info.get("id") or "")
    if not workspace_slug:
        logger.warning("术语规则工作区缺少有效标识")
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
            "复用已有术语规则工作区: term_document_count=%d",
            len(existing_paths_by_key),
        )
        return workspace_slug

    if not client.update_embeddings_batch(workspace_slug, adds=missing_doc_paths, user_id=user_id):
        logger.warning("向术语规则工作区加入术语文档失败")
        return None
    logger.info(
        "术语规则工作区已补充缺失文档: added_count=%d existing_count=%d",
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
            logger.warning("上传术语文件失败，已跳过该文件: file_name=%s", term_file.name)
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
    selected_documents: Optional[Sequence[WeaponrySelectedDocument]] = None,
) -> WeaponryRetrievalContext:
    # 指定范围时必须使用受理阶段冻结的文档快照，不能重新按 ``architecture_id`` 查库。
    # 否则同名文件重分类后会让临时 workspace 的真实文档与回调溯源记录产生分叉。
    if selected_documents:
        if any(
            not isinstance(item, WeaponrySelectedDocument)
            for item in selected_documents
        ):
            raise TypeError("selected_documents只能包含WeaponrySelectedDocument")
        records = [item.to_document_record() for item in selected_documents]
    else:
        records = _target_document_records(kb_service, architecture_id)
    target_file_names: Set[str] = set()
    target_doc_paths: Set[str] = set()
    source_original_names: Dict[str, str] = {}
    source_file_names: Dict[str, str] = {}
    original_names: Set[str] = set()
    file_names: Set[str] = set()
    for record in records:
        file_name = str(record.get("file_name") or "")
        original_name = str(record.get("original_name") or "") or file_name
        ingested_file_name = str(record.get("ingested_file_name") or "").strip()
        doc_path = str(record.get("doc_path") or "")
        anything_doc_id = str(record.get("anything_doc_id") or "")

        if original_name:
            original_names.add(original_name)
        if file_name:
            file_names.add(file_name)

        for value in (file_name, original_name, ingested_file_name):
            value = _normalize_source_name(value)
            if value:
                target_file_names.add(value)

        if not ingested_file_name:
            # 开发阶段不兼容旧数据，也不从 doc_path、AnythingLLM title 等位置猜测上传名。
            # 否则 MHTML 转换后的中间文件名会重新进入 callback，破坏业务原始名契约。
            logger.error(
                "知识谱系来源映射缺少实际上传文件名，拒绝执行并要求重新解析: "
                "architecture_id=%s file_name=%s",
                record.get("architecture_id"),
                file_name,
            )
            raise WeaponrySelectedDocumentError(
                "知识库文档缺少实际上传文件名，请重新执行文件解析后再进行知识谱系解析"
            )

        if doc_path:
            target_doc_paths.add(doc_path.replace("\\", "/"))

        aliases = [
            file_name,
            original_name,
            ingested_file_name,
            doc_path,
            Path(doc_path.replace("\\", "/")).name if doc_path else "",
            anything_doc_id,
            f"{anything_doc_id}.json" if anything_doc_id else "",
            f"custom-documents/{anything_doc_id}.json" if anything_doc_id else "",
        ]
        _add_source_name_mapping(source_original_names, aliases, original_name)
        _add_source_name_mapping(source_file_names, aliases, file_name)

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
                logger.info(
                    "已从目标工作区临时移除术语文档: removed_count=%d",
                    len(removed_term_paths),
                )
            else:
                logger.warning("从目标工作区临时移除术语文档失败")

    return WeaponryRetrievalContext(
        target_file_names=target_file_names,
        target_doc_paths=target_doc_paths,
        source_original_names=source_original_names,
        source_file_names=source_file_names,
        single_target_original_name=next(iter(original_names)) if len(original_names) == 1 else "",
        single_target_file_name=next(iter(file_names)) if len(file_names) == 1 else "",
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
            "已恢复目标工作区中的术语文档: restored_count=%d",
            len(context.target_workspace_term_doc_paths),
        )
    else:
        logger.warning("恢复目标工作区中的术语文档失败")


def _build_file_analyse_data_source(
    *,
    content: str,
    source: str,
    file_name: str,
    rows: List[str],
) -> Dict[str, Any]:
    """兼容入口：用 1D DTO 构造按文件聚合溯源对象。"""

    return WeaponryAnalyseDataSource(
        content=str(content or ""),
        source=str(source or ""),
        occurred_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        file_name=str(file_name or ""),
        rows=tuple(str(item) for item in rows),
        translation=_translate_if_needed(str(content or "")),
    ).to_public_dict()


def _build_empty_file_analyse_data_sources(content: str = "") -> List[Dict[str, Any]]:
    if content:
        return [
            WeaponryAnalyseDataSource(
                content=str(content),
                source="",
                occurred_at="",
                file_name="",
                rows=(),
                translation=_translate_if_needed(str(content)),
            ).to_public_dict()
        ]
    return [WeaponryAnalyseDataSource.empty().to_public_dict()]


# ---------------------------------------------------------------------------
# 进度发布
# ---------------------------------------------------------------------------

def _publish_progress(
    progress_hub: LLMProgressHub,
    architecture_id: int,
    progress: float,
) -> None:
    business_key = str(architecture_id)
    progress_hub.publish(
        "weaponry",
        business_key,
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
    """查询 INPUT 字段；迁移期只保留按文件聚合语义。"""

    _require_file_aggregate_strategy()
    specification = WeaponryFieldSpecification.from_mapping(field)
    field_name = specification.field_name
    field_desc = specification.field_description

    # Retrieval Query 与 Extraction Prompt 已在类型和文本上分离。旧链路尚未接入
    # Schema v2 production profile 已属于新模块；遗留链路仍只负责兼容召回，1D-6
    # 切换公开路由前不得把新 profile、evaluation 阈值或 score/rank 逻辑反向写回此处。
    retrieval_query = build_retrieval_query(
        RetrievalField(
            field_name=field_name,
            field_description=field_desc,
        )
    )
    vs_results = _vector_search_with_top_n(
        client,
        workspace_slug,
        retrieval_query.text,
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
        logger.info("字段向量检索未命中，使用空来源: field_name=%s", field_name)
        filled["analyseData"] = ""
        filled["analyseDataSource"] = _build_empty_file_analyse_data_sources()
        return filled

    file_chunks: Dict[str, List[str]] = defaultdict(list)
    file_max_score: Dict[str, float] = defaultdict(float)
    for chunk in vs_results:
        chunk_text = _strip_document_metadata(chunk.get("text", ""))
        if not chunk_text:
            continue
        file_name = _extract_chunk_source_name(chunk) or _fallback_target_file_name(
            retrieval_context
        )
        if not file_name:
            raise ValueError("未能从文档片段中提取出明确的文件名。")
        file_chunks[file_name].append(chunk_text)
        file_max_score[file_name] = max(file_max_score[file_name], _get_score(chunk))

    sorted_files = sorted(
        file_chunks,
        key=lambda value: (-file_max_score[value], value),
    )
    data_sources: List[Dict[str, Any]] = []
    first_valid_content = ""
    for file_name in sorted_files:
        chunks = _normalize_chunks_for_prompt(file_chunks[file_name])
        if not chunks:
            continue
        extraction_prompt = build_input_extraction_prompt(
            specification,
            _legacy_prompt_evidence(file_name, chunks),
            guidance=_legacy_guidance(terms_rule_context),
        )
        result = _send_prompt_to_isolated_thread(
            client,
            workspace_slug,
            thread_slug,
            extraction_prompt.text,
            user_id=user_id,
            mode="chat",
        )
        if not result:
            continue
        text_response = str(result.get("textResponse", "")).strip()
        if "未找到" in text_response or not text_response:
            continue
        data_sources.append(
            _build_file_analyse_data_source(
                content=text_response,
                source=_resolve_original_source_name(file_name, retrieval_context),
                file_name=_resolve_hashed_source_name(file_name, retrieval_context),
                rows=list(extraction_prompt.rows),
            )
        )
        if not first_valid_content:
            first_valid_content = text_response

    if not data_sources:
        logger.info(
            "字段按文件提取完成，但未得到有效信息: field_name=%s",
            field_name,
        )
        filled["analyseData"] = ""
        filled["analyseDataSource"] = _build_empty_file_analyse_data_sources()
        return filled

    logger.info(
        "字段按文件提取完成: field_name=%s valid_file_count=%d result_chars=%d",
        field_name,
        len(data_sources),
        len(first_valid_content),
    )
    filled["analyseData"] = first_valid_content
    filled["analyseDataSource"] = data_sources
    return filled


# ---------------------------------------------------------------------------
# TABLE 类型字段处理
# ---------------------------------------------------------------------------


def _legacy_table_specification(
    field: Mapping[str, Any],
    column_defs: Sequence[Mapping[str, Any]],
) -> WeaponryFieldSpecification:
    """把遗留字典边界转换为 1D 不可变 TABLE specification。"""

    payload = dict(field)
    payload.setdefault("fieldName", "表格")
    payload["fieldType"] = "TABLE"
    normalized_columns: List[Dict[str, Any]] = []
    for column in column_defs:
        normalized_column = dict(column)
        # 该默认值只存在于遗留内部 helper；HTTP Parser 在 1D-2 仍须严格要求公开单元格
        # 显式携带 fieldType=INPUT，不能把此兼容行为外扩为接口宽松解析。
        normalized_column.setdefault("fieldType", "INPUT")
        normalized_columns.append(normalized_column)
    payload["tableFieldList"] = [normalized_columns]
    return WeaponryFieldSpecification.from_mapping(payload)


def _extract_table_column_defs(field: Dict[str, Any]) -> List[Dict[str, Any]]:
    """兼容入口：从不可变 TABLE specification 投影遗留列字典。"""

    specification = WeaponryFieldSpecification.from_mapping(field)
    columns: List[Dict[str, Any]] = []
    for item in specification.columns:
        column = item.template.to_dict()
        column["fieldName"] = item.field_name
        column.setdefault("fieldType", "INPUT")
        column.pop("analyseData", None)
        column.pop("analyseDataSource", None)
        columns.append(column)
    return columns


def _build_table_retrieval_query(field: Dict[str, Any], column_defs: List[Dict[str, Any]]) -> str:
    """兼容入口：构造不含生成指令的 TABLE RetrievalQuery。"""

    retrieval_field = RetrievalField(
        field_name=str(field.get("fieldName") or "表格"),
        field_description=str(field.get("fieldDescription") or ""),
        field_type="TABLE",
        columns=tuple(
            RetrievalColumn(
                field_name=str(column.get("fieldName") or ""),
                field_description=str(column.get("fieldDescription") or ""),
            )
            for column in column_defs
        ),
    )
    return build_retrieval_query(retrieval_field).text


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


def _parse_table_json_rows(text: str, column_defs: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    specification = _legacy_table_specification({}, column_defs)
    return [
        item.to_legacy_dict()
        for item in parse_table_json_rows(
            text,
            specification,
            max_rows=MAX_TABLE_ROWS,
        )
    ]


def _merge_table_rows(
    row_results: List[Dict[str, Any]],
    column_defs: List[Dict[str, Any]],
) -> List[MergedTableRow]:
    """兼容入口：把遗留行结果交给纯领域合并规则。"""

    specification = _legacy_table_specification({}, column_defs)
    column_names = tuple(item.field_name for item in specification.columns)
    typed_results: List[TableRowResult] = []
    for row_result in row_results:
        row = row_result.get("row")
        if not isinstance(row, Mapping):
            continue
        parsed = ParsedTableRow(
            row_key=str(row.get("__rowKey") or ""),
            values=tuple(
                (name, _domain_normalize_table_cell_value(row.get(name)))
                for name in column_names
            ),
        )
        translations = tuple(
            (name, _translate_if_needed(parsed.get(name)))
            for name in column_names
            if parsed.get(name)
        )
        typed_results.append(
            TableRowResult(
                row=parsed,
                source_name=str(row_result.get("source") or ""),
                file_name=str(row_result.get("fileName") or ""),
                evidence_rows=tuple(
                    str(item) for item in row_result.get("rows") or ()
                ),
                occurred_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                translations=translations,
            )
        )
    return list(
        merge_table_rows(
            typed_results,
            specification,
            max_rows=MAX_TABLE_ROWS,
        )
    )


def _assemble_table_rows(
    merged_rows: List[MergedTableRow],
    column_defs: List[Dict[str, Any]],
) -> List[List[Dict[str, Any]]]:
    """兼容入口：使用不可变列模板组装二维公开结果。"""

    specification = _legacy_table_specification({}, column_defs)
    return [
        [cell.to_public_dict() for cell in row]
        for row in assemble_table_rows(merged_rows, specification)
    ]


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
    _require_file_aggregate_strategy()

    def _finish(filled_field: Dict[str, Any]) -> Dict[str, Any]:
        if on_cell_done:
            on_cell_done()
        return filled_field

    def _preserve_original_table_rows() -> Dict[str, Any]:
        filled = dict(field)
        if "tableFieldList" not in filled:
            filled["tableFieldList"] = []
        return filled

    template_rows = field.get("tableFieldList") or []
    if not template_rows:
        return _finish(_preserve_original_table_rows())

    column_defs = _extract_table_column_defs(field)
    if not column_defs:
        return _finish(_preserve_original_table_rows())
    specification = _legacy_table_specification(field, column_defs)

    table_name = field.get("fieldName", "表格")
    logger.info("开始提取表格字段: table_name=%s column_count=%d", table_name, len(column_defs))

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
        logger.info("表格字段向量检索未命中，保留原始模板: table_name=%s", table_name)
        return _finish(_preserve_original_table_rows())

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
    sorted_files = sorted(
        file_chunks,
        key=lambda file_name: (-file_max_score[file_name], file_name),
    )
    for file_name in sorted_files:
        chunks = _normalize_chunks_for_prompt(file_chunks[file_name])
        if not chunks:
            continue
        extraction_prompt = build_table_extraction_prompt(
            specification,
            _legacy_prompt_evidence(file_name, chunks),
            guidance=_legacy_guidance(terms_rule_context),
        )
        result = _send_prompt_to_isolated_thread(
            client,
            workspace_slug,
            thread_slug,
            extraction_prompt.text,
            user_id=user_id,
            mode="chat",
        )
        if not result:
            continue

        text_response = str(result.get("textResponse", "")).strip()
        rows = _parse_table_json_rows(text_response, column_defs)
        if not rows:
            if text_response and "未找到" not in text_response and text_response != "[]":
                logger.warning(
                    "表格字段的模型响应无法解析为有效行: table_name=%s response_chars=%d",
                    table_name,
                    len(text_response),
                )
            continue

        original_source_name = _resolve_original_source_name(file_name, retrieval_context)
        hashed_file_name = _resolve_hashed_source_name(file_name, retrieval_context)
        for row in rows:
            row_results.append(
                {
                    "row": row,
                    "source": original_source_name,
                    "fileName": hashed_file_name,
                    "rows": list(extraction_prompt.rows),
                    "score": file_max_score[file_name],
                }
            )

    filled = dict(field)
    merged_rows = _merge_table_rows(row_results, column_defs)
    assembled_rows = _assemble_table_rows(merged_rows, column_defs)
    if assembled_rows:
        filled["tableFieldList"] = assembled_rows
        extracted_row_count = len(assembled_rows)
    else:
        filled["tableFieldList"] = template_rows
        extracted_row_count = 0
    logger.info("表格字段提取完成: table_name=%s extracted_row_count=%d", table_name, extracted_row_count)
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
    selected_documents: Optional[Sequence[WeaponrySelectedDocument]] = None,
    execution_id: Optional[str] = None,
) -> None:
    """后台线程入口：执行 weaponry 解析任务。

    ``execution_id`` 为内部任务身份，不属于 HTTP 请求或回调字段。队列恢复指定文件
    范围任务时必须携带它，才能读取受理时对应的文档快照。
    """

    params = request_payload.get("params", {})
    raw_architecture_id = params.get("architectureId")
    try:
        # 即使遗留任务快照仍保存 ``"00042"``，Worker 也必须与受理、查询和进度入口
        # 使用同一规范业务键 ``42``。这里只规范内部身份，不修改任何公开参数集合。
        architecture_id = normalize_architecture_id_value(raw_architecture_id)
    except WeaponryDomainValidationError:
        logger.error(
            "武器装备任务输入损坏: architectureId格式或范围非法 "
            "architecture_id_type=%s",
            type(raw_architecture_id).__name__,
        )
        return
    architecture_id_str = str(architecture_id)
    field_list: List[Dict[str, Any]] = params.get("weaponryTemplateFieldList", [])
    raw_file_path_list = params.get("filePathList")
    has_explicit_file_scope = (
        isinstance(raw_file_path_list, list) and bool(raw_file_path_list)
    )
    client: Optional[AnythingLLMClient] = None
    workspace_slug = ""
    temporary_workspace_slug = ""
    thread_slug = ""
    thread_deleted = False
    retrieval_context: Optional[WeaponryRetrievalContext] = None
    terms_restored = False

    try:
        # 迁移期先在任何数据库写入、AnythingLLM 调用或 Callback 之前拒绝模式 1。
        # 1D-5 将把这项检查进一步前移到组合根启动配置校验。
        _require_file_aggregate_strategy()
        if selected_documents is None and has_explicit_file_scope:
            # 生产路由会直接传入同一份快照；此分支服务于未来携带 execution_id 的
            # 可靠调度器，确保异步重试不会重新按当前分类查询文档。
            resolved_selected_documents = _load_persisted_weaponry_selected_documents(
                task_service,
                architecture_id,
                execution_id,
            )
        else:
            resolved_selected_documents = tuple(selected_documents or ())
        if any(
            not isinstance(item, WeaponrySelectedDocument)
            for item in resolved_selected_documents
        ):
            raise TypeError("selected_documents只能包含WeaponrySelectedDocument")
        if has_explicit_file_scope and not resolved_selected_documents:
            raise ValueError("指定文件范围任务缺少已受理文档快照")

        # ─── 阶段 1：准备检索 Workspace ───
        task_service.update_task_progress(
            "weaponry", architecture_id_str,
            progress=0.05, message="正在查找知识库", status="1",
        )
        _publish_progress(progress_hub, architecture_id, 0.05)

        client = AnythingLLMClient(load_anythingllm_config())
        if resolved_selected_documents:
            # 指定范围不再依赖目标 ``architectureId`` 的永久 workspace。所有来源文档
            # 都只加入本任务新建的临时 workspace，绝不能向任一来源类别增删 embedding。
            selected_doc_paths = [
                item.doc_path for item in resolved_selected_documents
            ]

            temporary_workspace_name = f"weaponry-selection-{architecture_id}-{int(time.time() * 1000)}"
            workspace_info = client.create_rag_workspace(temporary_workspace_name, user_id=1)
            if not workspace_info:
                _fail_task(
                    task_service, progress_hub, architecture_id, architecture_id_str,
                    callback_url, callback_timeout,
                    msg="创建选中文件临时知识库失败",
                )
                return

            temporary_workspace_slug = str(
                workspace_info.get("slug") or workspace_info.get("id") or ""
            )
            if not temporary_workspace_slug:
                _fail_task(
                    task_service, progress_hub, architecture_id, architecture_id_str,
                    callback_url, callback_timeout,
                    msg="获取选中文件临时知识库标识失败",
                )
                return

            if not client.update_embeddings_batch(
                temporary_workspace_slug,
                adds=selected_doc_paths,
                user_id=1,
            ):
                _fail_task(
                    task_service, progress_hub, architecture_id, architecture_id_str,
                    callback_url, callback_timeout,
                    msg="向临时知识库关联选中文件失败",
                )
                return

            workspace_slug = temporary_workspace_slug
            logger.info(
                "武器装备解析已限制为跨分类选中文件: architecture_id=%s "
                "file_count=%d source_architecture_count=%d",
                architecture_id,
                len(resolved_selected_documents),
                len(
                    {
                        item.source_architecture_id
                        for item in resolved_selected_documents
                    }
                ),
            )
        else:
            # 未指定 filePathList 时必须保留旧语义：检索目标类别下全部永久文档，
            # 因而仍要求该类别已存在永久 workspace 映射。
            base_workspace_slug = kb_service.get_workspace_slug(architecture_id)
            if not base_workspace_slug:
                logger.warning(
                    "未找到分类对应的知识库工作区，任务将标记失败: architecture_id=%s",
                    architecture_id,
                )
                _fail_task(
                    task_service, progress_hub, architecture_id, architecture_id_str,
                    callback_url, callback_timeout,
                    msg=f"architectureId={architecture_id} 对应的知识库不存在",
                )
                return
            workspace_slug = base_workspace_slug

        # ─── 阶段 2：创建临时 Thread ───
        task_service.update_task_progress(
            "weaponry", architecture_id_str,
            progress=0.10, message="正在创建检索会话",
        )
        _publish_progress(progress_hub, architecture_id, 0.10)

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
            selected_documents=resolved_selected_documents or None,
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
            _publish_progress(progress_hub, architecture_id, progress)

        result_fields: List[Dict[str, Any]] = []
        try:
            for field in field_list:
                field_type = field.get("fieldType", "INPUT")
                field_name = field.get("fieldName", "unknown")
                logger.info("开始提取字段: field_name=%s field_type=%s", field_name, field_type)

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
            terms_restored = True

        # ─── 阶段 4：删除 Thread ───
        task_service.update_task_progress(
            "weaponry", architecture_id_str,
            progress=0.92, message="正在清理检索会话",
        )
        _publish_progress(progress_hub, architecture_id, 0.92)

        thread_deleted = client.delete_thread(workspace_slug, thread_slug, user_id=1)
        if not thread_deleted:
            logger.warning("删除临时检索会话失败，不影响已得到的任务结果")

        logger.info("武器装备提取任务执行完成: architecture_id=%s", architecture_id)

        # ─── 阶段 5：组装回调并发送 ───
        callback_payload = _build_weaponry_callback_payload(
            architecture_id, result_fields, status="2", msg="解析成功",
        )
        task_service.mark_business_result(
            "weaponry", architecture_id_str,
            callback_payload, status="2", message="解析完成",
        )
        _publish_progress(progress_hub, architecture_id, 1.0)

        if callback_url:
            if post_callback_payload(
                callback_url,
                callback_payload,
                timeout=callback_timeout,
                callback_context={"businessType": "weaponry", "architectureId": architecture_id},
            ):
                task_service.mark_callback_success("weaponry", architecture_id_str)
                logger.info("武器装备任务外部回调提交成功: architecture_id=%s", architecture_id)
            else:
                task_service.mark_callback_failed("weaponry", architecture_id_str, "callback failed")
                logger.warning("武器装备任务外部回调提交失败: architecture_id=%s", architecture_id)
        else:
            # 未配置回调时将终态记录为 skipped，防止后续巡检把它识别成待重放任务。
            task_service.mark_callback_skipped("weaponry", architecture_id_str)

    except Exception as e:
        logger.exception(
            "武器装备提取任务发生异常: architecture_id=%s error_type=%s",
            architecture_id,
            type(e).__name__,
        )
        _fail_task(
            task_service, progress_hub, architecture_id, architecture_id_str,
            callback_url, callback_timeout,
            msg=f"解析异常: {e}",
        )
    finally:
        if client and retrieval_context and workspace_slug and not terms_restored:
            try:
                _restore_target_workspace_terms(client, workspace_slug, retrieval_context, user_id=1)
            except Exception as cleanup_error:
                logger.warning(
                    "恢复目标工作区术语文档时发生异常，不影响任务结果: error_type=%s",
                    type(cleanup_error).__name__,
                )

        if client and workspace_slug and thread_slug and not thread_deleted:
            try:
                if not client.delete_thread(workspace_slug, thread_slug, user_id=1):
                    logger.warning("最终清理临时检索会话失败，不影响任务结果")
            except Exception as cleanup_error:
                logger.warning(
                    "最终清理临时检索会话时发生异常，不影响任务结果: error_type=%s",
                    type(cleanup_error).__name__,
                )

        if client and temporary_workspace_slug:
            try:
                if not client.delete_workspace(temporary_workspace_slug, user_id=1):
                    logger.warning(
                        "删除选中文件临时工作区失败，不影响任务结果",
                    )
            except Exception as cleanup_error:
                logger.warning(
                    "删除选中文件临时工作区时发生异常，不影响任务结果: error_type=%s",
                    type(cleanup_error).__name__,
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
    logger.warning(
        "武器装备提取任务已标记失败: architecture_id=%s failure_message_chars=%d",
        architecture_id,
        len(msg or ""),
    )
    callback_payload = _build_weaponry_callback_payload(
        architecture_id, [], status="3", msg=msg,
    )
    task_service.mark_business_result(
        "weaponry", architecture_id_str,
        callback_payload, status="3", message=msg,
    )
    _publish_progress(progress_hub, architecture_id, 1.0)

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
    else:
        # 失败结果已经落库但没有外部接收方，显式记录无需回调且不增加尝试次数。
        task_service.mark_callback_skipped("weaponry", architecture_id_str)
