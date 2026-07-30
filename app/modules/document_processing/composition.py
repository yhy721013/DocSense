"""阶段 1 的显式本地组合函数。

该函数不会被模块导入时自动执行，也不会启动后台线程或探测 LibreOffice。生产调用方
要到后续迁移波次才逐一接线；当前只为离线测试和未来组合根提供一个明确构造入口。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from app.modules.document_processing.adapters import (
    LocalArtifactStoreAdapter,
    MarkdownRagProjectionProcessorAdapter,
    ResourceLimitedDocumentProcessorAdapter,
    SQLiteMinerUOperationObserver,
    SQLiteProcessingRecordAdapter,
    build_markdown_rag_projection_profile,
)
from app.modules.document_processing.adapters.builtin_ocr import (
    BuiltinOCRDocumentProcessorAdapter,
)
from app.modules.document_processing.adapters.libreoffice import (
    LibreOfficeDocumentProcessorAdapter,
)
from app.modules.document_processing.adapters.mineru import (
    MinerUDocumentProcessorAdapter,
)
from app.modules.document_processing.application import (
    PrepareDocument,
    ProjectDocumentForRag,
)
from app.modules.document_processing.ports import (
    DocumentProcessorPort,
    LegacyOfficePreparer,
    ResourcePort,
)


_ENVIRONMENT_LOCK = threading.Lock()
_CONFIGURED_ENVIRONMENT: tuple[str, str] | None = None


def configure_document_processing_environment(
    *,
    mineru_model_source: str = "local",
    tessdata_prefix: str | None = None,
) -> None:
    """在启动并发任务前一次性配置供应商进程环境。

    同一进程重复调用必须给出相同配置；运行中改变模型源或 tessdata 会让不同任务读取
    到不同默认值，因此直接拒绝。
    """

    global _CONFIGURED_ENVIRONMENT
    model_source = str(mineru_model_source).strip()
    tessdata = str(tessdata_prefix or "").strip()
    if not model_source:
        raise ValueError("mineru_model_source 不能为空")
    requested = (model_source, tessdata)
    with _ENVIRONMENT_LOCK:
        if (
            _CONFIGURED_ENVIRONMENT is not None
            and _CONFIGURED_ENVIRONMENT != requested
        ):
            raise RuntimeError("文档处理进程环境已冻结，禁止运行中修改")
        os.environ["MINERU_MODEL_SOURCE"] = model_source
        if tessdata:
            os.environ["TESSDATA_PREFIX"] = tessdata
        _CONFIGURED_ENVIRONMENT = requested


def build_local_prepare_document(
    *,
    processor: DocumentProcessorPort,
    db_path: str | Path,
    artifact_root: str | Path,
) -> PrepareDocument:
    """构造阶段 1 单实例用例；调用方须显式传入共用 SQLite 文件路径。"""

    return PrepareDocument(
        processor=processor,
        artifact_store=LocalArtifactStoreAdapter(artifact_root),
        records=SQLiteProcessingRecordAdapter(db_path),
    )


def build_local_libreoffice_prepare_document(
    *,
    preparer: LegacyOfficePreparer,
    db_path: str | Path,
    artifact_root: str | Path,
    materialization_root: str | Path,
) -> PrepareDocument:
    """用同一 Store 实例装配 Legacy Office 源读取与目标发布。"""

    artifact_store = LocalArtifactStoreAdapter(artifact_root)
    processor = LibreOfficeDocumentProcessorAdapter(
        preparer=preparer,
        source_store=artifact_store,
        materialization_root=materialization_root,
    )
    return PrepareDocument(
        processor=processor,
        artifact_store=artifact_store,
        records=SQLiteProcessingRecordAdapter(db_path),
    )


def build_local_mineru_prepare_document(
    *,
    db_path: str | Path,
    artifact_root: str | Path,
    materialization_root: str | Path,
    resource: ResourcePort,
    api_url: str | None = None,
) -> PrepareDocument:
    """装配带共享重型许可和持久化供应商身份的 MinerU 用例。"""

    artifact_store = LocalArtifactStoreAdapter(artifact_root)
    processor = MinerUDocumentProcessorAdapter(
        source_store=artifact_store,
        materialization_root=materialization_root,
        operation_observer=SQLiteMinerUOperationObserver(db_path),
        api_url=api_url,
    )
    limited = ResourceLimitedDocumentProcessorAdapter(
        processor=processor,
        resource=resource,
    )
    return PrepareDocument(
        processor=limited,
        artifact_store=artifact_store,
        records=SQLiteProcessingRecordAdapter(db_path),
    )


def build_local_builtin_ocr_prepare_document(
    *,
    db_path: str | Path,
    artifact_root: str | Path,
    materialization_root: str | Path,
    resource: ResourcePort,
) -> PrepareDocument:
    """装配与 MinerU 共用许可实例的内置 OCR 用例。"""

    artifact_store = LocalArtifactStoreAdapter(artifact_root)
    processor = BuiltinOCRDocumentProcessorAdapter(
        source_store=artifact_store,
        materialization_root=materialization_root,
    )
    limited = ResourceLimitedDocumentProcessorAdapter(
        processor=processor,
        resource=resource,
    )
    return PrepareDocument(
        processor=limited,
        artifact_store=artifact_store,
        records=SQLiteProcessingRecordAdapter(db_path),
    )


def build_local_project_document_for_rag(
    *,
    db_path: str | Path,
    artifact_root: str | Path,
    materialization_root: str | Path,
) -> ProjectDocumentForRag:
    """装配单实例 RAG Markdown 投影；Store/Record 与准备链使用同一物理边界。"""

    artifact_store = LocalArtifactStoreAdapter(artifact_root)
    processor = MarkdownRagProjectionProcessorAdapter(
        source_store=artifact_store,
        materialization_root=materialization_root,
    )
    prepare = PrepareDocument(
        processor=processor,
        artifact_store=artifact_store,
        records=SQLiteProcessingRecordAdapter(db_path),
    )
    return ProjectDocumentForRag(
        prepare_document=prepare,
        profile=build_markdown_rag_projection_profile(),
    )


__all__ = [
    "build_local_builtin_ocr_prepare_document",
    "build_local_libreoffice_prepare_document",
    "build_local_mineru_prepare_document",
    "build_local_prepare_document",
    "build_local_project_document_for_rag",
    "configure_document_processing_environment",
]
