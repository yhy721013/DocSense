"""Markdown 到 RAG-only Markdown 的流式投影 Adapter。

投影只移除 Markdown 图片 data URI 的内嵌正文，保留普通链接、外部图片、标题、表格和
代码块。扫描器以固定块读取、候选语法超过内存阈值后自动落到 Processor 私有 scratch，
不会用一个无界正则把整份文档或十几 MiB Base64 一次载入内存。
"""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from app.modules.document_processing.adapters.content import FileArtifactContent
from app.modules.document_processing.domain import (
    ArtifactKind,
    DocumentProcessingRequest,
    DocumentRepresentation,
    ProcessingProfile,
    RagProjectionError,
)
from app.modules.document_processing.ports import ArtifactStorePort, ProcessorOutput


logger = logging.getLogger(__name__)

MARKDOWN_RAG_PROJECTION_PROCESSOR_ID = "markdown-rag-projection"
MARKDOWN_RAG_PROJECTION_PROCESSOR_FINGERPRINT = (
    "docsense-markdown-rag-projection-v1"
)
_SCAN_CHUNK_BYTES = 64 * 1024
_CANDIDATE_MEMORY_BYTES = 64 * 1024
_ALT_MAX_CHARS = 160
_PLACEHOLDER_MAX_CHARS = 512
_MAX_DATA_URI_HEADER_BYTES = 1024
_DATA_IMAGE_PREFIX = b"data:image/"
_ALLOWED_SOURCE_REPRESENTATIONS = frozenset(
    {
        DocumentRepresentation.MARKDOWN,
        DocumentRepresentation.TEXT,
    }
)
_EXPECTED_PROFILE_PARAMETERS = {
    "algorithmVersion": "markdown-rag-projection-v1",
    "altMaxChars": _ALT_MAX_CHARS,
    "candidateMemoryBytes": _CANDIDATE_MEMORY_BYTES,
    "dataUriImagePolicy": "remove-payload-with-digest-v1",
    "decodeStrategy": "strict-base64-else-raw-sha256-v1",
    "encoding": "utf-8-strict",
    "newlinePolicy": "preserve-source-v1",
    "placeholderMaxChars": _PLACEHOLDER_MAX_CHARS,
    "scanChunkBytes": _SCAN_CHUNK_BYTES,
}


def build_markdown_rag_projection_profile() -> ProcessingProfile:
    """返回冻结算法、资源上限和摘要策略的稳定 Profile。"""

    return ProcessingProfile.create(
        processor_id=MARKDOWN_RAG_PROJECTION_PROCESSOR_ID,
        processor_fingerprint=MARKDOWN_RAG_PROJECTION_PROCESSOR_FINGERPRINT,
        target_representation=DocumentRepresentation.MARKDOWN,
        parameters=_EXPECTED_PROFILE_PARAMETERS,
    )


@dataclass(slots=True)
class _ProjectionStats:
    source_bytes: int = 0
    output_bytes: int = 0
    removed_images: int = 0
    invalid_base64_images: int = 0
    malformed_data_images: int = 0


class _ValidatingByteReader:
    """固定块读取并增量验证 UTF-8，支持扫描器所需的少量回退。"""

    def __init__(self, source: BinaryIO) -> None:
        self._source = source
        self._buffer = b""
        self._position = 0
        self._pushback: list[int] = []
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.source_bytes = 0
        self._finished = False

    def read_byte(self) -> int | None:
        if self._pushback:
            return self._pushback.pop()
        if self._position >= len(self._buffer):
            chunk = self._source.read(_SCAN_CHUNK_BYTES)
            if not chunk:
                self._finish_validation()
                return None
            if not isinstance(chunk, bytes):
                raise RagProjectionError(
                    "rag_projection_reader_not_bytes",
                    "RAG 投影源 reader 必须返回 bytes",
                )
            try:
                self._decoder.decode(chunk, final=False)
            except UnicodeDecodeError as exc:
                raise RagProjectionError(
                    "rag_projection_utf8_invalid",
                    "RAG 投影源不是合法 UTF-8",
                ) from exc
            self.source_bytes += len(chunk)
            self._buffer = chunk
            self._position = 0
        value = self._buffer[self._position]
        self._position += 1
        return value

    def unread(self, values: bytes | bytearray | list[int]) -> None:
        for value in reversed(values):
            self._pushback.append(int(value))

    def read_until(self, delimiter: int) -> tuple[bytes, bool]:
        """块式读取到单字节分隔符，返回内容时不包含分隔符。"""

        if not 0 <= delimiter <= 255:
            raise ValueError("delimiter 必须是单字节整数")
        collected = bytearray()
        while self._pushback:
            value = self._pushback.pop()
            if value == delimiter:
                return bytes(collected), True
            collected.append(value)

        while True:
            if self._position >= len(self._buffer):
                chunk = self._source.read(_SCAN_CHUNK_BYTES)
                if not chunk:
                    self._finish_validation()
                    return bytes(collected), False
                if not isinstance(chunk, bytes):
                    raise RagProjectionError(
                        "rag_projection_reader_not_bytes",
                        "RAG 投影源 reader 必须返回 bytes",
                    )
                try:
                    self._decoder.decode(chunk, final=False)
                except UnicodeDecodeError as exc:
                    raise RagProjectionError(
                        "rag_projection_utf8_invalid",
                        "RAG 投影源不是合法 UTF-8",
                    ) from exc
                self.source_bytes += len(chunk)
                self._buffer = chunk
                self._position = 0

            found_at = self._buffer.find(bytes((delimiter,)), self._position)
            if found_at >= 0:
                collected.extend(self._buffer[self._position:found_at])
                self._position = found_at + 1
                return bytes(collected), True
            collected.extend(self._buffer[self._position:])
            self._position = len(self._buffer)
            # 每次最多把一个扫描块返回给调用方，保持额外内存与输入总大小无关。
            if collected:
                return bytes(collected), False

    def _finish_validation(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise RagProjectionError(
                "rag_projection_utf8_invalid",
                "RAG 投影源不是合法 UTF-8",
            ) from exc


class _MarkdownProjectionScanner:
    """不回溯整份文档的 Markdown data URI 图片扫描器。"""

    def transform(self, source: BinaryIO, destination: BinaryIO) -> _ProjectionStats:
        reader = _ValidatingByteReader(source)
        stats = _ProjectionStats()
        active_fence: int | None = None
        inline_code_ticks = 0
        normal_backslashes = 0
        at_line_start = True

        while True:
            if at_line_start and inline_code_ticks == 0:
                line_mode, marker, next_fence = self._read_line_marker(
                    reader,
                    active_fence,
                )
                if line_mode:
                    self._write(destination, marker, stats)
                    active_fence = next_fence
                    self._copy_line_raw(reader, destination, stats)
                    at_line_start = True
                    normal_backslashes = 0
                    continue

            value = reader.read_byte()
            if value is None:
                break
            if active_fence is not None:
                self._write(destination, bytes((value,)), stats)
                at_line_start = value == 0x0A
                continue
            if inline_code_ticks:
                if value == ord("`"):
                    run_length = self._copy_backtick_run(
                        reader,
                        destination,
                        stats,
                    )
                    if run_length == inline_code_ticks:
                        inline_code_ticks = 0
                    at_line_start = False
                else:
                    self._write(destination, bytes((value,)), stats)
                    at_line_start = value == 0x0A
                continue
            if value == ord("\\"):
                self._write(destination, b"\\", stats)
                normal_backslashes += 1
                at_line_start = False
                continue
            if value == ord("`") and normal_backslashes % 2 == 0:
                inline_code_ticks = self._copy_backtick_run(
                    reader,
                    destination,
                    stats,
                )
                normal_backslashes = 0
                at_line_start = False
                continue
            if value == ord("!") and normal_backslashes % 2 == 0:
                handled, last_consumed = self._try_project_image(
                    reader,
                    destination,
                    stats,
                )
                if handled:
                    at_line_start = last_consumed == 0x0A
                    normal_backslashes = 0
                    continue
            else:
                self._write(destination, bytes((value,)), stats)
            at_line_start = value == 0x0A
            normal_backslashes = 0

        stats.source_bytes = reader.source_bytes
        return stats

    def _copy_backtick_run(
        self,
        reader: _ValidatingByteReader,
        destination: BinaryIO,
        stats: _ProjectionStats,
    ) -> int:
        """复制当前反引号 run，并把第一个非反引号字节退回。"""

        run_length = 1
        self._write(destination, b"`", stats)
        while True:
            value = reader.read_byte()
            if value != ord("`"):
                if value is not None:
                    reader.unread([value])
                return run_length
            self._write(destination, b"`", stats)
            run_length += 1

    def _read_line_marker(
        self,
        reader: _ValidatingByteReader,
        active_fence: int | None,
    ) -> tuple[str, bytes, int | None]:
        """识别 fenced/四空格缩进代码行；未命中时把前瞻字节原样退回。"""

        consumed = bytearray()
        while len(consumed) < 4:
            value = reader.read_byte()
            if value != 0x20:
                if value is not None:
                    consumed.append(value)
                break
            consumed.append(value)
        if len(consumed) == 4 and consumed == b"    ":
            return "indented_code", bytes(consumed), active_fence
        if not consumed:
            return "", b"", active_fence

        first = consumed[-1]
        leading_spaces = len(consumed) - 1
        if leading_spaces <= 3 and first in (ord("`"), ord("~")):
            second = reader.read_byte()
            third = reader.read_byte()
            if second is not None:
                consumed.append(second)
            if third is not None:
                consumed.append(third)
            if second == first and third == first:
                if active_fence is None:
                    return "fence", bytes(consumed), first
                if active_fence == first:
                    return "fence", bytes(consumed), None
                # 另一种 fence 字符位于现有代码块内，只是普通代码行。
                return "fence_content", bytes(consumed), active_fence

        reader.unread(consumed)
        return "", b"", active_fence

    def _copy_line_raw(
        self,
        reader: _ValidatingByteReader,
        destination: BinaryIO,
        stats: _ProjectionStats,
    ) -> None:
        while True:
            value = reader.read_byte()
            if value is None:
                return
            self._write(destination, bytes((value,)), stats)
            if value == 0x0A:
                return

    def _try_project_image(
        self,
        reader: _ValidatingByteReader,
        destination: BinaryIO,
        stats: _ProjectionStats,
    ) -> tuple[bool, int | None]:
        """尝试消费一个图片语法；普通/不完整语法按原字节回放。"""

        next_value = reader.read_byte()
        if next_value != ord("["):
            self._write(destination, b"!", stats)
            if next_value is not None:
                reader.unread([next_value])
            return False, ord("!")

        with tempfile.SpooledTemporaryFile(
            max_size=_CANDIDATE_MEMORY_BYTES,
            mode="w+b",
        ) as candidate:
            candidate.write(b"![")
            alt_bytes = bytearray()
            escaped = False
            last_consumed: int | None = ord("[")
            while True:
                value = reader.read_byte()
                last_consumed = value
                if value is None:
                    self._flush_candidate(candidate, destination, stats)
                    return True, last_consumed
                candidate.write(bytes((value,)))
                if value == ord("]") and not escaped:
                    break
                if len(alt_bytes) < _ALT_MAX_CHARS * 4:
                    alt_bytes.append(value)
                if value == ord("\\") and not escaped:
                    escaped = True
                else:
                    escaped = False

            opening = reader.read_byte()
            last_consumed = opening
            if opening != ord("("):
                self._flush_candidate(candidate, destination, stats)
                if opening is not None:
                    reader.unread([opening])
                return True, ord("]")
            candidate.write(b"(")

            # CommonMark destination 可有少量空白或尖括号包装；这些字节必须在普通图片
            # 分支完整回放，在 data URI 分支则不进入向量化结果。
            while True:
                value = reader.read_byte()
                last_consumed = value
                if value not in (0x20, 0x09):
                    break
                candidate.write(bytes((value,)))
            if value == ord("<"):
                candidate.write(b"<")
                value = reader.read_byte()
                last_consumed = value

            prefix = bytearray()
            if value is not None:
                prefix.append(value)
            while len(prefix) < len(_DATA_IMAGE_PREFIX):
                value = reader.read_byte()
                last_consumed = value
                if value is None:
                    break
                prefix.append(value)
            candidate.write(prefix)
            if bytes(prefix).lower() != _DATA_IMAGE_PREFIX:
                self._flush_candidate(candidate, destination, stats)
                return True, last_consumed

            return self._consume_data_image(
                reader,
                destination,
                stats,
                alt_bytes=bytes(alt_bytes),
            )

    def _consume_data_image(
        self,
        reader: _ValidatingByteReader,
        destination: BinaryIO,
        stats: _ProjectionStats,
        *,
        alt_bytes: bytes,
    ) -> tuple[bool, int | None]:
        header = bytearray()
        header_overflow = False
        last_consumed: int | None = None
        found_comma = False
        while True:
            value = reader.read_byte()
            last_consumed = value
            if value is None or value == ord(")"):
                break
            if value == ord(","):
                found_comma = True
                break
            if len(header) < _MAX_DATA_URI_HEADER_BYTES:
                header.append(value)
            else:
                header_overflow = True

        raw_digest = hashlib.sha256()
        decoded_digest = hashlib.sha256()
        decoded_valid = found_comma and b";base64" in bytes(header).lower()
        base64_quartet = bytearray()
        padding_seen = False
        payload_bytes = 0

        if found_comma:
            while True:
                payload_chunk, found_closing = reader.read_until(ord(")"))
                raw_digest.update(payload_chunk)
                payload_bytes += len(payload_chunk)
                if decoded_valid:
                    cleaned = payload_chunk.translate(None, b" \t\r\n")
                    if padding_seen and cleaned:
                        decoded_valid = False
                    else:
                        base64_quartet.extend(cleaned)
                if decoded_valid:
                    complete_size = (len(base64_quartet) // 4) * 4
                    complete = bytes(base64_quartet[:complete_size])
                    del base64_quartet[:complete_size]
                    if complete:
                        if ord("=") in complete:
                            padding_seen = True
                        try:
                            decoded = base64.b64decode(complete, validate=True)
                        except (ValueError, binascii.Error):
                            decoded_valid = False
                        else:
                            decoded_digest.update(decoded)
                if found_closing:
                    last_consumed = ord(")")
                    break
                if not payload_chunk:
                    last_consumed = None
                    break
            if base64_quartet:
                decoded_valid = False

        malformed = (
            not found_comma
            or last_consumed != ord(")")
            or header_overflow
        )
        if malformed:
            stats.malformed_data_images += 1
        if found_comma and b";base64" in bytes(header).lower() and not decoded_valid:
            stats.invalid_base64_images += 1

        media_subtype = bytes(header).split(b";", 1)[0]
        media_type = self._safe_media_type(media_subtype)
        digest = (
            decoded_digest.hexdigest()
            if decoded_valid
            else raw_digest.hexdigest()
        )
        digest_label = "sha256" if decoded_valid else "payload_sha256"
        placeholder = self._placeholder(
            alt_bytes=alt_bytes,
            media_type=media_type,
            digest_label=digest_label,
            digest=digest,
            payload_bytes=payload_bytes,
            malformed=malformed,
        )
        self._write(destination, placeholder.encode("utf-8"), stats)
        stats.removed_images += 1
        return True, last_consumed

    @staticmethod
    def _safe_media_type(header_subtype: bytes) -> str:
        decoded = header_subtype.decode("ascii", errors="ignore").lower()
        safe = "".join(
            character
            for character in decoded
            if character.isalnum() or character in ".+-"
        )[:64]
        return f"image/{safe or 'unknown'}"

    @staticmethod
    def _placeholder(
        *,
        alt_bytes: bytes,
        media_type: str,
        digest_label: str,
        digest: str,
        payload_bytes: int,
        malformed: bool,
    ) -> str:
        alt = alt_bytes.decode("utf-8", errors="replace")
        alt = " ".join(alt.split()).replace("[", "（").replace("]", "）")
        alt = alt[:_ALT_MAX_CHARS] or "未提供"
        status = "；语法不完整" if malformed else ""
        result = (
            f"[内嵌图片已移除：alt={alt}；media_type={media_type}；"
            f"{digest_label}={digest}；payload_bytes={payload_bytes}{status}]"
        )
        # 当前固定字段加最大 alt 明显小于上限；仍保留防御性截断，避免未来字段扩展
        # 意外把超长占位文本送入 RAG。
        return result[:_PLACEHOLDER_MAX_CHARS]

    @staticmethod
    def _flush_candidate(
        candidate: BinaryIO,
        destination: BinaryIO,
        stats: _ProjectionStats,
    ) -> None:
        candidate.seek(0)
        while True:
            chunk = candidate.read(_SCAN_CHUNK_BYTES)
            if not chunk:
                break
            _MarkdownProjectionScanner._write(destination, chunk, stats)

    @staticmethod
    def _write(
        destination: BinaryIO,
        payload: bytes,
        stats: _ProjectionStats,
    ) -> None:
        destination.write(payload)
        stats.output_bytes += len(payload)


class MarkdownRagProjectionProcessorAdapter:
    """读取不可变 prepared Artifact，生成独立的 RAG 投影候选。"""

    def __init__(
        self,
        *,
        source_store: ArtifactStorePort,
        materialization_root: str | Path,
    ) -> None:
        if not isinstance(source_store, ArtifactStorePort):
            raise TypeError("source_store 必须实现 ArtifactStorePort")
        self._source_store = source_store
        self._materialization_root = Path(materialization_root).resolve()
        if (
            self._materialization_root.exists()
            and not self._materialization_root.is_dir()
        ):
            raise ValueError("RAG 投影 scratch root 必须是目录")
        self._materialization_root.mkdir(parents=True, exist_ok=True)
        self._scanner = _MarkdownProjectionScanner()

    def process(self, request: DocumentProcessingRequest) -> ProcessorOutput:
        self._validate_request(request)
        # 任务目录保留完整 SHA-256，避免长期运行及多实例环境中出现命名空间碰撞。
        # 输出文件不再重复携带 64 位 step_key，而由 tempfile 在目录内原子分配短名称；
        # 按故障环境约 92 字符的 root 计算，总路径约 92+1+64+1+14+1+17=190，
        # 既低于传统 Windows MAX_PATH，又不需要截断任务身份或随机数。
        task_namespace = hashlib.sha256(
            request.task_id.value.encode("utf-8")
        ).hexdigest()
        scratch_directory = (
            self._materialization_root / task_namespace / "rag-projection"
        ).resolve()
        self._require_contained(scratch_directory, self._materialization_root)
        scratch_directory.mkdir(parents=True, exist_ok=True)
        output_path: Path | None = None

        try:
            with (
                self._source_store.open_reader(
                    request.source_artifact
                ) as source,
                self._open_owned_output(scratch_directory) as owned_output,
            ):
                output_path, destination = owned_output
                self._require_contained(output_path, scratch_directory)
                stats = self._scanner.transform(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            if stats.source_bytes != request.source_artifact.metadata.size_bytes:
                raise RagProjectionError(
                    "rag_projection_source_size_mismatch",
                    "RAG 投影源实际长度与 Artifact 元数据不一致",
                )
            logger.info(
                "RAG Markdown 投影已生成: task_id=%s step_key=%s "
                "source_bytes=%d output_bytes=%d removed_images=%d "
                "invalid_base64_images=%d malformed_data_images=%d",
                request.task_id,
                request.step_key[:12],
                stats.source_bytes,
                stats.output_bytes,
                stats.removed_images,
                stats.invalid_base64_images,
                stats.malformed_data_images,
            )
            warnings = (
                (f"rag_projection_removed_images:{stats.removed_images}",)
                if stats.removed_images
                else ()
            )
            # 正常退出两个上下文后文件句柄已经关闭，才能把候选文件交给后续
            # Artifact 发布流程。显式校验可以在优化模式下继续守住该不变量。
            if output_path is None:
                raise RagProjectionError(
                    "rag_projection_output_missing",
                    "RAG 投影输出文件未创建",
                )
            return ProcessorOutput.with_cleanup(
                content=FileArtifactContent(output_path),
                kind=ArtifactKind.RAG_PROJECTION,
                representation=DocumentRepresentation.MARKDOWN,
                media_type="text/markdown; charset=utf-8",
                cleanup=lambda: self._cleanup_scratch(
                    output_path,
                    scratch_directory,
                ),
                warnings=warnings,
            )
        except RagProjectionError:
            self._cleanup_scratch(output_path, scratch_directory)
            raise
        except Exception as exc:
            self._cleanup_scratch(output_path, scratch_directory)
            logger.exception(
                "RAG Markdown 投影失败: task_id=%s step_key=%s error_type=%s",
                request.task_id,
                request.step_key[:12],
                type(exc).__name__,
            )
            raise RagProjectionError(
                "rag_projection_unexpected_error",
                "无法生成 RAG Markdown 投影",
            ) from exc

    @staticmethod
    @contextmanager
    def _open_owned_output(
        scratch_directory: Path,
    ) -> Iterator[tuple[Path, BinaryIO]]:
        """原子创建并持有一个短名称输出文件。

        ``mkstemp`` 使用独占创建并在名称冲突时由标准库重试，适用于同一任务被
        多线程或多实例同时执行的情况。文件描述符从创建成功起就由本上下文持有，
        任何异常都会先关闭句柄，再尝试删除仅属于本次调用的文件，绝不删除其他
        执行已经存在的碰撞目标。
        """

        descriptor, raw_path = tempfile.mkstemp(
            prefix="rag-",
            suffix=".part",
            dir=scratch_directory,
        )
        output_path = Path(raw_path).resolve()
        try:
            destination = os.fdopen(descriptor, "wb")
        except BaseException:
            # fdopen 极少失败，但失败时裸文件描述符仍归当前调用所有，必须显式关闭。
            os.close(descriptor)
            output_path.unlink(missing_ok=True)
            raise

        completed = False
        try:
            with destination:
                yield output_path, destination
            completed = True
        finally:
            if not completed:
                try:
                    output_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "RAG 投影异常候选清理失败: scratch_name=%s",
                        output_path.name,
                        exc_info=True,
                    )

    @staticmethod
    def _validate_request(request: DocumentProcessingRequest) -> None:
        if not isinstance(request, DocumentProcessingRequest):
            raise TypeError("request 必须是 DocumentProcessingRequest")
        if request.source_artifact.representation not in (
            _ALLOWED_SOURCE_REPRESENTATIONS
        ):
            raise RagProjectionError(
                "rag_projection_source_representation_invalid",
                "RAG Markdown 投影只接受 Markdown/Text Artifact",
            )
        profile = request.profile
        if (
            profile.processor_id != MARKDOWN_RAG_PROJECTION_PROCESSOR_ID
            or profile.processor_fingerprint
            != MARKDOWN_RAG_PROJECTION_PROCESSOR_FINGERPRINT
            or profile.target_representation is not DocumentRepresentation.MARKDOWN
            or profile.to_dict()["parameters"] != _EXPECTED_PROFILE_PARAMETERS
        ):
            raise RagProjectionError(
                "rag_projection_profile_mismatch",
                "RAG Markdown 投影 Profile 不匹配",
            )

    @staticmethod
    def _require_contained(candidate: Path, root: Path) -> None:
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise RagProjectionError(
                "rag_projection_scratch_escape",
                "RAG 投影 scratch 路径越界",
            ) from exc

    @staticmethod
    def _cleanup_scratch(
        path: Path | None,
        scratch_directory: Path,
    ) -> None:
        # 源 Artifact 可能在目标文件创建前就打开失败，此时只需回收空目录。
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "RAG 投影 scratch 文件清理失败: scratch_name=%s",
                    path.name,
                    exc_info=True,
                )
                return
        for directory in (scratch_directory, scratch_directory.parent):
            try:
                directory.rmdir()
            except OSError:
                break


__all__ = (
    "MARKDOWN_RAG_PROJECTION_PROCESSOR_FINGERPRINT",
    "MARKDOWN_RAG_PROJECTION_PROCESSOR_ID",
    "MarkdownRagProjectionProcessorAdapter",
    "build_markdown_rag_projection_profile",
)
