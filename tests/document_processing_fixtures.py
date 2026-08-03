"""共享 DocumentProcessing 离线测试装配。

测试必须复用现行 Artifact/Profile/Record 链，不能为了构造 Report/Analysis Adapter
重新引入旧路径式 OCR/MinerU 假实现。本模块只使用临时目录、SQLite 和禁用的
Legacy Office 配置，不启动外部进程或网络服务。
"""

from __future__ import annotations

from pathlib import Path

from app.modules.document_processing import (
    LegacyOfficeConfig,
    LibreOfficeLegacyOfficePreparer,
)
from app.modules.document_processing.adapters import (
    FIFOCapacityAdapter,
    LocalArtifactStoreAdapter,
    LocalDocumentPreparationAdapter,
    MarkdownRagProjectionProcessorAdapter,
    SQLiteProcessingRecordAdapter,
    build_markdown_rag_projection_profile,
)
from app.modules.document_processing.application import (
    PrepareDocument,
    ProjectDocumentForRag,
)


def build_test_document_preparer(
    root: str | Path,
) -> LocalDocumentPreparationAdapter:
    """构造完全离线的现行文档准备链，并让调用方拥有全部临时资源。"""

    resolved_root = Path(root).resolve()
    return LocalDocumentPreparationAdapter(
        artifact_store=LocalArtifactStoreAdapter(resolved_root / "artifacts"),
        records=SQLiteProcessingRecordAdapter(resolved_root / "processing.sqlite3"),
        resource=FIFOCapacityAdapter(1),
        legacy_office_preparer=LibreOfficeLegacyOfficePreparer(
            LegacyOfficeConfig.disabled(jobs_root=resolved_root / "office-jobs")
        ),
        materialization_root=resolved_root / "materializations",
        legacy_policy_fingerprint="tests-document-processing-policy-v1",
        ocr_languages="chi_sim+eng",
        ocr_dpi=300,
    )


def build_test_rag_projector(
    preparer: LocalDocumentPreparationAdapter,
    root: str | Path,
) -> ProjectDocumentForRag:
    """为 Analysis 构造与 canonical Artifact Store 共用所有权边界的 RAG 投影。"""

    resolved_root = Path(root).resolve()
    return ProjectDocumentForRag(
        prepare_document=PrepareDocument(
            processor=MarkdownRagProjectionProcessorAdapter(
                source_store=preparer.artifact_store,
                materialization_root=resolved_root / "materializations",
            ),
            artifact_store=preparer.artifact_store,
            records=SQLiteProcessingRecordAdapter(
                resolved_root / "processing.sqlite3"
            ),
        ),
        profile=build_markdown_rag_projection_profile(),
    )


__all__ = ["build_test_document_preparer", "build_test_rag_projector"]
