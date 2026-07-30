"""MinerU 文档解析适配器。

本模块是 MinerU 提交、轮询、下载、解压和结果选择的唯一实现位置。Translation
仅消费准备完成的 Markdown，不再拥有或反向暴露 MinerU 基础设施。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import mimetypes
import os
import re
import shutil
from pathlib import Path
from typing import Callable, List, Optional, Protocol, runtime_checkable
from urllib.parse import unquote, urlsplit

import httpx

from app.modules.document_processing.adapters.content import FileArtifactContent
from app.modules.document_processing.domain import (
    ArtifactKind,
    DocumentProcessingError,
    DocumentProcessingRequest,
    DocumentRepresentation,
    ProcessingProfile,
)
from app.modules.document_processing.ports import (
    ArtifactStorePort,
    ProcessorOutput,
)
from mineru.cli import api_client as _api_client
from mineru.cli.common import image_suffixes, office_suffixes, pdf_suffixes
from mineru.model.pptx.main import convert_path as _pptx_convert_path
from mineru.utils.enum_class import BlockType as _PptxBlockType
from mineru.utils.guess_suffix_or_lang import guess_suffix_by_path


logger = logging.getLogger(__name__)

SUPPORTED_INPUT_SUFFIXES = set(pdf_suffixes + image_suffixes + office_suffixes)
_PPTX_SUFFIXES = frozenset({".pptx"})
MINERU_PROCESSOR_ID = "mineru-to-markdown"
MINERU_PROCESSOR_FINGERPRINT = "docsense-mineru-adapter-v2"
_MATERIALIZATION_MARKER = ".docsense-mineru-materialization"
_MAX_EMBEDDED_IMAGE_BYTES = 32 * 1024 * 1024
_MAX_EMBEDDED_IMAGES_TOTAL_BYTES = 128 * 1024 * 1024
_MARKDOWN_IMAGE_PATTERN = re.compile(
    r"(?P<prefix>!\[[^\]]*\]\(\s*)"
    r"(?P<target><[^>]+>|[^\s)]+)"
    r"(?P<suffix>(?:\s+[\"'][^\"']*[\"'])?\s*\))"
)
_HTML_IMAGE_PATTERN = re.compile(
    r"(?P<prefix><img\b[^>]*?\bsrc\s*=\s*)"
    r"(?P<quote>[\"'])"
    r"(?P<target>.*?)"
    r"(?P=quote)",
    flags=re.IGNORECASE,
)
_SAFE_EMBEDDED_IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


@runtime_checkable
class MinerUOperationObserver(Protocol):
    """持久化外部提交边界所需的最小观察端口。"""

    def record_submission_intent(
        self,
        *,
        operation_key: str,
        provider: str,
    ) -> None:
        """在向供应商发送请求前，先持久化提交意图。"""

    def record_provider_identity(
        self,
        *,
        operation_key: str,
        provider_operation_id: str,
    ) -> None:
        """取得供应商任务身份后立即持久化，供恢复流程对账。"""

    def record_terminal(
        self,
        *,
        operation_key: str,
        state: str,
    ) -> None:
        """持久化供应商已确认的 succeeded/failed 终态。"""


class _NoopMinerUOperationObserver:
    """兼容旧直接调用路径的空观察器；新 Processor 必须注入真实实现。"""

    def record_submission_intent(
        self,
        *,
        operation_key: str,
        provider: str,
    ) -> None:
        del operation_key, provider

    def record_provider_identity(
        self,
        *,
        operation_key: str,
        provider_operation_id: str,
    ) -> None:
        del operation_key, provider_operation_id

    def record_terminal(
        self,
        *,
        operation_key: str,
        state: str,
    ) -> None:
        del operation_key, state


def build_mineru_profile(
    *,
    source_suffix: str,
    use_ocr: bool,
    lang: str,
    extract_images: bool = True,
    formula_enable: bool = True,
    table_enable: bool = True,
    backend: str = "pipeline",
    api_mode: str = "local",
    endpoint_fingerprint: str = "local",
) -> ProcessingProfile:
    """构造冻结 Profile，避免重试时重新读取变化后的 MinerU 默认值。"""

    normalized_suffix = str(source_suffix).strip().lower()
    normalized_lang = str(lang).strip()
    normalized_backend = str(backend).strip()
    normalized_api_mode = str(api_mode).strip().lower()
    normalized_endpoint_fingerprint = str(endpoint_fingerprint).strip().lower()
    if (
        normalized_suffix.lstrip(".") not in SUPPORTED_INPUT_SUFFIXES
        or not normalized_suffix.startswith(".")
        or not normalized_lang
        or not normalized_backend
        or normalized_api_mode not in {"local", "remote"}
        or not normalized_endpoint_fingerprint
    ):
        raise ValueError("MinerU Profile 参数不合法")
    return ProcessingProfile.create(
        processor_id=MINERU_PROCESSOR_ID,
        processor_fingerprint=MINERU_PROCESSOR_FINGERPRINT,
        target_representation=DocumentRepresentation.MARKDOWN,
        parameters={
            "apiMode": normalized_api_mode,
            "backend": normalized_backend,
            "endpointFingerprint": normalized_endpoint_fingerprint,
            "extractImages": bool(extract_images),
            "formulaEnable": bool(formula_enable),
            "lang": normalized_lang,
            "sourceSuffix": normalized_suffix,
            "tableEnable": bool(table_enable),
            "useOcr": bool(use_ocr),
        },
    )


def mineru_endpoint_fingerprint(api_url: str | None) -> str:
    """生成可持久化的 MinerU 端点指纹，不保存 URL、查询参数或认证信息。"""

    if not api_url:
        return "local"
    parsed = urlsplit(str(api_url).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("MinerU API URL 必须是有效的 http/https 地址")
    port = f":{parsed.port}" if parsed.port is not None else ""
    # 用户信息和 query 可能承载密钥；Profile 只冻结不透明指纹。
    safe_identity = (
        f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"
        f"{parsed.path.rstrip('/')}"
    )
    return hashlib.sha256(safe_identity.encode("utf-8")).hexdigest()


class MinerUConverter:
    """
    MinerU 文档转 Markdown 工具类
    支持：PDF, DOCX, PPTX, PNG/JPG, 扫描件
    输出：markdown + 图片
    """

    def __init__(
        self,
        output_dir: str = "./mineru_output",
        *,
        operation_observer: MinerUOperationObserver | None = None,
    ):
        """
        初始化 MinerU 转换器
        :param output_dir: 输出目录
        """
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._operation_observer = (
            operation_observer or _NoopMinerUOperationObserver()
        )

    def convert_to_markdown(
            self,
            input_path: str,
            use_ocr: bool = False,
            lang: str = "ch",
            extract_images: bool = True,
            formula_enable: bool = True,
            table_enable: bool = True,
            backend: str = "pipeline",
            api_url: Optional[str] = None,
            server_url: Optional[str] = None,
            output_subdir: Optional[str] = None,
            operation_key: str = "",
            return_result_directory: bool = False,
    ) -> str:
        """
        单个文件 / 文件夹 转 Markdown
        :param input_path: 单个文件路径 或 文件夹路径
        :param use_ocr: 是否开启 OCR（扫描 PDF、图片必开）
        :param lang: 语言 zh / en
        :param extract_images: 是否提取图片
        :param formula_enable: 是否启用公式识别
        :param table_enable: 是否启用表格识别
        :param backend: 后端类型
        :param api_url: 远程 API URL（None 则启动本地服务）
        :param server_url: 服务器 URL（仅 http-client 模式需要）
        :param output_subdir: 输出子目录名（用于区分不同文件的输出）
        :param operation_key: 外部操作的稳定内部键；新 Processor 调用时必须提供
        :param return_result_directory: 返回完整结果目录，由上层严格校验 Markdown 数量
        :return: 输出的 markdown 文件路径
        """
        input_path = Path(input_path).expanduser().resolve()

        if not input_path.exists():
            raise FileNotFoundError(f"Input path does not exist: {input_path}")

        # 【关键修改】如果有指定输出子目录，使用独立的输出目录
        if output_subdir:
            current_output_dir = (
                self.output_dir / str(output_subdir)
            ).resolve()
            try:
                current_output_dir.relative_to(self.output_dir)
            except ValueError as exc:
                raise ValueError("output_subdir 越出 MinerU 输出根目录") from exc
        else:
            current_output_dir = self.output_dir

        current_output_dir.mkdir(parents=True, exist_ok=True)

        # 收集输入文件
        input_files = self._collect_input_files(input_path)
        logger.info("待转换文件：%d 个", len(input_files))

        # PPTX 直接转换：MinerU API 层跳过 PPTX，使用原生 PptxConverter 绕过。
        pptx_files = [
            f for f in input_files if f.suffix.lower() in _PPTX_SUFFIXES
        ]
        if pptx_files:
            return self._convert_pptx_direct(
                pptx_files=pptx_files,
                output_dir=current_output_dir,
                extract_images=extract_images,
                return_result_directory=return_result_directory,
            )

        # 构建表单数据
        form_data = self._build_form_data(
            language=lang,
            backend=backend,
            parse_method="ocr" if use_ocr else "auto",
            formula_enable=formula_enable,
            table_enable=table_enable,
            server_url=server_url,
            start_page_id=0,
            end_page_id=None,
            return_md=True,
            return_images=extract_images,
        )

        upload_assets = [
            _api_client.UploadAsset(path=file_path, upload_name=file_path.name)
            for file_path in input_files
        ]

        # 执行转换
        result_md_path = self._run_conversion(
            upload_assets=upload_assets,
            form_data=form_data,
            api_url=api_url,
            server_url=server_url,
            output_dir=current_output_dir,
            operation_key=operation_key,
            return_result_directory=return_result_directory,
        )
        return result_md_path

    # ------------------------------------------------------------------
    # PPTX 直接转换（绕过 MinerU API 的 _process_office_doc 跳过逻辑）
    # ------------------------------------------------------------------

    def _convert_pptx_direct(
        self,
        *,
        pptx_files: List[Path],
        output_dir: Path,
        extract_images: bool,
        return_result_directory: bool,
    ) -> str:
        """使用 mineru.model.pptx 原生转换器将 PPTX 直接转为 Markdown。"""

        md_paths: List[Path] = []
        for pptx_file in pptx_files:
            logger.info(
                "PPTX 直接转换开始: file=%s", pptx_file.name,
            )
            pages = _pptx_convert_path(str(pptx_file))
            markdown_text = self._render_pptx_pages_to_markdown(
                pages, extract_images=extract_images,
            )
            md_name = pptx_file.stem + ".md"
            md_path = output_dir / md_name
            md_path.write_text(markdown_text, encoding="utf-8")
            logger.info(
                "PPTX 直接转换完成: file=%s -> %s (%d chars)",
                pptx_file.name, md_name, len(markdown_text),
            )
            md_paths.append(md_path)

        if return_result_directory:
            return str(output_dir)
        return str(md_paths[0])

    @staticmethod
    def _render_pptx_pages_to_markdown(
        pages: list,
        *,
        extract_images: bool,
    ) -> str:
        """将 PptxConverter 输出的 pages/blocks 渲染为 Markdown 文本。"""

        parts: List[str] = []
        for page_idx, blocks in enumerate(pages):
            if not blocks:
                continue
            if page_idx > 0:
                parts.append("\n---\n")
            for block in blocks:
                block_type = block.get("type")
                content = block.get("content", "")
                if block_type == _PptxBlockType.TITLE:
                    parts.append(f"# {content}\n")
                elif block_type == _PptxBlockType.TEXT:
                    if content.strip():
                        parts.append(f"{content}\n")
                elif block_type == _PptxBlockType.LIST:
                    attribute = block.get("attribute", "unordered")
                    items = block.get("list_items", [])
                    for idx, item in enumerate(items, start=1):
                        item_text = item.get("content", "")
                        if attribute == "ordered":
                            parts.append(f"{idx}. {item_text}\n")
                        else:
                            parts.append(f"- {item_text}\n")
                    parts.append("")  # 列表后空行
                elif block_type == _PptxBlockType.TABLE:
                    parts.append(f"{content}\n")
                elif block_type == _PptxBlockType.IMAGE:
                    if extract_images and content:
                        parts.append(f"![image]({content})\n")
                else:
                    # 其他类型按纯文本输出
                    if content and content.strip():
                        parts.append(f"{content}\n")
        return "\n".join(parts)

    def _collect_input_files(self, input_path: Path) -> List[Path]:
        """收集输入文件"""
        if input_path.is_file():
            file_suffix = guess_suffix_by_path(input_path)
            if str(file_suffix).lower().lstrip(".") not in SUPPORTED_INPUT_SUFFIXES:
                raise ValueError(f"Unsupported input file type: {input_path.name}")
            return [input_path]

        if not input_path.is_dir():
            raise ValueError(f"Input path must be a file or directory: {input_path}")

        input_files = sorted(
            (
                candidate.resolve()
                for candidate in input_path.iterdir()
                if candidate.is_file()
                   and str(guess_suffix_by_path(candidate)).lower().lstrip(".")
                   in SUPPORTED_INPUT_SUFFIXES
            ),
            key=lambda item: item.name,
        )
        if not input_files:
            raise ValueError(f"No supported files found in directory: {input_path}")
        return input_files

    def _build_form_data(
            self,
            language: str,
            backend: str,
            parse_method: str,
            formula_enable: bool,
            table_enable: bool,
            server_url: Optional[str],
            start_page_id: int,
            end_page_id: Optional[int],
            return_md: bool = True,
            return_images: bool = True,
    ) -> dict:
        """构建请求表单数据"""
        return _api_client.build_parse_request_form_data(
            lang_list=[language],
            backend=backend,
            parse_method=parse_method,
            formula_enable=formula_enable,
            table_enable=table_enable,
            server_url=server_url,
            start_page_id=start_page_id,
            end_page_id=end_page_id,
            return_md=return_md,
            return_middle_json=False,
            return_model_output=False,
            return_content_list=False,
            return_images=return_images,
            response_format_zip=True,
            return_original_file=False,
        )

    def _run_conversion(
            self,
            upload_assets: List[_api_client.UploadAsset],
            form_data: dict,
            api_url: Optional[str],
            server_url: Optional[str],
            output_dir: Optional[Path] = None,
            operation_key: str = "",
            return_result_directory: bool = False,
    ) -> str:
        """执行实际的文件转换"""
        local_server: Optional[_api_client.LocalAPIServer] = None
        result_zip_path: Optional[Path] = None
        submit_response = None
        task_label = f"{len(upload_assets)} file(s)"
        submission_attempted = False
        provider_identity_recorded = False

        # 使用指定的输出目录或默认输出目录
        if output_dir is None:
            output_dir = self.output_dir

        async def run_async():
            nonlocal local_server
            nonlocal provider_identity_recorded
            nonlocal result_zip_path
            nonlocal submit_response
            nonlocal submission_attempted

            async with httpx.AsyncClient(
                    timeout=_api_client.build_http_timeout(),
                    follow_redirects=True,
            ) as http_client:
                try:
                    if api_url is None:
                        local_server = _api_client.LocalAPIServer()
                        base_url = local_server.start()
                        logger.info("本地 MinerU API 服务已启动")

                        server_health = await _api_client.wait_for_local_api_ready(
                            http_client,
                            local_server,
                        )
                    else:
                        server_health = await _api_client.fetch_server_health(
                            http_client,
                            _api_client.normalize_base_url(api_url),
                        )

                    logger.info("MinerU API 健康检查通过")
                    logger.info("开始提交 MinerU 解析任务: file_count=%d", len(upload_assets))

                    if operation_key:
                        self._operation_observer.record_submission_intent(
                            operation_key=operation_key,
                            provider="mineru",
                        )
                    submission_attempted = True
                    submit_response = await _api_client.submit_parse_task(
                        base_url=server_health.base_url,
                        upload_assets=upload_assets,
                        form_data=form_data,
                    )
                    if operation_key:
                        self._operation_observer.record_provider_identity(
                            operation_key=operation_key,
                            provider_operation_id=submit_response.task_id,
                        )
                    provider_identity_recorded = True
                    logger.info("MinerU 解析任务已提交: has_task_id=%s", bool(submit_response.task_id))

                    if submit_response.queued_ahead is not None:
                        logger.info(
                            "MinerU 解析任务正在排队: queued_ahead=%s",
                            submit_response.queued_ahead,
                        )

                    await _api_client.wait_for_task_result(
                        client=http_client,
                        submit_response=submit_response,
                        task_label=task_label,
                        status_snapshot_callback=self._on_status_update,
                    )
                    logger.info("MinerU 解析任务已完成")

                    result_zip_path = await _api_client.download_result_zip(
                        client=http_client,
                        submit_response=submit_response,
                        task_label=task_label,
                    )
                    if operation_key:
                        self._operation_observer.record_terminal(
                            operation_key=operation_key,
                            state="succeeded",
                        )
                except Exception as exc:
                    if (
                        submit_response is not None
                        and self._is_confirmed_provider_failure(
                            exc,
                            provider_operation_id=submit_response.task_id,
                        )
                    ):
                        if operation_key:
                            self._operation_observer.record_terminal(
                                operation_key=operation_key,
                                state="failed",
                            )
                        raise DocumentProcessingError(
                            "mineru_provider_confirmed_failure",
                            "MinerU 供应商已明确返回任务失败",
                        ) from exc
                    # 请求已经发出后，断线可能发生在供应商受理之后。此时不能标为
                    # 普通失败，否则调度器会盲目重复提交同一个重型任务。
                    if submission_attempted:
                        error_code = (
                            "mineru_provider_result_outcome_unknown"
                            if provider_identity_recorded
                            else "mineru_submission_outcome_unknown"
                        )
                        raise DocumentProcessingError(
                            error_code,
                            "MinerU 外部操作结果未知，必须先对账",
                            outcome_unknown=True,
                        ) from exc
                    raise
                finally:
                    if local_server is not None:
                        local_server.stop()

            assert result_zip_path is not None
            try:
                # 【关键修复】参考 demo_minerU 的实现，不依赖返回值
                _api_client.safe_extract_zip(result_zip_path, output_dir)
                logger.info("MinerU 解析结果已解压: output_dir_name=%s", output_dir.name)
            finally:
                result_zip_path.unlink(missing_ok=True)

            # 【关键修复】直接返回 output_dir，而不是 extracted_path
            return output_dir

        # 运行异步代码
        result_path = asyncio.run(run_async())

        # 【关键修复】检查结果路径是否有效
        if result_path is None:
            raise RuntimeError("MinerU conversion failed: result path is None")

        # 新 Processor 必须看到完整结果集，才能执行“恰好一个 Markdown”的严格校验。
        # 旧调用方仍保留返回首个 Markdown 的兼容语义，避免一次性破坏内部工具调用。
        if return_result_directory:
            return str(result_path)

        # 兼容旧调用方：找到排序后的第一个 Markdown 文件。
        md_files = sorted(Path(result_path).rglob("*.md"))
        if md_files:
            return str(md_files[0])
        return str(result_path)

    def _on_status_update(self, status_snapshot):
        """状态更新回调"""
        logger.info(
            "MinerU 解析任务状态更新: status=%s queued_ahead=%s",
            status_snapshot.status,
            status_snapshot.queued_ahead,
        )

    @staticmethod
    def _is_confirmed_provider_failure(
        error: Exception,
        *,
        provider_operation_id: str,
    ) -> bool:
        """识别供应商明确终态失败；网络错误和排队超时仍保持结果未知。"""

        message = str(error)
        return message.startswith(
            f"Task {provider_operation_id} failed "
        )


class MinerUDocumentProcessorAdapter:
    """把单个源 Artifact 解析为 Markdown 候选。

    每次调用只使用 ``step_key`` 对应的独占目录，不共享 Converter、HTTP Session 或
    Progress Callback。目录带所有权标记，成功或确定失败时清理；结果未知时保留现场。
    """

    def __init__(
        self,
        *,
        source_store: ArtifactStorePort,
        materialization_root: str | Path,
        operation_observer: MinerUOperationObserver,
        converter_factory: Callable[..., MinerUConverter] = MinerUConverter,
        api_url: str | None = None,
    ) -> None:
        if not isinstance(source_store, ArtifactStorePort):
            raise TypeError("source_store 必须实现 ArtifactStorePort")
        if not isinstance(operation_observer, MinerUOperationObserver):
            raise TypeError("operation_observer 必须实现 MinerUOperationObserver")
        self._source_store = source_store
        self._root = self._canonical_resolved(
            Path(materialization_root).expanduser()
        )
        self._observer = operation_observer
        self._converter_factory = converter_factory
        self._api_url = str(api_url).strip() if api_url else None
        self._api_mode = "remote" if self._api_url else "local"
        self._endpoint_fingerprint = mineru_endpoint_fingerprint(self._api_url)

    def process(self, request: DocumentProcessingRequest) -> ProcessorOutput:
        parameters = self._validate_profile(request)
        scratch = self._canonical_resolved(self._root / request.step_key)
        self._require_contained(scratch)
        if scratch.exists():
            raise DocumentProcessingError(
                "mineru_materialization_conflict",
                "MinerU 独占物化目录已存在，必须先完成恢复或清理",
                outcome_unknown=True,
            )
        scratch.mkdir(parents=True, exist_ok=False)
        (scratch / _MATERIALIZATION_MARKER).write_text(
            "DOCSENSE_MINERU_MATERIALIZATION_V1\n",
            encoding="ascii",
        )
        source_path = scratch / f"source{parameters['sourceSuffix']}"
        try:
            with self._source_store.open_reader(
                request.source_artifact
            ) as reader, source_path.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())

            converter = self._converter_factory(
                output_dir=str(scratch / "output"),
                operation_observer=self._observer,
            )
            result = self._canonical_resolved(Path(
                converter.convert_to_markdown(
                    input_path=str(source_path),
                    use_ocr=bool(parameters["useOcr"]),
                    lang=str(parameters["lang"]),
                    extract_images=bool(parameters["extractImages"]),
                    formula_enable=bool(parameters["formulaEnable"]),
                    table_enable=bool(parameters["tableEnable"]),
                    backend=str(parameters["backend"]),
                    # URL 只存在于运行时 Adapter，不进入 Profile/数据库。
                    api_url=self._api_url,
                    output_subdir="result",
                    operation_key=request.step_key,
                    return_result_directory=True,
                )
            ))
            if not result.exists():
                raise DocumentProcessingError(
                    "mineru_markdown_result_missing",
                    "MinerU 返回的结果路径不存在",
                )
            if result.is_dir():
                markdown_files = sorted(result.rglob("*.md"))
                if len(markdown_files) != 1:
                    raise DocumentProcessingError(
                        "mineru_markdown_result_ambiguous",
                        "MinerU 必须为单文档生成且只生成一个 Markdown",
                    )
                result = markdown_files[0]
            if result.suffix.lower() != ".md" or result.stat().st_size <= 0:
                raise DocumentProcessingError(
                    "mineru_markdown_result_invalid",
                    "MinerU 未生成非空 Markdown",
                )
            result.relative_to(scratch)
            result = self._make_markdown_self_contained(
                markdown_path=result,
                scratch=scratch,
            )
            logger.info(
                "MinerU Processor 已生成候选: task_id=%s step_key=%s bytes=%d",
                request.task_id,
                request.step_key[:12],
                result.stat().st_size,
            )
            return ProcessorOutput.with_cleanup(
                content=FileArtifactContent(result),
                kind=ArtifactKind.PREPARED,
                representation=DocumentRepresentation.MARKDOWN,
                media_type="text/markdown",
                cleanup=lambda: self._cleanup(scratch),
            )
        except DocumentProcessingError as exc:
            if not exc.outcome_unknown:
                self._cleanup(scratch)
            raise
        except Exception as exc:
            self._cleanup(scratch)
            logger.exception(
                "MinerU Processor 执行失败: task_id=%s step_key=%s",
                request.task_id,
                request.step_key[:12],
            )
            raise DocumentProcessingError(
                "mineru_processor_failed",
                "MinerU Processor 执行失败",
            ) from exc

    def _make_markdown_self_contained(
        self,
        *,
        markdown_path: Path,
        scratch: Path,
    ) -> Path:
        """把 MinerU 相对图片引用内联，确保临时目录清理后 Artifact 仍可独立读取。

        这里只处理图片引用，不重写链接或任意 HTML。每个图片和总图片体积均有限制；
        文件必须位于本次独占目录内，防止恶意 Markdown 读取宿主机其他文件。
        """

        try:
            source = markdown_path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise DocumentProcessingError(
                "mineru_markdown_encoding_invalid",
                "MinerU Markdown 不是合法 UTF-8",
            ) from exc

        cache: dict[str, str] = {}
        total_bytes = 0

        def embed(target: str) -> str:
            nonlocal total_bytes
            normalized_target = target[1:-1] if (
                target.startswith("<") and target.endswith(">")
            ) else target
            parsed = urlsplit(normalized_target)
            if (
                parsed.scheme.lower() in {"http", "https", "data"}
                or normalized_target.startswith("#")
            ):
                return target
            if parsed.scheme or parsed.netloc:
                raise DocumentProcessingError(
                    "mineru_image_reference_unsupported",
                    "MinerU Markdown 包含不受支持的图片引用",
                )

            decoded_path = unquote(parsed.path)
            candidate = self._canonical_resolved(
                markdown_path.parent / decoded_path
            )
            try:
                candidate.relative_to(scratch)
            except ValueError as exc:
                raise DocumentProcessingError(
                    "mineru_image_reference_escape",
                    "MinerU Markdown 图片引用越出任务独占目录",
                ) from exc
            if not candidate.is_file():
                raise DocumentProcessingError(
                    "mineru_image_reference_missing",
                    "MinerU Markdown 引用的图片不存在",
                )

            cache_key = str(candidate)
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

            size = candidate.stat().st_size
            if size <= 0 or size > _MAX_EMBEDDED_IMAGE_BYTES:
                raise DocumentProcessingError(
                    "mineru_image_size_invalid",
                    "MinerU Markdown 图片为空或超过单文件限制",
                )
            if total_bytes + size > _MAX_EMBEDDED_IMAGES_TOTAL_BYTES:
                raise DocumentProcessingError(
                    "mineru_images_total_size_exceeded",
                    "MinerU Markdown 图片总大小超过限制",
                )
            media_type, _encoding = mimetypes.guess_type(candidate.name)
            if media_type not in _SAFE_EMBEDDED_IMAGE_MEDIA_TYPES:
                raise DocumentProcessingError(
                    "mineru_image_media_type_unsupported",
                    "MinerU Markdown 图片类型不受支持",
                )

            payload = candidate.read_bytes()
            # stat 与读取之间文件可能变化，因此以真实读取长度再次执行门禁。
            if len(payload) <= 0 or len(payload) > _MAX_EMBEDDED_IMAGE_BYTES:
                raise DocumentProcessingError(
                    "mineru_image_size_invalid",
                    "MinerU Markdown 图片为空或超过单文件限制",
                )
            if total_bytes + len(payload) > _MAX_EMBEDDED_IMAGES_TOTAL_BYTES:
                raise DocumentProcessingError(
                    "mineru_images_total_size_exceeded",
                    "MinerU Markdown 图片总大小超过限制",
                )
            total_bytes += len(payload)
            data_uri = (
                f"data:{media_type};base64,"
                f"{base64.b64encode(payload).decode('ascii')}"
            )
            cache[cache_key] = data_uri
            return data_uri

        def replace_markdown(match: re.Match[str]) -> str:
            return (
                f"{match.group('prefix')}"
                f"{embed(match.group('target'))}"
                f"{match.group('suffix')}"
            )

        def replace_html(match: re.Match[str]) -> str:
            quote = match.group("quote")
            return (
                f"{match.group('prefix')}{quote}"
                f"{embed(match.group('target'))}{quote}"
            )

        rewritten = _MARKDOWN_IMAGE_PATTERN.sub(replace_markdown, source)
        rewritten = _HTML_IMAGE_PATTERN.sub(replace_html, rewritten)
        if not cache:
            return markdown_path

        target = scratch / "prepared-self-contained.md"
        with target.open("x", encoding="utf-8", newline="\n") as writer:
            writer.write(rewritten)
            writer.flush()
            os.fsync(writer.fileno())
        logger.info(
            "MinerU Markdown 图片已内联: image_count=%d total_bytes=%d",
            len(cache),
            total_bytes,
        )
        return target

    def _validate_profile(
        self,
        request: DocumentProcessingRequest,
    ) -> dict[str, object]:
        profile = request.profile
        if (
            profile.processor_id != MINERU_PROCESSOR_ID
            or profile.target_representation
            is not DocumentRepresentation.MARKDOWN
        ):
            raise DocumentProcessingError(
                "mineru_profile_mismatch",
                "请求不是 MinerU Markdown profile",
            )
        parameters = profile.to_dict()["parameters"]
        expected = {
            "apiMode",
            "backend",
            "endpointFingerprint",
            "extractImages",
            "formulaEnable",
            "lang",
            "sourceSuffix",
            "tableEnable",
            "useOcr",
        }
        if not isinstance(parameters, dict) or set(parameters) != expected:
            raise DocumentProcessingError(
                "mineru_profile_invalid",
                "MinerU profile 参数集合不合法",
            )
        if (
            parameters["apiMode"] != self._api_mode
            or parameters["endpointFingerprint"] != self._endpoint_fingerprint
        ):
            raise DocumentProcessingError(
                "mineru_profile_runtime_mismatch",
                "MinerU 冻结端点指纹与当前运行时不一致",
            )
        suffix = str(parameters["sourceSuffix"]).strip().lower()
        if (
            not suffix.startswith(".")
            or suffix.lstrip(".") not in SUPPORTED_INPUT_SUFFIXES
        ):
            raise DocumentProcessingError(
                "mineru_profile_invalid",
                "MinerU profile 源格式不受支持",
            )
        return parameters

    def _cleanup(self, scratch: Path) -> None:
        try:
            self._require_contained(scratch)
            marker = scratch / _MATERIALIZATION_MARKER
            if (
                scratch.parent != self._root
                or not marker.is_file()
                or marker.read_text(encoding="ascii")
                != "DOCSENSE_MINERU_MATERIALIZATION_V1\n"
            ):
                logger.warning(
                    "跳过不满足所有权条件的 MinerU 目录清理: directory_name=%s",
                    scratch.name,
                )
                return
            shutil.rmtree(scratch)
        except OSError:
            logger.warning(
                "MinerU 物化目录清理失败，将由巡检继续处理: directory_name=%s",
                scratch.name,
                exc_info=True,
            )

    def _require_contained(self, path: Path) -> None:
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise DocumentProcessingError(
                "mineru_materialization_path_escape",
                "MinerU 物化路径越出允许边界",
            ) from exc

    @staticmethod
    def _canonical_resolved(path: Path) -> Path:
        """统一 Windows 扩展长度路径与普通路径表示，避免等价路径误判逃逸。"""

        resolved = path.resolve()
        if os.name != "nt":
            return resolved
        text = str(resolved)
        if text.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + text[8:])
        if text.startswith("\\\\?\\"):
            return Path(text[4:])
        return resolved


__all__ = [
    "MINERU_PROCESSOR_FINGERPRINT",
    "MINERU_PROCESSOR_ID",
    "SUPPORTED_INPUT_SUFFIXES",
    "MinerUConverter",
    "MinerUDocumentProcessorAdapter",
    "MinerUOperationObserver",
    "build_mineru_profile",
    "mineru_endpoint_fingerprint",
]

