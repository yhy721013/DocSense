"""MHTML 浏览器 PDF 与纯文本 Markdown Processor。"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.modules.document_processing.adapters.content import (
    BytesArtifactContent,
    FileArtifactContent,
)
from app.modules.document_processing.domain import (
    ArtifactKind,
    DocumentProcessingError,
    DocumentProcessingRequest,
    DocumentRepresentation,
    ProcessingProfile,
    extract_mhtml_text,
    is_mhtml_content,
)
from app.modules.document_processing.ports import ArtifactStorePort, ProcessorOutput


logger = logging.getLogger(__name__)
MHTML_BROWSER_PROCESSOR_ID = "mhtml-browser-pdf"
MHTML_TEXT_PROCESSOR_ID = "mhtml-text-markdown"
_MARKER = ".docsense-mhtml-job"


class MHTMLBrowserConversionError(DocumentProcessingError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


class MHTMLBrowserOutcomeUnknownError(DocumentProcessingError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, outcome_unknown=True)


@dataclass(frozen=True, slots=True)
class _BrowserExecution:
    returncode: int
    timed_out: bool = False
    termination_confirmed: bool = True


def create_mhtml_browser_profile(*, browser_fingerprint: str) -> ProcessingProfile:
    """冻结已确认的降级、未知结果与 ``--no-sandbox`` 策略。"""

    return ProcessingProfile.create(
        processor_id=MHTML_BROWSER_PROCESSOR_ID,
        processor_fingerprint=browser_fingerprint,
        target_representation=DocumentRepresentation.PDF,
        parameters={
            "fallbackPolicy": "markdown_on_confirmed_failure_v1",
            "noSandbox": True,
            "unknownOutcomePolicy": "reconcile_then_fallback_v1",
        },
    )


def create_mhtml_text_profile() -> ProcessingProfile:
    return ProcessingProfile.create(
        processor_id=MHTML_TEXT_PROCESSOR_ID,
        processor_fingerprint="mhtml-main-content-v1",
        target_representation=DocumentRepresentation.MARKDOWN,
        parameters={"mode": "general", "newline": "lf"},
    )


class MHTMLToPDFConverter:
    """兼容旧签名的浏览器转换器，固定保留 ``--no-sandbox``。"""

    def __init__(
        self,
        *,
        browser_path: str | None = None,
        timeout_seconds: float = 60.0,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self.chrome_path = browser_path or self._find_browser()
        if not self.chrome_path:
            raise MHTMLBrowserConversionError(
                "mhtml_browser_unavailable",
                "未找到 Chrome 或 Edge 浏览器",
            )
        self._timeout_seconds = float(timeout_seconds)
        self._runner = runner

    @staticmethod
    def _find_browser() -> str | None:
        candidates: list[str] = []
        configured = os.environ.get("CHROME_PATH", "").strip()
        if configured:
            candidates.append(configured)
        for name in (
            "chrome.exe", "google-chrome.exe", "msedge.exe",
            "google-chrome", "google-chrome-stable", "chromium", "microsoft-edge",
        ):
            discovered = shutil.which(name)
            if discovered:
                candidates.append(discovered)
        if os.name == "nt":
            candidates.extend(
                [
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    os.path.expandvars(
                        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
                    ),
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                ]
            )
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
        return None

    def convert(self, mhtml_path: str, output_path: str | None = None) -> str:
        source = Path(mhtml_path)
        if not source.is_file():
            raise MHTMLBrowserConversionError(
                "mhtml_source_missing",
                "MHTML 文件不存在",
            )
        destination = (
            Path(output_path)
            if output_path
            else source.with_suffix(".pdf")
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.chrome_path,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-software-rasterizer",
            "--allow-file-access-from-files",
            f"--print-to-pdf={destination}",
            "--print-to-pdf-no-header",
            source.resolve().as_uri(),
        ]
        try:
            completed = self._run(command)
        except subprocess.TimeoutExpired as exc:
            # 注入的兼容 runner 无法提供进程树身份，超时只能按未知结果处理。
            raise MHTMLBrowserOutcomeUnknownError(
                "mhtml_browser_timeout_outcome_unknown",
                "浏览器转换超时，结果需要协调",
            ) from exc
        except OSError as exc:
            raise MHTMLBrowserConversionError(
                "mhtml_browser_start_failed",
                "浏览器启动失败",
            ) from exc
        if completed.timed_out and not completed.termination_confirmed:
            raise MHTMLBrowserOutcomeUnknownError(
                "mhtml_browser_termination_outcome_unknown",
                "浏览器进程树终止结果未知",
            )
        if completed.timed_out and not self._valid_pdf(destination):
            raise MHTMLBrowserConversionError(
                "mhtml_browser_timeout_confirmed",
                "浏览器超时且进程树已确认终止",
            )
        if completed.returncode != 0 and not destination.is_file():
            raise MHTMLBrowserConversionError(
                "mhtml_browser_nonzero_exit",
                "浏览器明确失败且未生成 PDF",
            )
        if not self._valid_pdf(destination):
            raise MHTMLBrowserConversionError(
                "mhtml_pdf_invalid",
                "浏览器未生成有效 PDF",
            )
        logger.info(
            "MHTML 浏览器 PDF 已生成: browser=%s bytes=%d no_sandbox=true",
            Path(self.chrome_path).name,
            destination.stat().st_size,
        )
        return str(destination)

    def _run(self, command: list[str]) -> _BrowserExecution:
        if self._runner is not None:
            completed = self._runner(
                command,
                capture_output=True,
                timeout=self._timeout_seconds,
                encoding="utf-8",
                errors="replace",
            )
            return _BrowserExecution(returncode=int(completed.returncode))

        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **popen_kwargs)
        try:
            process.communicate(timeout=self._timeout_seconds)
            return _BrowserExecution(returncode=int(process.returncode or 0))
        except subprocess.TimeoutExpired:
            confirmed = self._terminate_process_tree(process)
            return _BrowserExecution(
                returncode=int(process.returncode or -1),
                timed_out=True,
                termination_confirmed=confirmed,
            )

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> bool:
        """有界终止浏览器树；只有确认退出后才允许 Markdown 降级。"""

        try:
            if os.name == "nt":
                completed = subprocess.run(
                    [
                        "taskkill",
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    return False
                return completed.returncode == 0 or process.poll() is not None

            process_group = os.getpgid(process.pid)
            os.killpg(process_group, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process_group, signal.SIGKILL)
                process.wait(timeout=5)
            return process.poll() is not None
        except (OSError, subprocess.SubprocessError):
            logger.warning(
                "MHTML 浏览器进程树终止无法确认: pid=%s",
                process.pid,
                exc_info=True,
            )
            return False

    @staticmethod
    def _valid_pdf(path: Path) -> bool:
        try:
            if not path.is_file() or path.stat().st_size < 8:
                return False
            with path.open("rb") as reader:
                return reader.read(5) == b"%PDF-"
        except OSError:
            return False


class MHTMLBrowserPDFProcessorAdapter:
    def __init__(
        self,
        *,
        source_store: ArtifactStorePort,
        converter: MHTMLToPDFConverter,
        scratch_root: str | Path,
    ) -> None:
        self._store = source_store
        self._converter = converter
        self._root = Path(scratch_root).resolve()

    def process(self, request: DocumentProcessingRequest) -> ProcessorOutput:
        if request.profile.processor_id != MHTML_BROWSER_PROCESSOR_ID:
            raise DocumentProcessingError(
                "mhtml_browser_profile_mismatch",
                "MHTML 浏览器 Profile 不匹配",
            )
        parameters = request.profile.to_dict()["parameters"]
        if parameters != {
            "fallbackPolicy": "markdown_on_confirmed_failure_v1",
            "noSandbox": True,
            "unknownOutcomePolicy": "reconcile_then_fallback_v1",
        }:
            raise DocumentProcessingError(
                "mhtml_browser_profile_invalid",
                "MHTML 浏览器 Profile 策略不合法",
            )
        job = self._create_job(request.step_key)
        source = job / "source.mhtml"
        output = job / "output.pdf"
        try:
            with self._store.open_reader(request.source_artifact) as reader, source.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            # 只读取识别所需的固定文件头。不能使用 ``read_bytes()[:N]``，因为前者
            # 会先把整个大文件载入内存，再执行切片。
            with source.open("rb") as reader:
                header = reader.read(1024)
            if not is_mhtml_content(file_name=source.name, header=header):
                raise DocumentProcessingError("mhtml_signature_invalid", "源 Artifact 不是 MHTML")
            self._converter.convert(str(source), str(output))
            return ProcessorOutput.with_cleanup(
                content=FileArtifactContent(output),
                kind=ArtifactKind.NORMALIZED,
                representation=DocumentRepresentation.PDF,
                media_type="application/pdf",
                cleanup=lambda: self._cleanup_job(job),
            )
        except DocumentProcessingError as exc:
            if not exc.outcome_unknown:
                self._cleanup_job(job)
            else:
                logger.warning(
                    "MHTML 浏览器结果未知，保留确定性 scratch: "
                    "task_id=%s step_key=%s",
                    request.task_id,
                    request.step_key[:12],
                )
            raise
        except Exception as exc:
            self._cleanup_job(job)
            raise DocumentProcessingError(
                "mhtml_browser_processor_failed",
                "MHTML 浏览器 Processor 失败",
            ) from exc

    def _create_job(self, step_key: str) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        job = (self._root / f"job-{step_key}").resolve()
        job.relative_to(self._root)
        try:
            # 独占创建是任务隔离边界：无论既有目录是否带有效标记，都不能由本次
            # 执行“接管”。它可能是上一执行结果未知时保留的对账现场。
            job.mkdir(exist_ok=False)
        except FileExistsError as exc:
            logger.warning(
                "MHTML scratch 已存在，拒绝接管: job_name=%s",
                job.name,
            )
            raise DocumentProcessingError(
                "mhtml_scratch_ownership_conflict",
                "MHTML scratch 已存在，必须先完成恢复或清理",
                outcome_unknown=True,
            ) from exc

        marker = job / _MARKER
        try:
            with marker.open("x", encoding="ascii", newline="\n") as writer:
                writer.write("DOCSENSE_MHTML_JOB_V1\n")
                writer.flush()
                os.fsync(writer.fileno())
        except OSError as exc:
            # 目录由本次调用刚创建；仅在仍为空时回收，绝不递归删除未知内容。
            try:
                job.rmdir()
            except OSError:
                logger.warning(
                    "MHTML 所有权标记写入失败且空目录无法回收: job_name=%s",
                    job.name,
                    exc_info=True,
                )
            raise DocumentProcessingError(
                "mhtml_scratch_marker_failed",
                "MHTML scratch 所有权标记创建失败",
            ) from exc
        return job

    def _cleanup_job(self, job: Path) -> bool:
        try:
            candidate = job.resolve()
            candidate.relative_to(self._root)
            marker = candidate / _MARKER
            if (
                candidate.parent != self._root
                or not candidate.name.startswith("job-")
                or marker.read_text(encoding="ascii") != "DOCSENSE_MHTML_JOB_V1\n"
            ):
                return False
            shutil.rmtree(candidate)
            return True
        except (OSError, ValueError):
            logger.warning("MHTML scratch 清理失败: job_name=%s", job.name, exc_info=True)
            return False


class MHTMLTextProcessorAdapter:
    """纯规则 Markdown 降级，不启动浏览器或远端服务。"""

    def __init__(self, *, source_store: ArtifactStorePort) -> None:
        self._store = source_store

    def process(self, request: DocumentProcessingRequest) -> ProcessorOutput:
        if request.profile.processor_id != MHTML_TEXT_PROCESSOR_ID:
            raise DocumentProcessingError(
                "mhtml_text_profile_mismatch",
                "MHTML 文本 Profile 不匹配",
            )
        with self._store.open_reader(request.source_artifact) as reader:
            payload = reader.read()
        text = extract_mhtml_text(payload)
        return ProcessorOutput(
            content=BytesArtifactContent((text + "\n").encode("utf-8")),
            kind=ArtifactKind.NORMALIZED,
            representation=DocumentRepresentation.MARKDOWN,
            media_type="text/markdown",
            warnings=("mhtml_browser_confirmed_failure_fallback",),
        )


def convert_mhtml_to_pdf(
    mhtml_path: str,
    output_path: str | None = None,
    **_: object,
) -> str:
    return MHTMLToPDFConverter().convert(mhtml_path, output_path)


__all__ = [
    "MHTMLBrowserConversionError",
    "MHTMLBrowserOutcomeUnknownError",
    "MHTMLBrowserPDFProcessorAdapter",
    "MHTMLTextProcessorAdapter",
    "MHTMLToPDFConverter",
    "convert_mhtml_to_pdf",
    "create_mhtml_browser_profile",
    "create_mhtml_text_profile",
]
