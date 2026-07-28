"""Fail-closed LibreOffice headless adapter for legacy Office documents."""

from __future__ import annotations

import logging
import ntpath
import os
import posixpath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Sequence

from .domain import (
    LegacyOfficeConfig,
    LegacyOfficeConversionError,
    LegacyOfficePreparationResult,
    _CleanupLease,
)


logger = logging.getLogger(__name__)

_OLE2_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
_OWNED_JOB_MARKER_NAME = ".docsense-legacy-office-job"
_OWNED_JOB_MARKER_VALUE = "DOCSENSE_LEGACY_OFFICE_JOB_V1\n"
# 标记文件必须以二进制方式写入，避免 Windows 文本模式把 LF 扩展成 CRLF，进而让
# 本进程刚创建的任务目录无法通过所有权校验。兼容集合仅额外接受旧 Windows 版本
# 确实可能写出的 CRLF 字节，不能放宽为任意文本或忽略空白，否则会削弱删除边界。
_OWNED_JOB_MARKER_BYTES = _OWNED_JOB_MARKER_VALUE.encode("ascii")
_OWNED_JOB_MARKER_COMPATIBLE_BYTES = frozenset(
    {
        _OWNED_JOB_MARKER_BYTES,
        _OWNED_JOB_MARKER_BYTES.replace(b"\n", b"\r\n"),
    }
)
_LOG_DIAGNOSTIC_LIMIT = 2048
_PROCESS_TREE_GRACE_SECONDS = 2.0
_PROCESS_OUTPUT_CAPTURE_BYTES = 64 * 1024
_PROCESS_OUTPUT_READ_BYTES = 8 * 1024
_OOXML_VALIDATION_TIMEOUT_SECONDS = 120.0
_OOXML_MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
_OOXML_VALIDATOR_EXIT_INVALID_ZIP = 20
_OOXML_VALIDATOR_EXIT_CORRUPT_ZIP = 21
_OOXML_VALIDATOR_EXIT_REQUIRED_MEMBER_MISSING = 22
_OOXML_VALIDATOR_EXIT_UNCOMPRESSED_LIMIT_EXCEEDED = 23
_OOXML_VALIDATOR_EXIT_ENCRYPTED_MEMBER = 24
_OOXML_VALIDATOR_EXIT_INVALID_ARGUMENT = 25
_OOXML_VALIDATOR_EXIT_UNEXPECTED_FAILURE = 26
_OOXML_VALIDATOR_EXIT_UNSAFE_MEMBER = 27
_OOXML_VALIDATOR_EXIT_UNSUPPORTED_COMPRESSION = 28
_OOXML_VALIDATOR_EXIT_DUPLICATE_MEMBER = 29
_OOXML_VALIDATOR_EXIT_ZIP_DIRECTORY_LIMIT_EXCEEDED = 30
_OOXML_VALIDATOR_EXIT_INVALID_ZIP_METADATA = 31

_POSIX_SUBPROCESS_ENVIRONMENT_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "PATH",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "__CF_USER_TEXT_ENCODING",
    }
)
_WINDOWS_SUBPROCESS_ENVIRONMENT_KEYS = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "LANG",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)


def _ooxml_validator_script_path() -> Path:
    """Return the trusted standalone validator shipped with DocSense."""

    return Path(__file__).with_name("ooxml_validator.py")


@dataclass(frozen=True, slots=True)
class _LegacyOfficeFormat:
    source_suffix: str
    target_suffix: str
    convert_filter: str
    required_member: str


_LEGACY_FORMATS: Mapping[str, _LegacyOfficeFormat] = {
    ".doc": _LegacyOfficeFormat(
        source_suffix=".doc",
        target_suffix=".docx",
        convert_filter="docx:Office Open XML Text",
        required_member="word/document.xml",
    ),
    ".ppt": _LegacyOfficeFormat(
        source_suffix=".ppt",
        target_suffix=".pptx",
        convert_filter="pptx:Impress MS PowerPoint 2007 XML",
        required_member="ppt/presentation.xml",
    ),
    ".xls": _LegacyOfficeFormat(
        source_suffix=".xls",
        target_suffix=".xlsx",
        convert_filter="xlsx:Calc Office Open XML",
        required_member="xl/workbook.xml",
    ),
}


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class _CommandTimedOut(RuntimeError):
    pass


class _BoundedPipeCollector:
    """Continuously drain one pipe while retaining only a bounded tail."""

    def __init__(
        self,
        stream: BinaryIO | None,
        *,
        capture_limit: int = _PROCESS_OUTPUT_CAPTURE_BYTES,
    ) -> None:
        self._stream = stream
        self._capture_limit = capture_limit
        self._buffer = bytearray()
        self._discarded_bytes = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._drain,
            name="legacy-office-output-drain",
            daemon=True,
        )

    def start(self) -> None:
        if self._stream is not None:
            self._thread.start()

    def wait_for_eof(self, timeout_seconds: float) -> bool:
        if self._stream is None:
            return True
        self._thread.join(timeout=max(0.0, timeout_seconds))
        return not self._thread.is_alive()

    def finish(self) -> bytes:
        if self._stream is None:
            return b""
        thread_alive = self._thread.is_alive()
        if thread_alive:
            logger.warning(
                "Legacy Office output drain did not reach EOF after process termination"
            )
        with self._lock:
            captured = bytes(self._buffer)
            discarded_bytes = self._discarded_bytes
        if not thread_alive:
            try:
                self._stream.close()
            except OSError:
                pass
        if discarded_bytes <= 0:
            return captured
        marker = (
            f"[output truncated; discarded {discarded_bytes} bytes]\n"
        ).encode("ascii")
        if len(marker) >= self._capture_limit:
            return marker[: self._capture_limit]
        keep_bytes = self._capture_limit - len(marker)
        return marker + captured[-keep_bytes:]

    def _drain(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(_PROCESS_OUTPUT_READ_BYTES)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                self._append(bytes(chunk))
        except (OSError, ValueError):
            # The process may close the pipe while timeout cleanup is running.
            return

    def _append(self, chunk: bytes) -> None:
        with self._lock:
            if len(chunk) >= self._capture_limit:
                self._discarded_bytes += (
                    len(self._buffer) + len(chunk) - self._capture_limit
                )
                self._buffer[:] = chunk[-self._capture_limit :]
                return
            overflow = len(self._buffer) + len(chunk) - self._capture_limit
            if overflow > 0:
                del self._buffer[:overflow]
                self._discarded_bytes += overflow
            self._buffer.extend(chunk)


def is_legacy_office_path(path: str | Path) -> bool:
    """Return whether a path has a supported Office 97--2003 suffix."""

    return Path(str(path)).suffix.lower() in _LEGACY_FORMATS


def _is_absolute_path(value: str, platform_name: str) -> bool:
    if platform_name.startswith("win"):
        return ntpath.isabs(value)
    # 路径语义必须由目标部署平台决定，不能由当前执行测试的宿主机决定。例如在 Windows
    # 上静态验证 macOS 资产时，``Path('/Applications/...')`` 不是 Windows 绝对路径，
    # 但它仍是合法的 macOS 配置。
    return posixpath.isabs(value)


def _standard_executable_candidates(
    platform_name: str,
    environment: Mapping[str, str],
) -> list[str]:
    candidates: list[str] = []
    if platform_name.startswith("win"):
        for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            root = str(environment.get(variable, "")).strip()
            if root:
                program_root = ntpath.join(root, "LibreOffice", "program")
                candidates.extend(
                    [
                        ntpath.join(program_root, "soffice.com"),
                        ntpath.join(program_root, "soffice.exe"),
                    ]
                )
        candidates.extend(
            [
                r"C:\Program Files\LibreOffice\program\soffice.com",
                r"C:\Program Files\LibreOffice\program\soffice.exe",
                r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
                r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
            ]
        )
    elif platform_name == "darwin":
        candidates.append(
            "/Applications/LibreOffice.app/Contents/MacOS/soffice"
        )
        user_home = str(environment.get("HOME", "")).strip()
        if user_home:
            # 候选路径属于目标 macOS，必须使用 POSIX 拼接；否则在 Windows 上执行
            # 离线资产审计时会被宿主 ``Path`` 改写为反斜杠，导致同一配置无法复核。
            candidates.append(
                posixpath.join(
                    user_home,
                    "Applications",
                    "LibreOffice.app",
                    "Contents",
                    "MacOS",
                    "soffice",
                )
            )

    deduplicated: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.lower() if platform_name.startswith("win") else candidate
        if key not in seen:
            seen.add(key)
            deduplicated.append(candidate)
    return deduplicated


def discover_libreoffice_executable(
    explicit: str | None = None,
    *,
    platform_name: str | None = None,
    environment: Mapping[str, str] | None = None,
    path_is_file: Callable[[str], bool] | None = None,
    path_is_executable: Callable[[str], bool] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """Find ``soffice`` in the approved precedence order.

    An explicitly configured path is authoritative: a bad explicit value is an
    error and never silently falls through to another host installation.
    """

    selected_platform = platform_name or sys.platform
    selected_environment = environment if environment is not None else os.environ
    selected_is_file = path_is_file or os.path.isfile
    selected_is_executable = path_is_executable or (
        lambda value: selected_platform.startswith("win")
        or os.access(value, os.X_OK)
    )
    selected_which = which or shutil.which

    def usable(candidate: str) -> bool:
        return bool(
            selected_is_file(candidate)
            and selected_is_executable(candidate)
        )

    configured = str(explicit or "").strip()
    if configured:
        if not _is_absolute_path(configured, selected_platform):
            raise LegacyOfficeConversionError(
                "executable_path_not_absolute",
                diagnostic="configured executable must be an absolute path",
            )
        if not usable(configured):
            raise LegacyOfficeConversionError(
                "executable_not_found",
                diagnostic="configured executable is missing or not executable",
            )
        return configured

    for candidate in _standard_executable_candidates(
        selected_platform,
        selected_environment,
    ):
        if usable(candidate):
            return candidate

    for command_name in ("soffice", "libreoffice"):
        candidate = selected_which(command_name)
        if candidate and usable(candidate):
            return candidate

    raise LegacyOfficeConversionError(
        "executable_not_found",
        diagnostic="no approved LibreOffice executable was found",
    )


def _as_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8", errors="replace")


def _minimal_subprocess_environment(
    environment: Mapping[str, str],
    *,
    platform_name: str,
) -> dict[str, str]:
    """Keep host prerequisites without forwarding DocSense credentials."""

    if platform_name.startswith("win"):
        allowed = _WINDOWS_SUBPROCESS_ENVIRONMENT_KEYS
        selected = {
            key.upper(): str(value)
            for key, value in environment.items()
            if key.upper() in allowed
        }
    else:
        selected = {
            key: str(value)
            for key, value in environment.items()
            if key in _POSIX_SUBPROCESS_ENVIRONMENT_KEYS or key.startswith("LC_")
        }
    # Headless conversion must not select an installed GUI backend.  This is
    # not a substitute for the hardened profile; it only narrows process state.
    selected["SAL_USE_VCLPLUGIN"] = "svp"
    return selected


def _sanitize_for_log(
    value: bytes | str,
    *,
    sensitive_values: Sequence[str] = (),
) -> str:
    """Return one bounded diagnostic line without host paths."""

    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    for sensitive in sorted(
        (item for item in sensitive_values if item),
        key=len,
        reverse=True,
    ):
        text = text.replace(sensitive, "<redacted-path>")
        try:
            text = text.replace(Path(sensitive).as_uri(), "<redacted-path>")
        except (ValueError, OSError):
            pass

    # Absolute POSIX paths and Windows drive/UNC paths are never useful in a
    # callback-facing error.  Preserve only ordinary prose and filenames.
    text = re.sub(
        r"(?i)(?:file://)?(?:[a-z]:[\\/]|/|\\\\)[^\s\"'<>]*",
        "<redacted-path>",
        text,
    )
    text = "".join(character if character.isprintable() else " " for character in text)
    text = " ".join(text.split())
    if len(text) > _LOG_DIAGNOSTIC_LIMIT:
        return text[: _LOG_DIAGNOSTIC_LIMIT - 3] + "..."
    return text


def _hardened_registry_xml() -> str:
    """Return settings consumed by the task-private LibreOffice profile."""

    return """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"
           xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
    <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
    <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="DisablePythonRuntime" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="DisableOLEAutomation" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="SecureURL" oor:op="fuse"><value/></prop>
    <prop oor:name="AllowedDocumentEventURLs" oor:op="fuse"><value/></prop>
  </item>
  <item oor:path="/org.openoffice.Office.Writer/Content/Update">
    <!-- Writer's Link=2 is the 26.2 "Never update links" value. -->
    <prop oor:name="Link" oor:op="fuse"><value>2</value></prop>
  </item>
  <item oor:path="/org.openoffice.Office.Calc/Content/Update">
    <!-- Calc uses a different enum: LM_NEVER=1. -->
    <prop oor:name="Link" oor:op="fuse"><value>1</value></prop>
  </item>
</oor:items>
"""


class LibreOfficeLegacyOfficePreparer:
    """Thread-safe implementation backed by a local LibreOffice process."""

    def __init__(
        self,
        config: LegacyOfficeConfig,
        *,
        platform_name: str | None = None,
        environment: Mapping[str, str] | None = None,
        process_factory: Callable[..., Any] | None = None,
        taskkill_runner: Callable[..., Any] | None = None,
        ooxml_validation_runner: Callable[..., Any] | None = None,
        path_is_file: Callable[[str], bool] | None = None,
        path_is_executable: Callable[[str], bool] | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self.config = config
        self._platform_name = platform_name or sys.platform
        self._environment = dict(environment if environment is not None else os.environ)
        self._subprocess_environment = _minimal_subprocess_environment(
            self._environment,
            platform_name=self._platform_name,
        )
        self._process_factory = process_factory or subprocess.Popen
        self._taskkill_runner = taskkill_runner or subprocess.run
        self._ooxml_validation_runner = (
            ooxml_validation_runner or subprocess.run
        )
        self._path_is_file = path_is_file
        self._path_is_executable = path_is_executable
        self._which = which
        self._conversion_slots = threading.BoundedSemaphore(config.max_concurrency)
        self._preflight_lock = threading.Lock()
        self._executable: str | None = None
        self._version: str | None = None

    def preflight(self) -> str | None:
        """Validate the configured stable LibreOffice series once.

        Disabled configurations return immediately and do not inspect standard
        paths, ``PATH``, or spawn a process.
        """

        if not self.config.enabled:
            return None
        if self._version is not None:
            return self._version

        with self._preflight_lock:
            if self._version is not None:
                return self._version
            executable = discover_libreoffice_executable(
                self.config.executable,
                platform_name=self._platform_name,
                environment=self._environment,
                path_is_file=self._path_is_file,
                path_is_executable=self._path_is_executable,
                which=self._which,
            )
            try:
                completed = self._run_process(
                    [executable, "--version"],
                    cwd=None,
                    timeout_seconds=self.config.timeout_seconds,
                )
            except _CommandTimedOut as exc:
                raise LegacyOfficeConversionError(
                    "version_probe_timeout",
                    diagnostic="LibreOffice version probe timed out",
                ) from exc
            except OSError as exc:
                raise LegacyOfficeConversionError(
                    "version_probe_failed",
                    diagnostic=_sanitize_for_log(
                        str(exc),
                        sensitive_values=(executable,),
                    ),
                ) from exc

            output = _as_bytes(completed.stdout) + b"\n" + _as_bytes(completed.stderr)
            diagnostic = _sanitize_for_log(
                output,
                sensitive_values=(executable,),
            )
            if completed.returncode != 0:
                raise LegacyOfficeConversionError(
                    "version_probe_failed",
                    diagnostic=diagnostic,
                )

            if re.search(r"\bLibreOfficeDev\b", diagnostic, flags=re.IGNORECASE):
                raise LegacyOfficeConversionError(
                    "development_version_rejected",
                    diagnostic=diagnostic,
                )
            if re.search(
                r"\b(?:alpha\d*|beta\d*|rc\d*|development|nightly)\b",
                diagnostic,
                flags=re.IGNORECASE,
            ):
                raise LegacyOfficeConversionError(
                    "prerelease_version_rejected",
                    diagnostic=diagnostic,
                )
            match = re.search(
                r"\bLibreOffice\s+(\d+(?:\.\d+){2,3})\b",
                diagnostic,
            )
            if match is None:
                raise LegacyOfficeConversionError(
                    "unrecognized_version",
                    diagnostic=diagnostic,
                )
            version = match.group(1)
            allowed_prefix = self.config.allowed_version_series + "."
            if not version.startswith(allowed_prefix):
                raise LegacyOfficeConversionError(
                    "version_not_allowed",
                    diagnostic=diagnostic,
                )

            self._executable = executable
            self._version = version
            return version

    def prepare(
        self,
        source_path: str | Path,
        *,
        job_id: str,
    ) -> LegacyOfficePreparationResult:
        """Convert a legacy file or return an unchanged non-legacy path."""

        del job_id  # Job directory names are intentionally random and opaque.
        original_path = Path(source_path)
        source_suffix = original_path.suffix.lower()
        format_spec = _LEGACY_FORMATS.get(source_suffix)
        if format_spec is None:
            return LegacyOfficePreparationResult(
                original_path=original_path,
                prepared_path=original_path,
                source_suffix=source_suffix,
                target_suffix=source_suffix,
                libreoffice_version=None,
                converted=False,
            )
        if not self.config.enabled:
            raise LegacyOfficeConversionError("feature_disabled")

        version = self.preflight()
        if version is None:  # Defensive: enabled preflight must return a version.
            raise LegacyOfficeConversionError("version_probe_failed")

        self._validate_source(original_path)
        job_path: Path | None = None
        try:
            job_path = self._create_job_directory()
            input_path = job_path / f"input{format_spec.source_suffix}"
            output_directory = job_path / "out"
            profile_directory = job_path / "profile"
            output_directory.mkdir(mode=0o700)
            self._write_hardened_profile(profile_directory)
            process_environment = self._create_private_process_environment(job_path)
            self._copy_validated_source(original_path, input_path)

            with self._conversion_slots:
                self._convert(
                    input_path=input_path,
                    output_directory=output_directory,
                    profile_directory=profile_directory,
                    format_spec=format_spec,
                    sensitive_source=original_path,
                    process_environment=process_environment,
                )
                # ZIP validation can consume substantial CPU and decompressed
                # bytes.  Keep conversion, validation, and publication under
                # one permit so max_concurrency bounds the complete local
                # processing unit rather than only the LibreOffice child.
                prepared_path = self._validate_output(
                    output_directory,
                    format_spec,
                )
                prepared_path = self._publish_unique_prepared_path(
                    prepared_path,
                    format_spec,
                )
            return LegacyOfficePreparationResult(
                original_path=original_path,
                prepared_path=prepared_path,
                source_suffix=format_spec.source_suffix,
                target_suffix=format_spec.target_suffix,
                libreoffice_version=version,
                converted=True,
                _lease=_CleanupLease(lambda: self._cleanup_owned_job(job_path)),
            )
        except LegacyOfficeConversionError:
            if job_path is not None:
                self._cleanup_owned_job(job_path)
            raise
        except Exception as exc:
            diagnostic = _sanitize_for_log(
                str(exc),
                sensitive_values=(
                    str(original_path),
                    str(job_path or ""),
                    str(self._executable or ""),
                ),
            )
            logger.error(
                "Legacy Office conversion failed code=unexpected diagnostic=%s",
                diagnostic,
            )
            if job_path is not None:
                self._cleanup_owned_job(job_path)
            raise LegacyOfficeConversionError(
                "unexpected_conversion_failure",
                diagnostic=diagnostic,
            ) from exc

    def sweep_stale_jobs(self) -> int:
        """Remove only directly owned job directories under the configured root."""

        root = self.config.jobs_root
        if not root.exists():
            return 0
        if root.is_symlink() or not root.is_dir():
            logger.warning(
                "Legacy Office stale-job sweep skipped: jobs root is not a safe directory"
            )
            return 0

        removed = 0
        try:
            entries = list(root.iterdir())
        except OSError as exc:
            logger.warning(
                "Legacy Office stale-job sweep failed diagnostic=%s",
                _sanitize_for_log(str(exc), sensitive_values=(str(root),)),
            )
            return 0
        for entry in entries:
            if (
                not entry.name.startswith("job-")
                or entry.is_symlink()
                or not entry.is_dir()
                or not self._is_owned_job(entry)
            ):
                continue
            if self._cleanup_owned_job(entry):
                removed += 1
        return removed

    def _validate_source(self, source_path: Path) -> None:
        try:
            file_stat = source_path.stat()
        except OSError as exc:
            raise LegacyOfficeConversionError(
                "input_unreadable",
                diagnostic="input is missing or unreadable",
            ) from exc
        if not stat.S_ISREG(file_stat.st_mode):
            raise LegacyOfficeConversionError(
                "input_not_regular_file",
                diagnostic="input is not a regular file",
            )
        if file_stat.st_size <= 0:
            raise LegacyOfficeConversionError(
                "input_empty",
                diagnostic="input is empty",
            )
        if file_stat.st_size > self.config.max_input_bytes:
            raise LegacyOfficeConversionError(
                "input_too_large",
                diagnostic="input exceeds the configured byte limit",
            )
        try:
            with source_path.open("rb") as source:
                magic = source.read(len(_OLE2_MAGIC))
        except OSError as exc:
            raise LegacyOfficeConversionError(
                "input_unreadable",
                diagnostic="input is unreadable",
            ) from exc
        if magic != _OLE2_MAGIC:
            raise LegacyOfficeConversionError(
                "invalid_ole2_signature",
                diagnostic="input does not contain the required OLE2 signature",
            )

    def _ensure_jobs_root(self) -> Path:
        root = self.config.jobs_root
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise LegacyOfficeConversionError(
                "jobs_root_unavailable",
                diagnostic="conversion jobs root could not be created",
            ) from exc
        if root.is_symlink() or not root.is_dir():
            raise LegacyOfficeConversionError(
                "jobs_root_unsafe",
                diagnostic="conversion jobs root is not a safe directory",
            )
        try:
            root.chmod(0o700)
        except OSError:
            # ACLs or Windows may not support POSIX modes; directory ownership
            # and random task names still provide the containment boundary.
            pass
        return root

    def _create_job_directory(self) -> Path:
        root = self._ensure_jobs_root()
        job_path: Path | None = None
        try:
            job_path = Path(tempfile.mkdtemp(prefix="job-", dir=str(root)))
            job_path.chmod(0o700)
            marker = job_path / _OWNED_JOB_MARKER_NAME
            marker.write_bytes(_OWNED_JOB_MARKER_BYTES)
            try:
                marker.chmod(0o600)
            except OSError:
                pass
            return job_path
        except OSError as exc:
            if job_path is not None:
                try:
                    shutil.rmtree(job_path)
                except OSError:
                    logger.warning(
                        "Legacy Office incomplete job initialization cleanup failed"
                    )
            raise LegacyOfficeConversionError(
                "job_directory_unavailable",
                diagnostic="task-private conversion directory could not be created",
            ) from exc

    def _copy_validated_source(self, source_path: Path, target_path: Path) -> None:
        copied_bytes = 0
        try:
            with source_path.open("rb") as source, target_path.open("xb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    copied_bytes += len(chunk)
                    if copied_bytes > self.config.max_input_bytes:
                        raise LegacyOfficeConversionError(
                            "input_too_large",
                            diagnostic="input changed and exceeded the byte limit",
                        )
                    target.write(chunk)
            try:
                target_path.chmod(0o600)
            except OSError:
                pass
        except LegacyOfficeConversionError:
            raise
        except OSError as exc:
            raise LegacyOfficeConversionError(
                "input_copy_failed",
                diagnostic="input could not be copied into the conversion boundary",
            ) from exc
        if copied_bytes <= 0:
            raise LegacyOfficeConversionError(
                "input_empty",
                diagnostic="input became empty while being copied",
            )
        try:
            with target_path.open("rb") as copied:
                copied_magic = copied.read(len(_OLE2_MAGIC))
        except OSError as exc:
            raise LegacyOfficeConversionError(
                "input_copy_failed",
                diagnostic="copied input could not be revalidated",
            ) from exc
        if copied_magic != _OLE2_MAGIC:
            raise LegacyOfficeConversionError(
                "invalid_ole2_signature",
                diagnostic="copied input failed the OLE2 signature recheck",
            )

    def _write_hardened_profile(self, profile_directory: Path) -> None:
        try:
            user_directory = profile_directory / "user"
            user_directory.mkdir(parents=True, mode=0o700)
            profile_file = user_directory / "registrymodifications.xcu"
            profile_file.write_text(_hardened_registry_xml(), encoding="utf-8")
            try:
                profile_directory.chmod(0o700)
                user_directory.chmod(0o700)
                profile_file.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            raise LegacyOfficeConversionError(
                "profile_initialization_failed",
                diagnostic="hardened LibreOffice profile could not be created",
            ) from exc

    def _create_private_process_environment(
        self,
        job_path: Path,
    ) -> dict[str, str]:
        """Create job-owned home/temp locations and return process overrides."""

        home_directory = job_path / "home"
        temp_directory = job_path / "tmp"
        try:
            home_directory.mkdir(mode=0o700)
            temp_directory.mkdir(mode=0o700)
            if self._platform_name.startswith("win"):
                app_data = home_directory / "AppData" / "Roaming"
                local_app_data = home_directory / "AppData" / "Local"
                app_data.mkdir(parents=True, mode=0o700)
                local_app_data.mkdir(parents=True, mode=0o700)
            else:
                app_data = None
                local_app_data = None
            for directory in (
                home_directory,
                temp_directory,
                app_data,
                local_app_data,
            ):
                if directory is not None:
                    try:
                        directory.chmod(0o700)
                    except OSError:
                        pass
        except OSError as exc:
            raise LegacyOfficeConversionError(
                "private_environment_initialization_failed",
                diagnostic="task-private process home or temp could not be created",
            ) from exc

        if self._platform_name.startswith("win"):
            return {
                "USERPROFILE": str(home_directory),
                "TEMP": str(temp_directory),
                "TMP": str(temp_directory),
                "APPDATA": str(app_data),
                "LOCALAPPDATA": str(local_app_data),
            }
        return {
            "HOME": str(home_directory),
            "TEMP": str(temp_directory),
            "TMP": str(temp_directory),
            "TMPDIR": str(temp_directory),
        }

    def _convert(
        self,
        *,
        input_path: Path,
        output_directory: Path,
        profile_directory: Path,
        format_spec: _LegacyOfficeFormat,
        sensitive_source: Path,
        process_environment: Mapping[str, str],
    ) -> _CommandResult:
        executable = self._executable
        if executable is None:
            raise LegacyOfficeConversionError("version_probe_failed")
        command = [
            executable,
            f"-env:UserInstallation={profile_directory.resolve().as_uri()}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--norestore",
            "--invisible",
            "--convert-to",
            format_spec.convert_filter,
            "--outdir",
            str(output_directory),
            str(input_path),
        ]
        try:
            completed = self._run_process(
                command,
                cwd=input_path.parent,
                timeout_seconds=self.config.timeout_seconds,
                environment_overrides=process_environment,
            )
        except _CommandTimedOut as exc:
            logger.error(
                "Legacy Office conversion failed code=conversion_timeout diagnostic=process tree terminated"
            )
            raise LegacyOfficeConversionError(
                "conversion_timeout",
                diagnostic="LibreOffice conversion timed out and its process tree was terminated",
            ) from exc
        except OSError as exc:
            diagnostic = _sanitize_for_log(
                str(exc),
                sensitive_values=(
                    str(sensitive_source),
                    str(input_path.parent),
                    executable,
                ),
            )
            logger.error(
                "Legacy Office conversion failed code=process_start_failed diagnostic=%s",
                diagnostic,
            )
            raise LegacyOfficeConversionError(
                "process_start_failed",
                diagnostic=diagnostic,
            ) from exc

        diagnostic = _sanitize_for_log(
            _as_bytes(completed.stdout) + b"\n" + _as_bytes(completed.stderr),
            sensitive_values=(
                str(sensitive_source),
                str(input_path.parent),
                executable,
            ),
        )
        if completed.returncode != 0:
            logger.error(
                "Legacy Office conversion failed code=nonzero_exit returncode=%s diagnostic=%s",
                completed.returncode,
                diagnostic,
            )
            raise LegacyOfficeConversionError(
                "nonzero_exit",
                diagnostic=diagnostic,
            )
        if diagnostic:
            logger.info(
                "Legacy Office conversion completed diagnostic=%s",
                diagnostic,
            )
        return completed

    def _validate_output(
        self,
        output_directory: Path,
        format_spec: _LegacyOfficeFormat,
    ) -> Path:
        try:
            outputs = [
                path
                for path in output_directory.iterdir()
                if path.is_file() and not path.is_symlink()
            ]
        except OSError as exc:
            raise LegacyOfficeConversionError(
                "output_unreadable",
                diagnostic="conversion output directory is unreadable",
            ) from exc
        if not outputs:
            raise LegacyOfficeConversionError(
                "output_missing",
                diagnostic="LibreOffice did not create an output document",
            )
        if len(outputs) != 1:
            raise LegacyOfficeConversionError(
                "multiple_outputs",
                diagnostic="LibreOffice created an ambiguous output set",
            )
        output_path = outputs[0]
        expected_name = f"input{format_spec.target_suffix}"
        if (
            output_path.name.lower() != expected_name.lower()
            or output_path.suffix.lower() != format_spec.target_suffix
        ):
            raise LegacyOfficeConversionError(
                "unexpected_output_name",
                diagnostic="LibreOffice output name or extension was unexpected",
            )
        try:
            output_size = output_path.stat().st_size
        except OSError as exc:
            raise LegacyOfficeConversionError(
                "output_unreadable",
                diagnostic="conversion output is unreadable",
            ) from exc
        if output_size <= 0:
            raise LegacyOfficeConversionError(
                "output_empty",
                diagnostic="conversion output is empty",
            )
        if output_size > self.config.max_output_bytes:
            raise LegacyOfficeConversionError(
                "output_too_large",
                diagnostic="conversion output exceeds the configured byte limit",
            )

        self._validate_ooxml_in_isolated_process(
            output_path,
            required_member=format_spec.required_member,
        )
        return output_path

    def _validate_ooxml_in_isolated_process(
        self,
        output_path: Path,
        *,
        required_member: str,
    ) -> None:
        """Validate OOXML in a killable process with fixed resource bounds."""

        validator_script = _ooxml_validator_script_path()
        command = [
            sys.executable,
            "-I",
            "-S",
            str(validator_script),
            str(output_path),
            required_member,
            str(_OOXML_MAX_UNCOMPRESSED_BYTES),
        ]
        try:
            completed = self._ooxml_validation_runner(
                command,
                cwd=str(output_path.parent),
                env=self._subprocess_environment.copy(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
                timeout=_OOXML_VALIDATION_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LegacyOfficeConversionError(
                "ooxml_validation_timeout",
                diagnostic="OOXML integrity validation exceeded its fixed time limit",
            ) from exc
        except OSError as exc:
            raise LegacyOfficeConversionError(
                "ooxml_validation_unavailable",
                diagnostic="isolated OOXML integrity validation could not start",
            ) from exc

        returncode = int(getattr(completed, "returncode", -1))
        error_by_returncode = {
            _OOXML_VALIDATOR_EXIT_INVALID_ZIP: (
                "invalid_ooxml_zip",
                "conversion output is not a valid OOXML ZIP package",
            ),
            _OOXML_VALIDATOR_EXIT_CORRUPT_ZIP: (
                "corrupt_ooxml_zip",
                "conversion output contains corrupt ZIP data",
            ),
            _OOXML_VALIDATOR_EXIT_REQUIRED_MEMBER_MISSING: (
                "missing_ooxml_member",
                "conversion output lacks exactly one required OOXML member",
            ),
            _OOXML_VALIDATOR_EXIT_UNCOMPRESSED_LIMIT_EXCEEDED: (
                "ooxml_uncompressed_too_large",
                "conversion output exceeds the fixed uncompressed byte limit",
            ),
            _OOXML_VALIDATOR_EXIT_ENCRYPTED_MEMBER: (
                "encrypted_ooxml_member",
                "conversion output contains an encrypted ZIP member",
            ),
            _OOXML_VALIDATOR_EXIT_UNSAFE_MEMBER: (
                "unsafe_ooxml_member",
                "conversion output contains an unsafe ZIP member",
            ),
            _OOXML_VALIDATOR_EXIT_UNSUPPORTED_COMPRESSION: (
                "unsupported_ooxml_compression",
                "conversion output uses a disallowed ZIP compression method",
            ),
            _OOXML_VALIDATOR_EXIT_DUPLICATE_MEMBER: (
                "duplicate_ooxml_member",
                "conversion output contains duplicate ZIP member names",
            ),
            _OOXML_VALIDATOR_EXIT_ZIP_DIRECTORY_LIMIT_EXCEEDED: (
                "ooxml_zip_directory_limit_exceeded",
                "conversion output exceeds fixed ZIP directory metadata limits",
            ),
            _OOXML_VALIDATOR_EXIT_INVALID_ZIP_METADATA: (
                "invalid_ooxml_zip",
                "conversion output contains invalid single-disk ZIP metadata",
            ),
            _OOXML_VALIDATOR_EXIT_INVALID_ARGUMENT: (
                "ooxml_validator_contract_error",
                "isolated OOXML validator received an invalid internal contract",
            ),
            _OOXML_VALIDATOR_EXIT_UNEXPECTED_FAILURE: (
                "ooxml_validation_internal_error",
                "isolated OOXML validator failed internally",
            ),
        }
        if returncode == 0:
            return
        if returncode in error_by_returncode:
            code, diagnostic = error_by_returncode[returncode]
        else:
            code = "ooxml_validation_internal_error"
            diagnostic = "isolated OOXML validator exited unexpectedly"
        logger.error(
            "Legacy Office OOXML validation failed code=%s validator_returncode=%d",
            code,
            returncode,
        )
        raise LegacyOfficeConversionError(code, diagnostic=diagnostic)

    def _publish_unique_prepared_path(
        self,
        validated_output: Path,
        format_spec: _LegacyOfficeFormat,
    ) -> Path:
        """Atomically give the validated artifact an opaque, unique basename.

        LibreOffice still receives and emits the fixed short ``input`` name.
        Renaming only after complete OOXML validation prevents that fixed
        basename from becoming a persistent AnythingLLM ingestion identity.
        """

        opaque_name = f"prepared-{uuid.uuid4().hex}{format_spec.target_suffix}"
        prepared_path = validated_output.parent / opaque_name
        try:
            if prepared_path.exists():
                # UUID collisions are not expected, and silently overwriting a
                # file would violate the one-output boundary.
                raise LegacyOfficeConversionError(
                    "prepared_name_collision",
                    diagnostic="opaque prepared output name collided",
                )
            os.replace(validated_output, prepared_path)
            remaining_outputs = [
                path
                for path in prepared_path.parent.iterdir()
                if path.is_file() and not path.is_symlink()
            ]
        except LegacyOfficeConversionError:
            raise
        except OSError as exc:
            raise LegacyOfficeConversionError(
                "output_publish_failed",
                diagnostic="validated output could not be atomically published",
            ) from exc
        if remaining_outputs != [prepared_path]:
            raise LegacyOfficeConversionError(
                "multiple_outputs",
                diagnostic="opaque output publication changed the output set",
            )
        return prepared_path

    def _run_process(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None,
        timeout_seconds: float,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> _CommandResult:
        process_environment = self._subprocess_environment.copy()
        if environment_overrides:
            process_environment.update(
                {
                    str(key): str(value)
                    for key, value in environment_overrides.items()
                }
            )
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
            "close_fds": True,
            "bufsize": 0,
            "env": process_environment,
        }
        if cwd is not None:
            kwargs["cwd"] = str(cwd)
        if self._platform_name.startswith("win"):
            kwargs["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
            )
        else:
            kwargs["start_new_session"] = True

        process = self._process_factory(list(command), **kwargs)
        stdout_collector = _BoundedPipeCollector(
            getattr(process, "stdout", None)
        )
        stderr_collector = _BoundedPipeCollector(
            getattr(process, "stderr", None)
        )
        stdout_collector.start()
        stderr_collector.start()
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        try:
            returncode = int(
                process.wait(
                    timeout=max(0.0, deadline - time.monotonic())
                )
            )
            stdout_complete = stdout_collector.wait_for_eof(
                deadline - time.monotonic()
            )
            stderr_complete = stderr_collector.wait_for_eof(
                deadline - time.monotonic()
            )
            if not stdout_complete or not stderr_complete:
                timed_out = True
                self._terminate_process_tree(process)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_process_tree(process)
            returncode = int(getattr(process, "returncode", -1) or -1)
        except OSError:
            self._terminate_process_tree(process)
            raise
        finally:
            if timed_out:
                stdout_collector.wait_for_eof(_PROCESS_TREE_GRACE_SECONDS)
                stderr_collector.wait_for_eof(_PROCESS_TREE_GRACE_SECONDS)
            stdout = stdout_collector.finish()
            stderr = stderr_collector.finish()
        if timed_out:
            raise _CommandTimedOut
        return _CommandResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def _terminate_process_tree(self, process: Any) -> None:
        if self._platform_name.startswith("win"):
            try:
                self._taskkill_runner(
                    [
                        "taskkill",
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                    ],
                    shell=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=_PROCESS_TREE_GRACE_SECONDS)
            except (OSError, subprocess.SubprocessError):
                pass
            return

        # ``start_new_session=True`` makes the child's pid its process-group id.
        # Keep that known id even if the direct child exits before timeout
        # handling; descendants may still be alive in the original group.
        try:
            process_group = int(process.pid)
        except (TypeError, ValueError):
            process_group = None
        if process_group is not None and process_group <= 0:
            process_group = None
        if process_group is not None:
            try:
                os.killpg(process_group, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        else:
            try:
                process.terminate()
            except (OSError, ProcessLookupError):
                pass
        try:
            process.wait(timeout=_PROCESS_TREE_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass

        if process_group is not None:
            # Always attempt SIGKILL for the known group, even when the parent
            # has already exited and ``communicate`` returned.  This closes the
            # parent-exited/descendant-survived gap.
            try:
                # 数值 9 是 POSIX SIGKILL。Windows 不会进入此分支，但使用回退值可让
                # Windows CI 对 macOS 进程组算法进行无进程副作用的静态模拟。
                os.killpg(process_group, getattr(signal, "SIGKILL", 9))
            except (OSError, ProcessLookupError):
                pass
        else:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
        try:
            process.wait(timeout=_PROCESS_TREE_GRACE_SECONDS)
        except (OSError, subprocess.SubprocessError):
            pass

    def _is_owned_job(self, job_path: Path) -> bool:
        marker = job_path / _OWNED_JOB_MARKER_NAME
        try:
            return (
                not marker.is_symlink()
                and marker.is_file()
                and marker.read_bytes() in _OWNED_JOB_MARKER_COMPATIBLE_BYTES
            )
        except OSError:
            return False

    def _cleanup_owned_job(self, job_path: Path) -> bool:
        root = self.config.jobs_root
        try:
            if (
                job_path.is_symlink()
                or job_path.parent.resolve() != root.resolve()
                or not self._is_owned_job(job_path)
            ):
                logger.warning(
                    "Legacy Office cleanup skipped: target is outside the owned job boundary"
                )
                return False
            shutil.rmtree(job_path)
            return True
        except OSError as exc:
            logger.warning(
                "Legacy Office cleanup failed; startup sweep will retry diagnostic=%s",
                _sanitize_for_log(
                    str(exc),
                    sensitive_values=(str(job_path), str(root)),
                ),
            )
            return False


__all__ = [
    "LibreOfficeLegacyOfficePreparer",
    "discover_libreoffice_executable",
    "is_legacy_office_path",
]
