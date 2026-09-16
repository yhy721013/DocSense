"""把既有 LibreOffice 安全内核适配为通用 DocumentProcessorPort。"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from app.modules.document_processing.adapters.content import FileArtifactContent
from app.modules.document_processing.domain import (
    ArtifactKind,
    DocumentProcessingError,
    DocumentProcessingRequest,
    DocumentRepresentation,
    LegacyOfficeConversionError,
)
from app.modules.document_processing.ports import (
    ArtifactStorePort,
    LegacyOfficePreparer,
    ProcessorOutput,
)

from .profile import (
    LEGACY_OFFICE_PROCESSOR_ID,
    normalize_legacy_suffix,
    target_media_type_for,
    target_suffix_for,
)


logger = logging.getLogger(__name__)
_MATERIALIZATION_MARKER = ".docsense-document-materialization"


class LibreOfficeDocumentProcessorAdapter:
    """读取源 Artifact、调用唯一 Legacy Preparer，并交付待发布 OOXML。

    源文件和转换结果的宿主路径只存在于本 Adapter。返回值是带清理租约的
    ``ProcessorOutput``；Application 发布 Artifact 并提交记录后会幂等关闭该租约。
    """

    def __init__(
        self,
        *,
        preparer: LegacyOfficePreparer,
        source_store: ArtifactStorePort,
        materialization_root: str | Path,
    ) -> None:
        if not isinstance(preparer, LegacyOfficePreparer):
            raise TypeError("preparer 必须实现 LegacyOfficePreparer")
        if not isinstance(source_store, ArtifactStorePort):
            raise TypeError("source_store 必须实现 ArtifactStorePort")
        self._preparer = preparer
        self._source_store = source_store
        self._root = self._canonical_resolved(
            Path(materialization_root).expanduser()
        )
        if self._root.exists() and not self._root.is_dir():
            raise ValueError("materialization_root 必须是目录")

    def process(self, request: DocumentProcessingRequest) -> ProcessorOutput:
        if not isinstance(request, DocumentProcessingRequest):
            raise TypeError("request 必须是 DocumentProcessingRequest")
        source_suffix, target_suffix, expected_version = self._validate_profile(
            request
        )
        actual_version = self._preparer.preflight()
        if actual_version != expected_version:
            raise DocumentProcessingError(
                "snapshot_version_mismatch",
                "冻结的 LibreOffice 版本与当前运行时不一致",
            )

        materialized_root = self._create_materialization_directory()
        source_path = materialized_root / f"source{source_suffix}"
        preparation = None
        try:
            with self._source_store.open_reader(
                request.source_artifact
            ) as reader, source_path.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            preparation = self._preparer.prepare(
                source_path,
                job_id=request.step_key,
            )
            if (
                not preparation.converted
                or preparation.source_suffix != source_suffix
                or preparation.target_suffix != target_suffix
            ):
                raise DocumentProcessingError(
                    "legacy_office_conversion_contract_error",
                    "Legacy Office 转换结果不符合冻结 profile",
                )
            content = FileArtifactContent(preparation.prepared_path)

            def cleanup() -> None:
                try:
                    preparation.close()
                finally:
                    self._cleanup_materialization(materialized_root)

            logger.info(
                "Legacy Office Processor 已生成候选: task_id=%s "
                "step_key=%s source_suffix=%s target_suffix=%s version=%s",
                request.task_id,
                request.step_key[:12],
                source_suffix,
                target_suffix,
                expected_version,
            )
            return ProcessorOutput.with_cleanup(
                content=content,
                kind=ArtifactKind.NORMALIZED,
                representation=DocumentRepresentation.OOXML,
                media_type=target_media_type_for(source_suffix),
                cleanup=cleanup,
            )
        except LegacyOfficeConversionError as exc:
            self._close_failed_candidate(preparation, materialized_root)
            # 保留已经冻结的 Legacy Office 错误码，避免迁移后诊断能力倒退。
            raise DocumentProcessingError(
                exc.code,
                exc.safe_message,
                outcome_unknown=exc.outcome_unknown,
            ) from exc
        except DocumentProcessingError:
            self._close_failed_candidate(preparation, materialized_root)
            raise
        except Exception as exc:
            self._close_failed_candidate(preparation, materialized_root)
            logger.exception(
                "Legacy Office Processor 适配异常: task_id=%s step_key=%s",
                request.task_id,
                request.step_key[:12],
            )
            raise DocumentProcessingError(
                "legacy_office_processor_failed",
                "Legacy Office Processor 执行失败",
            ) from exc

    def sweep_stale_materializations(self) -> int:
        """启动时只清理带所有权标记的直接子目录。"""

        if not self._root.exists():
            return 0
        if self._root.is_symlink() or not self._root.is_dir():
            logger.warning("跳过不安全的文档物化根目录巡检")
            return 0
        try:
            candidates = tuple(self._root.iterdir())
        except OSError:
            logger.warning("读取文档物化根目录失败", exc_info=True)
            return 0
        removed = 0
        for candidate in candidates:
            if self._cleanup_materialization(candidate):
                removed += 1
        return removed

    def _close_failed_candidate(
        self,
        preparation: object | None,
        materialized_root: Path,
    ) -> None:
        try:
            if preparation is not None:
                preparation.close()
        except Exception:
            logger.warning(
                "Legacy Office 候选清理失败，将由既有 job 巡检继续处理",
                exc_info=True,
            )
        finally:
            self._cleanup_materialization(materialized_root)

    @staticmethod
    def _validate_profile(
        request: DocumentProcessingRequest,
    ) -> tuple[str, str, str]:
        profile = request.profile
        if (
            profile.processor_id != LEGACY_OFFICE_PROCESSOR_ID
            or profile.target_representation
            is not DocumentRepresentation.OOXML
        ):
            raise DocumentProcessingError(
                "legacy_office_profile_mismatch",
                "请求不是 Legacy Office OOXML profile",
            )
        parameters = profile.to_dict()["parameters"]
        expected_keys = {
            "libreofficeVersion",
            "policyFingerprint",
            "sourceSuffix",
            "targetSuffix",
        }
        if not isinstance(parameters, dict) or set(parameters) != expected_keys:
            raise DocumentProcessingError(
                "legacy_office_profile_invalid",
                "Legacy Office profile 参数集合不合法",
            )
        try:
            source_suffix = normalize_legacy_suffix(parameters["sourceSuffix"])
            target_suffix = target_suffix_for(source_suffix)
        except (TypeError, ValueError) as exc:
            raise DocumentProcessingError(
                "legacy_office_profile_invalid",
                "Legacy Office profile 格式不合法",
            ) from exc
        if parameters["targetSuffix"] != target_suffix:
            raise DocumentProcessingError(
                "legacy_office_profile_invalid",
                "Legacy Office profile 目标格式不一致",
            )
        version = str(parameters["libreofficeVersion"]).strip()
        if not version:
            raise DocumentProcessingError(
                "legacy_office_profile_invalid",
                "Legacy Office profile 版本为空",
            )
        return source_suffix, target_suffix, version

    def _create_materialization_directory(self) -> Path:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            created = Path(tempfile.mkdtemp(prefix="job-", dir=self._root))
            marker = created / _MATERIALIZATION_MARKER
            marker.write_text("DOCSENSE_DOCUMENT_MATERIALIZATION_V1\n", encoding="ascii")
            resolved = self._canonical_resolved(created)
            self._require_contained(resolved, self._root)
            return resolved
        except OSError as exc:
            raise DocumentProcessingError(
                "materialization_create_failed",
                "无法创建文档物化目录",
            ) from exc

    def _cleanup_materialization(self, path: Path) -> bool:
        """只删除本 Adapter 创建且仍位于受控根下的随机目录。"""

        try:
            candidate = self._canonical_resolved(path)
            self._require_contained(candidate, self._root)
            marker = candidate / _MATERIALIZATION_MARKER
            if (
                candidate.parent != self._root
                or not candidate.name.startswith("job-")
                or marker.is_symlink()
                or not marker.is_file()
                or marker.read_text(encoding="ascii")
                != "DOCSENSE_DOCUMENT_MATERIALIZATION_V1\n"
            ):
                logger.warning(
                    "跳过不满足所有权条件的文档物化目录清理: "
                    "directory_name=%s",
                    candidate.name,
                )
                return False
            shutil.rmtree(candidate)
            return True
        except OSError:
            logger.warning(
                "文档物化目录清理失败，将由启动巡检继续处理: "
                "directory_name=%s",
                path.name,
                exc_info=True,
            )
            return False

    @staticmethod
    def _require_contained(candidate: Path, root: Path) -> None:
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise DocumentProcessingError(
                "materialization_path_escape",
                "文档物化路径越出允许边界",
            ) from exc

    @staticmethod
    def _canonical_resolved(path: Path) -> Path:
        resolved = path.resolve()
        if os.name != "nt":
            return resolved
        text = str(resolved)
        if text.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + text[8:])
        if text.startswith("\\\\?\\"):
            return Path(text[4:])
        return resolved


__all__ = ["LibreOfficeDocumentProcessorAdapter"]
