"""无需重型转换的通用格式校验与 Artifact 直通适配器。"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import BinaryIO, Iterator

from app.modules.document_processing.domain import (
    ArtifactKind,
    ArtifactRef,
    DocumentProcessingError,
    DocumentProcessingRequest,
    DocumentRepresentation,
    ProcessingProfile,
)
from app.modules.document_processing.ports import (
    ArtifactStorePort,
    ProcessorOutput,
)


logger = logging.getLogger(__name__)
PASSTHROUGH_PROCESSOR_ID = "validated-passthrough"
PASSTHROUGH_PROCESSOR_FINGERPRINT = "docsense-passthrough-adapter-v1"
_TEXT_REPRESENTATIONS = {
    DocumentRepresentation.TEXT,
    DocumentRepresentation.MARKDOWN,
}


def build_passthrough_profile(
    *,
    source_suffix: str,
    target_representation: DocumentRepresentation,
    media_type: str,
    max_size_bytes: int,
    encoding: str = "utf-8",
) -> ProcessingProfile:
    """冻结直通格式、MIME、编码和尺寸上限。"""

    suffix = str(source_suffix).strip().lower()
    normalized_media_type = str(media_type).strip().lower()
    normalized_encoding = str(encoding).strip().lower()
    if not suffix.startswith(".") or not normalized_media_type:
        raise ValueError("直通 Profile 的 source_suffix/media_type 不合法")
    if (
        isinstance(max_size_bytes, bool)
        or not isinstance(max_size_bytes, int)
        or max_size_bytes <= 0
    ):
        raise ValueError("max_size_bytes 必须是正整数")
    if (
        target_representation in _TEXT_REPRESENTATIONS
        and normalized_encoding != "utf-8"
    ):
        # 第一版只支持确定性的 UTF-8；后续若引入探测器，必须把探测策略纳入 Profile。
        raise ValueError("文本直通当前只允许 utf-8")
    return ProcessingProfile.create(
        processor_id=PASSTHROUGH_PROCESSOR_ID,
        processor_fingerprint=PASSTHROUGH_PROCESSOR_FINGERPRINT,
        target_representation=target_representation,
        parameters={
            "encoding": normalized_encoding,
            "maxSizeBytes": max_size_bytes,
            "mediaType": normalized_media_type,
            "sourceSuffix": suffix,
        },
    )


class _StoredArtifactContent:
    """把 Store 中已有内容作为新发布步骤的流式内容源。"""

    def __init__(self, store: ArtifactStorePort, request: DocumentProcessingRequest):
        self._store = store
        self._artifact = request.source_artifact

    @contextmanager
    def open_reader(self) -> Iterator[BinaryIO]:
        with self._store.open_reader(self._artifact) as reader:
            yield reader


class ValidatedPassthroughDocumentProcessorAdapter:
    """校验非空、尺寸、MIME 和文本编码后发布新的谱系节点。"""

    def __init__(self, *, source_store: ArtifactStorePort) -> None:
        if not isinstance(source_store, ArtifactStorePort):
            raise TypeError("source_store 必须实现 ArtifactStorePort")
        self._source_store = source_store

    def process(self, request: DocumentProcessingRequest) -> ProcessorOutput:
        parameters = self._validate_profile(request)
        source = request.source_artifact
        max_size = int(parameters["maxSizeBytes"])
        if source.metadata.size_bytes <= 0:
            raise DocumentProcessingError(
                "passthrough_empty_content",
                "直通文档不能为空",
            )
        if source.metadata.size_bytes > max_size:
            raise DocumentProcessingError(
                "passthrough_size_limit_exceeded",
                "直通文档超过冻结的尺寸上限",
            )
        expected_media_type = str(parameters["mediaType"])
        if source.metadata.media_type != expected_media_type:
            raise DocumentProcessingError(
                "passthrough_media_type_mismatch",
                "源 Artifact MIME 与冻结 Profile 不一致",
            )
        if request.profile.target_representation in _TEXT_REPRESENTATIONS:
            self._validate_utf8_text(source, source.metadata.size_bytes)
        elif request.profile.target_representation is DocumentRepresentation.PDF:
            self._validate_pdf_header(source)

        logger.info(
            "通用直通 Processor 校验通过: task_id=%s step_key=%s "
            "representation=%s bytes=%d",
            request.task_id,
            request.step_key[:12],
            request.profile.target_representation.value,
            source.metadata.size_bytes,
        )
        return ProcessorOutput(
            content=_StoredArtifactContent(self._source_store, request),
            kind=ArtifactKind.PREPARED,
            representation=request.profile.target_representation,
            media_type=expected_media_type,
        )

    def _validate_utf8_text(
        self,
        source: ArtifactRef,
        expected_size: int,
    ) -> None:
        with self._source_store.open_reader(source) as reader:
            payload = reader.read(expected_size + 1)
        if len(payload) != expected_size:
            raise DocumentProcessingError(
                "passthrough_source_size_mismatch",
                "文本 Artifact 实际长度与元数据不一致",
            )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentProcessingError(
                "passthrough_text_encoding_invalid",
                "文本 Artifact 不是合法 UTF-8",
            ) from exc
        if not text.strip():
            raise DocumentProcessingError(
                "passthrough_empty_text",
                "文本 Artifact 不得只包含空白",
            )

    def _validate_pdf_header(self, source: ArtifactRef) -> None:
        with self._source_store.open_reader(source) as reader:
            header = reader.read(5)
        if header != b"%PDF-":
            raise DocumentProcessingError(
                "passthrough_pdf_header_invalid",
                "PDF Artifact 文件头不合法",
            )

    def _validate_profile(
        self,
        request: DocumentProcessingRequest,
    ) -> dict[str, object]:
        profile = request.profile
        if profile.processor_id != PASSTHROUGH_PROCESSOR_ID:
            raise DocumentProcessingError(
                "passthrough_profile_mismatch",
                "请求不是通用直通 Profile",
            )
        parameters = profile.to_dict()["parameters"]
        if not isinstance(parameters, dict) or set(parameters) != {
            "encoding",
            "maxSizeBytes",
            "mediaType",
            "sourceSuffix",
        }:
            raise DocumentProcessingError(
                "passthrough_profile_invalid",
                "通用直通 Profile 参数集合不合法",
            )
        if (
            not str(parameters["sourceSuffix"]).startswith(".")
            or not str(parameters["mediaType"]).strip()
            or isinstance(parameters["maxSizeBytes"], bool)
            or not isinstance(parameters["maxSizeBytes"], int)
            or parameters["maxSizeBytes"] <= 0
        ):
            raise DocumentProcessingError(
                "passthrough_profile_invalid",
                "通用直通 Profile 参数值不合法",
            )
        return parameters


__all__ = [
    "PASSTHROUGH_PROCESSOR_FINGERPRINT",
    "PASSTHROUGH_PROCESSOR_ID",
    "ValidatedPassthroughDocumentProcessorAdapter",
    "build_passthrough_profile",
]
