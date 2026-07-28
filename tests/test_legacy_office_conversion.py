from __future__ import annotations

import concurrent.futures
import io
import os
import re
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
import zipfile
import math
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from app.modules.document_processing import (
    LegacyOfficeConfig,
    LegacyOfficeConversionError,
    LegacyOfficePreparationResult,
    LegacyOfficePreparer,
    LibreOfficeLegacyOfficePreparer,
    discover_libreoffice_executable,
    is_legacy_office_path,
)
from app.modules.document_processing.libreoffice import (
    _OOXML_MAX_UNCOMPRESSED_BYTES,
    _OOXML_VALIDATION_TIMEOUT_SECONDS,
    _OWNED_JOB_MARKER_NAME,
    _OWNED_JOB_MARKER_VALUE,
    _PROCESS_OUTPUT_CAPTURE_BYTES,
    _PROCESS_OUTPUT_READ_BYTES,
    _sanitize_for_log,
)
from app.modules.document_processing.ooxml_validator import (
    EXIT_DUPLICATE_MEMBER,
    EXIT_INVALID_ZIP_METADATA,
    EXIT_UNSAFE_MEMBER,
    EXIT_UNCOMPRESSED_LIMIT_EXCEEDED,
    EXIT_UNSUPPORTED_COMPRESSION,
    EXIT_ZIP_DIRECTORY_LIMIT_EXCEEDED,
    ZIP_MAX_CENTRAL_DIRECTORY_BYTES,
    ZIP_MAX_MEMBER_COUNT,
    _InvalidZipMetadataError,
    _read_zip_directory_metadata,
    validate_ooxml_archive,
)


OLE2_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")

FORMAT_CASES = {
    ".doc": (
        ".docx",
        "docx:Office Open XML Text",
        "word/document.xml",
    ),
    ".ppt": (
        ".pptx",
        "pptx:Impress MS PowerPoint 2007 XML",
        "ppt/presentation.xml",
    ),
    ".xls": (
        ".xlsx",
        "xlsx:Calc Office Open XML",
        "xl/workbook.xml",
    ),
}


def write_ole2(path: Path, *, size: int = 64) -> None:
    path.write_bytes(OLE2_MAGIC + b"\x00" * max(0, size - len(OLE2_MAGIC)))


def write_synthetic_zip_metadata(
    path: Path,
    *,
    member_count: int,
    central_directory_size: int,
    zip64: bool,
    populate_central_directory: bool = False,
    central_directory_member_count: int | None = None,
    disk_number: int = 0,
    zip64_record_payload_size: int = 44,
    locator_disk_count: int = 1,
) -> None:
    """Write sparse tail metadata for preflight-only boundary tests."""

    with path.open("wb") as file_object:
        if populate_central_directory:
            written_member_count = (
                member_count
                if central_directory_member_count is None
                else central_directory_member_count
            )
            minimum_size = written_member_count * 46
            variable_size = central_directory_size - minimum_size
            if written_member_count < 1 or variable_size < 0:
                raise ValueError("synthetic central directory size is invalid")
            common_extra_size, extra_remainder = divmod(
                variable_size,
                written_member_count,
            )
            if common_extra_size + bool(extra_remainder) > 0xFFFF:
                raise ValueError("synthetic central directory extra fields overflow")
            for index in range(written_member_count):
                extra_size = common_extra_size + (
                    1 if index < extra_remainder else 0
                )
                header = bytearray(46)
                header[:4] = b"PK\x01\x02"
                struct.pack_into("<H", header, 30, extra_size)
                file_object.write(header)
                file_object.seek(extra_size, os.SEEK_CUR)
        elif central_directory_size:
            file_object.write(b"PK\x01\x02")
            file_object.seek(central_directory_size)
        if zip64:
            zip64_eocd_offset = file_object.tell()
            file_object.write(
                struct.pack(
                    "<4sQ2H2I4Q",
                    b"PK\x06\x06",
                    zip64_record_payload_size,
                    45,
                    45,
                    disk_number,
                    0,
                    member_count,
                    member_count,
                    central_directory_size,
                    0,
                )
            )
            if zip64_record_payload_size > 44:
                file_object.write(
                    b"\x00" * (zip64_record_payload_size - 44)
                )
            file_object.write(
                struct.pack(
                    "<4sIQI",
                    b"PK\x06\x07",
                    0,
                    zip64_eocd_offset,
                    locator_disk_count,
                )
            )
            file_object.write(
                struct.pack(
                    "<4s4H2IH",
                    b"PK\x05\x06",
                    0,
                    0,
                    min(member_count, 0xFFFF),
                    min(member_count, 0xFFFF),
                    min(central_directory_size, 0xFFFFFFFF),
                    0,
                    0,
                )
            )
            return

        file_object.write(
            struct.pack(
                "<4s4H2IH",
                b"PK\x05\x06",
                disk_number,
                0,
                member_count,
                member_count,
                central_directory_size,
                0,
                0,
            )
        )


class FakeProcess:
    def __init__(
        self,
        command,
        *,
        factory: "FakeProcessFactory",
        kwargs: dict,
    ) -> None:
        self.command = list(command)
        self.factory = factory
        self.kwargs = kwargs
        self.pid = factory.next_pid
        factory.next_pid += 1
        is_version = "--version" in self.command
        self.returncode = (
            None
            if factory.timeout_conversion and not is_version
            else (
                factory.version_returncode
                if is_version
                else factory.conversion_returncode
            )
        )
        self.killed = False
        self.terminated = False
        self._conversion_completed = False
        self.stdout = TrackingBytesIO(
            factory.version_output if is_version else factory.conversion_stdout
        )
        self.stderr = TrackingBytesIO(
            factory.version_stderr if is_version else factory.conversion_stderr
        )

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        if "--version" in self.command:
            return self.returncode
        if self.killed or self.terminated:
            return self.returncode
        if self._conversion_completed:
            return self.returncode
        self._conversion_completed = True
        self.factory.conversion_commands.append((self.command, self.kwargs))
        with self.factory.activity_lock:
            self.factory.active_conversions += 1
            self.factory.max_active_conversions = max(
                self.factory.max_active_conversions,
                self.factory.active_conversions,
            )
        try:
            if self.factory.conversion_delay:
                time.sleep(self.factory.conversion_delay)
            self.returncode = self.factory.conversion_returncode
            if self.returncode == 0:
                self.factory.create_outputs(self.command)
            return self.returncode
        finally:
            with self.factory.activity_lock:
                self.factory.active_conversions -= 1

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class TrackingBytesIO(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.total_bytes_read = 0
        self.read_calls = 0
        self.max_requested_bytes = 0

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        self.max_requested_bytes = max(self.max_requested_bytes, size)
        value = super().read(size)
        self.total_bytes_read += len(value)
        return value


class FakeProcessFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.conversion_commands: list[tuple[list[str], dict]] = []
        self.next_pid = 41000
        self.version_output = b"LibreOffice 26.2.5.2 60(Build:2)\n"
        self.version_stderr = b""
        self.version_returncode = 0
        self.conversion_returncode = 0
        self.conversion_stdout = b"convert /private/task/input.doc -> output\n"
        self.conversion_stderr = b""
        self.timeout_conversion = False
        self.output_mode = "valid"
        self.output_padding = 0
        self.conversion_delay = 0.0
        self.activity_lock = threading.Lock()
        self.active_conversions = 0
        self.max_active_conversions = 0
        self.last_process: FakeProcess | None = None

    def __call__(self, command, **kwargs):
        command_list = list(command)
        self.calls.append((command_list, kwargs))
        process = FakeProcess(command_list, factory=self, kwargs=kwargs)
        self.last_process = process
        return process

    def create_outputs(self, command: list[str]) -> None:
        if self.output_mode == "missing":
            return
        outdir = Path(command[command.index("--outdir") + 1])
        source = Path(command[-1])
        filter_value = command[command.index("--convert-to") + 1]
        target_extension = "." + filter_value.split(":", 1)[0]
        target = outdir / f"{source.stem}{target_extension}"
        if self.output_mode == "wrong_name":
            target = outdir / f"unexpected{target_extension}"
        if self.output_mode == "empty":
            target.touch()
            return
        if self.output_mode == "not_zip":
            target.write_bytes(b"not a ZIP package")
            return
        required_member = {
            ".docx": "word/document.xml",
            ".pptx": "ppt/presentation.xml",
            ".xlsx": "xl/workbook.xml",
        }[target_extension]
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as package:
            if self.output_mode != "missing_member":
                package.writestr(required_member, b"<document/>")
            package.writestr("[Content_Types].xml", b"<Types/>")
            if self.output_padding:
                package.writestr(
                    "padding.bin",
                    os.urandom(self.output_padding),
                )
        if self.output_mode == "corrupt_member":
            with zipfile.ZipFile(target, "r") as package:
                info = package.getinfo(required_member)
                member_offset = (
                    info.header_offset
                    + 30
                    + len(info.filename.encode("utf-8"))
                    + len(info.extra)
                )
            with target.open("r+b") as output:
                output.seek(member_offset)
                original = output.read(1)
                output.seek(member_offset)
                output.write(bytes([original[0] ^ 0xFF]))
        if self.output_mode == "multiple":
            (outdir / f"other{target_extension}").write_bytes(target.read_bytes())


class LegacyOfficeConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        self.executable = self.root / "soffice"
        self.executable.write_text("#!/bin/sh\n", encoding="utf-8")
        self.executable.chmod(0o700)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def config(self, **overrides) -> LegacyOfficeConfig:
        values = {
            "enabled": True,
            "executable": str(self.executable),
            "allowed_version_series": "26.2",
            "timeout_seconds": 1,
            "max_concurrency": 1,
            "max_input_bytes": 1024 * 1024,
            "max_output_bytes": 1024 * 1024,
            "jobs_root": self.root / "runtime" / "office_conversion" / "jobs",
        }
        values.update(overrides)
        return LegacyOfficeConfig(**values)

    def preparer(
        self,
        factory: FakeProcessFactory | None = None,
        **kwargs,
    ) -> tuple[LibreOfficeLegacyOfficePreparer, FakeProcessFactory]:
        selected_factory = factory or FakeProcessFactory()
        return (
            LibreOfficeLegacyOfficePreparer(
                self.config(**kwargs),
                process_factory=selected_factory,
            ),
            selected_factory,
        )

    def source(self, suffix: str, *, size: int = 64) -> Path:
        path = self.root / f"source{suffix}"
        write_ole2(path, size=size)
        return path

    def assert_error_code(self, code: str, callback) -> LegacyOfficeConversionError:
        with self.assertRaises(LegacyOfficeConversionError) as captured:
            callback()
        self.assertEqual(captured.exception.code, code)
        self.assertEqual(str(captured.exception), "Legacy Office 文件本地转换失败")
        return captured.exception

    def test_config_and_result_are_frozen(self) -> None:
        config = self.config()
        with self.assertRaises(FrozenInstanceError):
            config.enabled = False  # type: ignore[misc]
        result = LegacyOfficePreparationResult(
            original_path=Path("a.pdf"),
            prepared_path=Path("a.pdf"),
            source_suffix=".PDF",
            target_suffix=".PDF",
            libreoffice_version=None,
            converted=False,
        )
        with self.assertRaises(FrozenInstanceError):
            result.converted = True  # type: ignore[misc]
        self.assertEqual(result.source_suffix, ".pdf")

    def test_config_validates_limits_and_offers_disabled_factory(self) -> None:
        disabled = LegacyOfficeConfig.disabled(jobs_root=self.root / "jobs")
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.jobs_root, self.root / "jobs")
        for field_name, value in (
            ("timeout_seconds", 0),
            ("timeout_seconds", True),
            ("timeout_seconds", "120"),
            ("timeout_seconds", math.nan),
            ("timeout_seconds", math.inf),
            ("max_concurrency", 0),
            ("max_input_bytes", 0),
            ("max_output_bytes", 0),
        ):
            with self.subTest(field=field_name):
                with self.assertRaises(ValueError):
                    self.config(**{field_name: value})
        with self.assertRaises(ValueError):
            self.config(allowed_version_series="release")

    def test_protocol_and_case_insensitive_suffixes(self) -> None:
        preparer, _ = self.preparer()
        self.assertIsInstance(preparer, LegacyOfficePreparer)
        for name in ("A.DOC", "b.PpT", "c.XLS"):
            self.assertTrue(is_legacy_office_path(name))
        for name in ("a.docx", "b.pdf", "c"):
            self.assertFalse(is_legacy_office_path(name))

    def test_disabled_never_probes_and_nonlegacy_passes_through(self) -> None:
        process_factory = FakeProcessFactory()
        which_calls: list[str] = []
        preparer = LibreOfficeLegacyOfficePreparer(
            LegacyOfficeConfig.disabled(jobs_root=self.root / "jobs"),
            process_factory=process_factory,
            which=lambda command: which_calls.append(command) or None,
        )
        self.assertIsNone(preparer.preflight())
        pdf = self.root / "sample.pdf"
        with preparer.prepare(pdf, job_id="nonlegacy") as result:
            self.assertFalse(result.converted)
            self.assertEqual(result.prepared_path, pdf)
        self.assert_error_code(
            "feature_disabled",
            lambda: preparer.prepare(self.root / "legacy.DOC", job_id="legacy"),
        )
        self.assertEqual(which_calls, [])
        self.assertEqual(process_factory.calls, [])
        self.assertFalse((self.root / "jobs").exists())

    def test_executable_discovery_precedence_and_absolute_requirement(self) -> None:
        explicit = "/opt/docsense/soffice"
        self.assertEqual(
            discover_libreoffice_executable(
                explicit,
                platform_name="darwin",
                path_is_file=lambda value: value == explicit,
                path_is_executable=lambda value: True,
                which=lambda _: "/path/ignored",
            ),
            explicit,
        )
        self.assert_error_code(
            "executable_path_not_absolute",
            lambda: discover_libreoffice_executable(
                "relative/soffice",
                platform_name="darwin",
            ),
        )

        standard = r"C:\Program Files\LibreOffice\program\soffice.com"
        discovered = discover_libreoffice_executable(
            None,
            platform_name="win32",
            environment={"ProgramFiles": r"C:\Program Files"},
            path_is_file=lambda value: value == standard,
            path_is_executable=lambda value: True,
            which=lambda _: None,
        )
        self.assertEqual(discovered, standard)

        user_standard = (
            "/Users/docsense/Applications/LibreOffice.app/Contents/MacOS/soffice"
        )
        discovered = discover_libreoffice_executable(
            None,
            platform_name="darwin",
            environment={"HOME": "/Users/docsense"},
            path_is_file=lambda value: value == user_standard,
            path_is_executable=lambda value: True,
            which=lambda _: None,
        )
        self.assertEqual(discovered, user_standard)

        path_candidate = "/usr/local/bin/soffice"
        discovered = discover_libreoffice_executable(
            None,
            platform_name="linux",
            path_is_file=lambda value: value == path_candidate,
            path_is_executable=lambda value: True,
            which=lambda command: path_candidate if command == "soffice" else None,
        )
        self.assertEqual(discovered, path_candidate)

    def test_preflight_accepts_stable_allowed_version_and_caches(self) -> None:
        factory = FakeProcessFactory()
        executable = "/opt/docsense/soffice"
        preparer = LibreOfficeLegacyOfficePreparer(
            self.config(executable=executable),
            platform_name="darwin",
            environment={
                "HOME": "/Users/docsense",
                "PATH": "/usr/bin:/bin",
                "TMPDIR": "/private/tmp",
                "LANG": "en_US.UTF-8",
                "ANYTHINGLLM_API_KEY": "must-not-reach-child",
                "CALLBACK_URL": "http://private.invalid/callback",
            },
            process_factory=factory,
            path_is_file=lambda value: value == executable,
            path_is_executable=lambda value: value == executable,
        )
        self.assertEqual(preparer.preflight(), "26.2.5.2")
        self.assertEqual(preparer.preflight(), "26.2.5.2")
        version_calls = [
            command for command, _ in factory.calls if "--version" in command
        ]
        self.assertEqual(len(version_calls), 1)
        self.assertFalse(factory.calls[0][1]["shell"])
        self.assertTrue(factory.calls[0][1]["close_fds"])
        self.assertEqual(factory.calls[0][1]["stdin"], subprocess.DEVNULL)
        self.assertTrue(factory.calls[0][1]["start_new_session"])
        child_environment = factory.calls[0][1]["env"]
        self.assertEqual(child_environment["HOME"], "/Users/docsense")
        self.assertEqual(child_environment["TMPDIR"], "/private/tmp")
        self.assertEqual(child_environment["SAL_USE_VCLPLUGIN"], "svp")
        self.assertNotIn("ANYTHINGLLM_API_KEY", child_environment)
        self.assertNotIn("CALLBACK_URL", child_environment)
        self.assertFalse(preparer.config.jobs_root.exists())

    def test_output_pipes_are_fully_drained_with_bounded_in_memory_tails(
        self,
    ) -> None:
        factory = FakeProcessFactory()
        stdout_payload = (
            b"x" * (_PROCESS_OUTPUT_CAPTURE_BYTES * 4)
            + b"\nLibreOffice 26.2.5.2\n"
        )
        stderr_payload = (
            b"y" * (_PROCESS_OUTPUT_CAPTURE_BYTES * 3)
            + b"\nstderr-tail\n"
        )
        factory.version_output = stdout_payload
        factory.version_stderr = stderr_payload
        preparer, _ = self.preparer(factory)

        result = preparer._run_process(
            [str(self.executable), "--version"],
            cwd=None,
            timeout_seconds=1,
        )

        process = factory.last_process
        self.assertEqual(process.stdout.total_bytes_read, len(stdout_payload))
        self.assertEqual(process.stderr.total_bytes_read, len(stderr_payload))
        self.assertGreater(process.stdout.read_calls, 2)
        self.assertGreater(process.stderr.read_calls, 2)
        self.assertLessEqual(
            process.stdout.max_requested_bytes,
            _PROCESS_OUTPUT_READ_BYTES,
        )
        self.assertLessEqual(
            process.stderr.max_requested_bytes,
            _PROCESS_OUTPUT_READ_BYTES,
        )
        self.assertLessEqual(len(result.stdout), _PROCESS_OUTPUT_CAPTURE_BYTES)
        self.assertLessEqual(len(result.stderr), _PROCESS_OUTPUT_CAPTURE_BYTES)
        self.assertIn(b"output truncated", result.stdout)
        self.assertIn(b"LibreOffice 26.2.5.2", result.stdout)
        self.assertIn(b"stderr-tail", result.stderr)
        self.assertFalse(preparer.config.jobs_root.exists())

    def test_real_subprocess_large_output_cannot_deadlock_pipe_drains(self) -> None:
        preparer = LibreOfficeLegacyOfficePreparer(
            LegacyOfficeConfig.disabled(jobs_root=self.root / "unused-jobs")
        )
        payload_bytes = _PROCESS_OUTPUT_CAPTURE_BYTES * 8
        script = (
            "import sys\n"
            f"sys.stdout.buffer.write(b'x' * {payload_bytes} + b'STDOUT_TAIL')\n"
            "sys.stdout.buffer.flush()\n"
            f"sys.stderr.buffer.write(b'y' * {payload_bytes} + b'STDERR_TAIL')\n"
            "sys.stderr.buffer.flush()\n"
        )
        result = preparer._run_process(
            [sys.executable, "-c", script],
            cwd=None,
            timeout_seconds=5,
        )
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout), _PROCESS_OUTPUT_CAPTURE_BYTES)
        self.assertLessEqual(len(result.stderr), _PROCESS_OUTPUT_CAPTURE_BYTES)
        self.assertIn(b"output truncated", result.stdout)
        self.assertIn(b"output truncated", result.stderr)
        self.assertTrue(result.stdout.endswith(b"STDOUT_TAIL"))
        self.assertTrue(result.stderr.endswith(b"STDERR_TAIL"))
        self.assertFalse((self.root / "unused-jobs").exists())

    def test_preflight_rejects_dev_wrong_series_and_unrecognized_versions(self) -> None:
        cases = (
            (b"LibreOfficeDev 26.2.5.0 alpha", "development_version_rejected"),
            (b"LibreOffice 26.2.5.0 beta1", "prerelease_version_rejected"),
            (b"LibreOffice 26.2.5.0 rc2", "prerelease_version_rejected"),
            (b"LibreOffice 25.8.7.2", "version_not_allowed"),
            (b"LibreOffice version unknown", "unrecognized_version"),
        )
        for output, code in cases:
            with self.subTest(code=code):
                factory = FakeProcessFactory()
                factory.version_output = output
                preparer, _ = self.preparer(factory)
                self.assert_error_code(code, preparer.preflight)

    def test_preflight_nonzero_exit_and_missing_executable_fail(self) -> None:
        factory = FakeProcessFactory()
        factory.version_returncode = 4
        preparer, _ = self.preparer(factory)
        self.assert_error_code("version_probe_failed", preparer.preflight)
        missing = LibreOfficeLegacyOfficePreparer(
            self.config(executable=str(self.root / "missing")),
            process_factory=FakeProcessFactory(),
        )
        self.assert_error_code("executable_not_found", missing.preflight)

    def test_all_formats_use_exact_filter_hardened_profile_and_valid_ooxml(self) -> None:
        for source_suffix, (
            target_suffix,
            expected_filter,
            required_member,
        ) in FORMAT_CASES.items():
            with self.subTest(source_suffix=source_suffix):
                factory = FakeProcessFactory()
                preparer, _ = self.preparer(factory)
                source = self.source(source_suffix.upper())
                with preparer.prepare(source, job_id="business-file-name") as result:
                    self.assertTrue(result.converted)
                    self.assertEqual(result.original_path, source)
                    self.assertEqual(result.source_suffix, source_suffix)
                    self.assertEqual(result.target_suffix, target_suffix)
                    self.assertEqual(result.libreoffice_version, "26.2.5.2")
                    self.assertRegex(
                        result.prepared_path.name,
                        rf"^prepared-[0-9a-f]{{32}}{re.escape(target_suffix)}$",
                    )
                    self.assertNotEqual(result.prepared_path.parent, source.parent)
                    with zipfile.ZipFile(result.prepared_path) as package:
                        self.assertIn(required_member, package.namelist())

                    command, kwargs = factory.conversion_commands[-1]
                    self.assertEqual(
                        command[command.index("--convert-to") + 1],
                        expected_filter,
                    )
                    self.assertFalse(kwargs["shell"])
                    if sys.platform.startswith("win"):
                        self.assertIn("creationflags", kwargs)
                        self.assertNotIn("start_new_session", kwargs)
                    else:
                        self.assertTrue(kwargs["start_new_session"])
                    self.assertIn("-env:UserInstallation=file://", command[1])
                    # 生产命令仍校验 file URI；测试直接从任务所有权边界读取同一 Profile，
                    # 避免用 Windows Path 错误解析 macOS URI 或反向解析。
                    profile = result.prepared_path.parent.parent / "profile"
                    registry = (
                        profile / "user" / "registrymodifications.xcu"
                    ).read_text(encoding="utf-8")
                    self.assertIn("MacroSecurityLevel", registry)
                    self.assertIn("DisableMacrosExecution", registry)
                    self.assertIn("DisableActiveContent", registry)
                    self.assertIn("SecureURL", registry)
                    self.assertIn("Office.Writer/Content/Update", registry)
                    self.assertIn("Office.Calc/Content/Update", registry)
                    self.assertRegex(
                        registry,
                        r"Office\.Writer/Content/Update[\s\S]*?"
                        r'oor:name="Link"[\s\S]*?<value>2</value>',
                    )
                    self.assertRegex(
                        registry,
                        r"Office\.Calc/Content/Update[\s\S]*?"
                        r'oor:name="Link"[\s\S]*?<value>1</value>',
                    )
                    job_path = result.prepared_path.parent.parent
                    self.assertEqual(
                        (job_path / f"input{source_suffix}").name,
                        f"input{source_suffix}",
                    )
                    self.assertTrue(job_path.exists())
                self.assertFalse(job_path.exists())

    def test_conversion_redirects_posix_home_and_temp_into_private_job(
        self,
    ) -> None:
        factory = FakeProcessFactory()
        executable = "/opt/docsense/soffice"
        preparer = LibreOfficeLegacyOfficePreparer(
            self.config(executable=executable),
            platform_name="darwin",
            environment={
                "HOME": "/Users/host-account",
                "PATH": "/usr/bin:/bin",
                "TMPDIR": "/private/host-tmp",
                "TMP": "/private/host-tmp",
                "TEMP": "/private/host-tmp",
                "ANYTHINGLLM_API_KEY": "must-not-reach-child",
            },
            process_factory=factory,
            path_is_file=lambda value: value == executable,
            path_is_executable=lambda value: value == executable,
        )
        with preparer.prepare(self.source(".doc"), job_id="private-env") as result:
            command, kwargs = factory.conversion_commands[-1]
            job_path = result.prepared_path.parent.parent
            child_environment = kwargs["env"]
            self.assertEqual(
                Path(child_environment["HOME"]),
                job_path / "home",
            )
            for variable in ("TMPDIR", "TMP", "TEMP"):
                self.assertEqual(
                    Path(child_environment[variable]),
                    job_path / "tmp",
                )
            self.assertTrue((job_path / "home").is_dir())
            self.assertTrue((job_path / "tmp").is_dir())
            # Windows 文件系统不提供可比的 POSIX mode bits；目录隔离仍在所有平台验证，
            # 精确 0700 仅在具备该语义的宿主机断言。
            if os.name != "nt":
                self.assertEqual(
                    (job_path / "home").stat().st_mode & 0o777,
                    0o700,
                )
                self.assertEqual(
                    (job_path / "tmp").stat().st_mode & 0o777,
                    0o700,
                )
            self.assertNotIn("ANYTHINGLLM_API_KEY", child_environment)
            self.assertEqual(Path(command[-1]).parent, job_path)
        self.assertFalse(job_path.exists())

    def test_consecutive_same_format_prepared_basenames_are_unique_and_opaque(
        self,
    ) -> None:
        preparer, _ = self.preparer()
        source = self.root / "甲方业务文件名.doc"
        write_ole2(source)
        prepared_names: list[str] = []
        for job_id in ("business-execution-one", "business-execution-two"):
            with preparer.prepare(source, job_id=job_id) as result:
                prepared_names.append(result.prepared_path.name)
                self.assertRegex(
                    result.prepared_path.name,
                    r"^prepared-[0-9a-f]{32}\.docx$",
                )
                self.assertNotIn(source.stem, result.prepared_path.name)
                self.assertNotIn(job_id, result.prepared_path.name)
        self.assertEqual(len(set(prepared_names)), 2)

    def test_zero_byte_forged_signature_and_input_limit_fail_closed(self) -> None:
        preparer, factory = self.preparer(max_input_bytes=32)
        empty = self.root / "empty.doc"
        empty.touch()
        forged = self.root / "forged.ppt"
        forged.write_bytes(b"not an OLE file")
        oversized = self.root / "oversized.xls"
        write_ole2(oversized, size=64)
        for source, code in (
            (empty, "input_empty"),
            (forged, "invalid_ole2_signature"),
            (oversized, "input_too_large"),
        ):
            with self.subTest(code=code):
                self.assert_error_code(
                    code,
                    lambda source=source: preparer.prepare(source, job_id="bad"),
                )
        self.assertEqual(factory.conversion_commands, [])

    def test_nonzero_exit_missing_and_multiple_outputs_fail_and_cleanup(self) -> None:
        cases = (
            ("nonzero", "nonzero_exit"),
            ("missing", "output_missing"),
            ("multiple", "multiple_outputs"),
        )
        for mode, code in cases:
            with self.subTest(mode=mode):
                factory = FakeProcessFactory()
                if mode == "nonzero":
                    factory.conversion_returncode = 9
                    factory.conversion_stderr = b"private /tmp/job/input.doc"
                else:
                    factory.output_mode = mode
                preparer, _ = self.preparer(factory)
                self.assert_error_code(
                    code,
                    lambda: preparer.prepare(
                        self.source(".doc"),
                        job_id="bad-output",
                    ),
                )
                jobs_root = preparer.config.jobs_root
                self.assertEqual(
                    list(jobs_root.iterdir()) if jobs_root.exists() else [],
                    [],
                )

    def test_output_shape_zip_member_and_size_are_validated(self) -> None:
        cases = (
            ("wrong_name", "unexpected_output_name", {}),
            ("empty", "output_empty", {}),
            ("not_zip", "invalid_ooxml_zip", {}),
            ("corrupt_member", "corrupt_ooxml_zip", {}),
            ("missing_member", "missing_ooxml_member", {}),
            ("valid", "output_too_large", {"max_output_bytes": 30}),
        )
        for mode, code, config_overrides in cases:
            with self.subTest(mode=mode):
                factory = FakeProcessFactory()
                factory.output_mode = mode
                preparer, _ = self.preparer(factory, **config_overrides)
                self.assert_error_code(
                    code,
                    lambda: preparer.prepare(
                        self.source(".ppt"),
                        job_id="bad-ooxml",
                    ),
                )

    def test_ooxml_uncompressed_limit_is_checked_before_member_reads(self) -> None:
        archive_path = self.root / "limit.docx"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", b"x" * 64)
        self.assertEqual(
            validate_ooxml_archive(
                archive_path,
                required_member="word/document.xml",
                max_uncompressed_bytes=32,
            ),
            EXIT_UNCOMPRESSED_LIMIT_EXCEEDED,
        )
        self.assertEqual(
            _OOXML_MAX_UNCOMPRESSED_BYTES,
            4 * 1024 * 1024 * 1024,
        )

    def test_zip_directory_metadata_limits_accept_exact_boundaries(self) -> None:
        cases = (
            (
                "classic-65535-members.zip",
                65_535,
                65_535 * 46,
                False,
            ),
            (
                "member-boundary.zip",
                ZIP_MAX_MEMBER_COUNT,
                ZIP_MAX_MEMBER_COUNT * 46,
                True,
            ),
            (
                "directory-boundary.zip",
                1024,
                ZIP_MAX_CENTRAL_DIRECTORY_BYTES,
                False,
            ),
        )
        for filename, member_count, directory_size, use_zip64 in cases:
            with self.subTest(filename=filename):
                archive_path = self.root / filename
                write_synthetic_zip_metadata(
                    archive_path,
                    member_count=member_count,
                    central_directory_size=directory_size,
                    zip64=use_zip64,
                    populate_central_directory=True,
                )
                with archive_path.open("rb") as file_object:
                    metadata = _read_zip_directory_metadata(
                        file_object,
                        file_size=archive_path.stat().st_size,
                    )
                self.assertEqual(metadata.member_count, member_count)
                self.assertEqual(
                    metadata.central_directory_size,
                    directory_size,
                )
                self.assertEqual(metadata.uses_zip64, use_zip64)

    def test_zip_directory_metadata_limits_fail_before_zipfile(self) -> None:
        cases = (
            (
                "too-many-members.zip",
                ZIP_MAX_MEMBER_COUNT + 1,
                (ZIP_MAX_MEMBER_COUNT + 1) * 46,
                True,
            ),
            (
                "directory-too-large.zip",
                1,
                ZIP_MAX_CENTRAL_DIRECTORY_BYTES + 1,
                False,
            ),
        )
        for filename, member_count, directory_size, use_zip64 in cases:
            with self.subTest(filename=filename):
                archive_path = self.root / filename
                write_synthetic_zip_metadata(
                    archive_path,
                    member_count=member_count,
                    central_directory_size=directory_size,
                    zip64=use_zip64,
                )
                with patch(
                    "app.modules.document_processing.ooxml_validator."
                    "zipfile.ZipFile"
                ) as zip_file:
                    exit_code = validate_ooxml_archive(
                        archive_path,
                        required_member="word/document.xml",
                        max_uncompressed_bytes=1024,
                    )
                self.assertEqual(
                    exit_code,
                    EXIT_ZIP_DIRECTORY_LIMIT_EXCEEDED,
                )
                zip_file.assert_not_called()

        forged_count = self.root / "forged-low-member-count.zip"
        write_synthetic_zip_metadata(
            forged_count,
            member_count=1,
            central_directory_member_count=ZIP_MAX_MEMBER_COUNT + 1,
            central_directory_size=(ZIP_MAX_MEMBER_COUNT + 1) * 46,
            zip64=True,
            populate_central_directory=True,
        )
        with patch(
            "app.modules.document_processing.ooxml_validator.zipfile.ZipFile"
        ) as zip_file:
            self.assertEqual(
                validate_ooxml_archive(
                    forged_count,
                    required_member="word/document.xml",
                    max_uncompressed_bytes=1024,
                ),
                EXIT_ZIP_DIRECTORY_LIMIT_EXCEEDED,
            )
        zip_file.assert_not_called()

    def test_zip64_and_malformed_tail_metadata_fail_closed(self) -> None:
        valid_zip64 = self.root / "valid-zip64-metadata.zip"
        write_synthetic_zip_metadata(
            valid_zip64,
            member_count=70_000,
            central_directory_size=70_000 * 46,
            zip64=True,
            populate_central_directory=True,
        )
        with valid_zip64.open("rb") as file_object:
            metadata = _read_zip_directory_metadata(
                file_object,
                file_size=valid_zip64.stat().st_size,
            )
        self.assertTrue(metadata.uses_zip64)
        self.assertEqual(metadata.member_count, 70_000)

        malformed_paths: list[Path] = []

        missing_eocd = self.root / "missing-eocd.zip"
        missing_eocd.write_bytes(b"PK\x03\x04not-a-complete-zip")
        malformed_paths.append(missing_eocd)

        multidisk = self.root / "multidisk.zip"
        write_synthetic_zip_metadata(
            multidisk,
            member_count=1,
            central_directory_size=46,
            zip64=False,
            disk_number=1,
        )
        malformed_paths.append(multidisk)

        missing_zip64_locator = self.root / "missing-zip64-locator.zip"
        missing_zip64_locator.write_bytes(
            struct.pack(
                "<4s4H2IH",
                b"PK\x05\x06",
                0,
                0,
                0xFFFF,
                0xFFFF,
                0xFFFFFFFF,
                0xFFFFFFFF,
                0,
            )
        )
        malformed_paths.append(missing_zip64_locator)

        multidisk_zip64 = self.root / "multidisk-zip64.zip"
        write_synthetic_zip_metadata(
            multidisk_zip64,
            member_count=70_000,
            central_directory_size=70_000 * 46,
            zip64=True,
            locator_disk_count=2,
        )
        malformed_paths.append(multidisk_zip64)

        malformed_zip64_size = self.root / "malformed-zip64-size.zip"
        write_synthetic_zip_metadata(
            malformed_zip64_size,
            member_count=70_000,
            central_directory_size=70_000 * 46,
            zip64=True,
            zip64_record_payload_size=43,
        )
        malformed_paths.append(malformed_zip64_size)

        mismatched_count = self.root / "mismatched-member-count.zip"
        write_synthetic_zip_metadata(
            mismatched_count,
            member_count=1,
            central_directory_member_count=2,
            central_directory_size=2 * 46,
            zip64=False,
            populate_central_directory=True,
        )
        malformed_paths.append(mismatched_count)

        member_disk_start = self.root / "member-disk-start.zip"
        write_synthetic_zip_metadata(
            member_disk_start,
            member_count=1,
            central_directory_size=46,
            zip64=False,
            populate_central_directory=True,
        )
        with member_disk_start.open("r+b") as file_object:
            file_object.seek(34)
            file_object.write(struct.pack("<H", 1))
        malformed_paths.append(member_disk_start)

        for archive_path in malformed_paths:
            with self.subTest(filename=archive_path.name):
                with archive_path.open("rb") as file_object:
                    with self.assertRaises(_InvalidZipMetadataError):
                        _read_zip_directory_metadata(
                            file_object,
                            file_size=archive_path.stat().st_size,
                        )
                with patch(
                    "app.modules.document_processing.ooxml_validator."
                    "zipfile.ZipFile"
                ) as zip_file:
                    self.assertEqual(
                        validate_ooxml_archive(
                            archive_path,
                            required_member="word/document.xml",
                            max_uncompressed_bytes=1024,
                        ),
                        EXIT_INVALID_ZIP_METADATA,
                    )
                zip_file.assert_not_called()

    def test_ooxml_validator_rejects_unsafe_and_ambiguous_members(self) -> None:
        required_member = "word/document.xml"
        cases = (
            ("traversal.docx", "../escape.bin", None, EXIT_UNSAFE_MEMBER),
            ("absolute.docx", "/escape.bin", None, EXIT_UNSAFE_MEMBER),
            ("backslash.docx", r"word\\escape.bin", None, EXIT_UNSAFE_MEMBER),
            ("control.docx", "word/bad\x01.bin", None, EXIT_UNSAFE_MEMBER),
            ("lzma.docx", "word/extra.bin", zipfile.ZIP_LZMA, EXIT_UNSUPPORTED_COMPRESSION),
        )
        for filename, unsafe_name, compression, expected_exit in cases:
            with self.subTest(filename=filename):
                archive_path = self.root / filename
                selected_compression = (
                    zipfile.ZIP_DEFLATED if compression is None else compression
                )
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(
                        required_member,
                        b"<document/>",
                        compress_type=zipfile.ZIP_DEFLATED,
                    )
                    archive.writestr(
                        unsafe_name,
                        b"unsafe",
                        compress_type=selected_compression,
                    )
                self.assertEqual(
                    validate_ooxml_archive(
                        archive_path,
                        required_member=required_member,
                        max_uncompressed_bytes=1024,
                    ),
                    expected_exit,
                )

        symlink_archive = self.root / "symlink.docx"
        symlink_member = zipfile.ZipInfo("word/link.bin")
        symlink_member.create_system = 3
        symlink_member.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(symlink_archive, "w") as archive:
            archive.writestr(required_member, b"<document/>")
            archive.writestr(symlink_member, b"../target")
        self.assertEqual(
            validate_ooxml_archive(
                symlink_archive,
                required_member=required_member,
                max_uncompressed_bytes=1024,
            ),
            EXIT_UNSAFE_MEMBER,
        )

        duplicate_archive = self.root / "duplicate.docx"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate_archive, "w") as archive:
                archive.writestr(required_member, b"<document/>")
                archive.writestr("word/duplicate.bin", b"first")
                archive.writestr("word/duplicate.bin", b"second")
        self.assertEqual(
            validate_ooxml_archive(
                duplicate_archive,
                required_member=required_member,
                max_uncompressed_bytes=1024,
            ),
            EXIT_DUPLICATE_MEMBER,
        )

    def test_ooxml_validation_uses_fixed_isolated_timeout(self) -> None:
        calls: list[tuple[list[str], dict]] = []

        def timeout_runner(command, **kwargs):
            calls.append((list(command), kwargs))
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        factory = FakeProcessFactory()
        preparer = LibreOfficeLegacyOfficePreparer(
            self.config(),
            environment={
                "HOME": "/Users/docsense",
                "PATH": "/usr/bin:/bin",
                "ANYTHINGLLM_API_KEY": "must-not-reach-validator",
            },
            process_factory=factory,
            ooxml_validation_runner=timeout_runner,
        )
        self.assert_error_code(
            "ooxml_validation_timeout",
            lambda: preparer.prepare(
                self.source(".doc"),
                job_id="validation-timeout",
            ),
        )
        command, kwargs = calls[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:3], ["-I", "-S"])
        self.assertIn("ooxml_validator.py", command[3])
        self.assertEqual(
            command[-1],
            str(_OOXML_MAX_UNCOMPRESSED_BYTES),
        )
        self.assertEqual(
            kwargs["timeout"],
            _OOXML_VALIDATION_TIMEOUT_SECONDS,
        )
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("ANYTHINGLLM_API_KEY", kwargs["env"])

    @unittest.skipUnless(
        sys.platform == "darwin",
        "真实 OOXML 校验超时进程回收仅在本轮 macOS 实机验证",
    )
    def test_macos_real_ooxml_validator_timeout_reaps_pid_and_job(self) -> None:
        hang_script = self.root / "hang_validator.py"
        pid_path = hang_script.with_suffix(".pid")
        hang_script.write_text(
            "import os, time\n"
            "from pathlib import Path\n"
            "Path(__file__).with_suffix(\".pid\").write_text(str(os.getpid()))\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        preparer, _ = self.preparer()
        with (
            patch(
                "app.modules.document_processing.libreoffice."
                "_ooxml_validator_script_path",
                return_value=hang_script,
            ),
            patch(
                "app.modules.document_processing.libreoffice."
                "_OOXML_VALIDATION_TIMEOUT_SECONDS",
                0.2,
            ),
        ):
            self.assert_error_code(
                "ooxml_validation_timeout",
                lambda: preparer.prepare(
                    self.source(".doc"),
                    job_id="real-validator-timeout",
                ),
            )

        self.assertTrue(pid_path.is_file())
        validator_pid = int(pid_path.read_text(encoding="utf-8"))
        with self.assertRaises(ProcessLookupError):
            os.kill(validator_pid, 0)
        jobs_root = preparer.config.jobs_root
        self.assertEqual(
            list(jobs_root.iterdir()) if jobs_root.exists() else [],
            [],
        )

    def test_validator_contract_and_internal_exit_codes_are_distinct(self) -> None:
        output_path = self.root / "output.docx"
        output_path.write_bytes(b"placeholder")
        cases = (
            (25, "ooxml_validator_contract_error"),
            (26, "ooxml_validation_internal_error"),
            (30, "ooxml_zip_directory_limit_exceeded"),
            (31, "invalid_ooxml_zip"),
            (2, "ooxml_validation_internal_error"),
            (-9, "ooxml_validation_internal_error"),
        )
        for returncode, expected_code in cases:
            with self.subTest(returncode=returncode):
                preparer = LibreOfficeLegacyOfficePreparer(
                    LegacyOfficeConfig.disabled(jobs_root=self.root / "unused"),
                    ooxml_validation_runner=lambda command, **kwargs: (
                        subprocess.CompletedProcess(command, returncode)
                    ),
                )
                with self.assertLogs(
                    "app.modules.document_processing.libreoffice",
                    level="ERROR",
                ) as logs:
                    self.assert_error_code(
                        expected_code,
                        lambda: preparer._validate_ooxml_in_isolated_process(
                            output_path,
                            required_member="word/document.xml",
                        ),
                    )
                self.assertIn(
                    f"validator_returncode={returncode}",
                    "\n".join(logs.output),
                )

    def test_copied_input_signature_is_rechecked_after_source_race(self) -> None:
        preparer, factory = self.preparer()
        source = self.source(".doc")
        original_validate = preparer._validate_source

        def validate_then_replace(path: Path) -> None:
            original_validate(path)
            path.write_bytes(b"forged after initial validation")

        with patch.object(
            preparer,
            "_validate_source",
            side_effect=validate_then_replace,
        ):
            self.assert_error_code(
                "invalid_ole2_signature",
                lambda: preparer.prepare(source, job_id="raced"),
            )
        self.assertEqual(factory.conversion_commands, [])

    def test_cleanup_os_error_does_not_reverse_success_and_sweep_retries(self) -> None:
        preparer, _ = self.preparer()
        result = preparer.prepare(self.source(".doc"), job_id="cleanup-failure")
        job_path = result.prepared_path.parent.parent
        with patch(
            "app.modules.document_processing.libreoffice.shutil.rmtree",
            side_effect=OSError("private cleanup path"),
        ):
            result.close()
        self.assertTrue(job_path.exists())
        # The result lease is intentionally idempotent.  The next startup sweep
        # owns retrying the residual directory.
        self.assertEqual(preparer.sweep_stale_jobs(), 1)
        self.assertFalse(job_path.exists())

    def test_cleanup_failure_is_swallowed_and_startup_sweep_is_scoped(self) -> None:
        callback_result = LegacyOfficePreparationResult(
            original_path=Path("a.doc"),
            prepared_path=Path("a.docx"),
            source_suffix=".doc",
            target_suffix=".docx",
            libreoffice_version="26.2.5.2",
            converted=True,
        )
        # Default no-op cleanup remains idempotent.
        callback_result.close()
        callback_result.close()

        preparer, _ = self.preparer()
        jobs_root = preparer.config.jobs_root
        owned = jobs_root / "job-owned"
        legacy_windows_owned = jobs_root / "job-legacy-windows"
        unowned = jobs_root / "job-unowned"
        unrelated = jobs_root / "other"
        for directory in (owned, legacy_windows_owned, unowned, unrelated):
            directory.mkdir(parents=True)
        marker_bytes = _OWNED_JOB_MARKER_VALUE.encode("ascii")
        (owned / _OWNED_JOB_MARKER_NAME).write_bytes(marker_bytes)
        # 兼容修复前 Windows 文本模式写出的 CRLF 标记，确保历史残留仍可由启动扫描回收。
        (legacy_windows_owned / _OWNED_JOB_MARKER_NAME).write_bytes(
            marker_bytes.replace(b"\n", b"\r\n")
        )
        (unowned / "data").write_text("keep", encoding="utf-8")
        (unrelated / _OWNED_JOB_MARKER_NAME).write_bytes(marker_bytes)
        symlink = jobs_root / "job-link"
        try:
            symlink.symlink_to(owned, target_is_directory=True)
        except OSError:
            symlink = None
        self.assertEqual(preparer.sweep_stale_jobs(), 2)
        self.assertFalse(owned.exists())
        self.assertFalse(legacy_windows_owned.exists())
        self.assertTrue(unowned.exists())
        self.assertTrue(unrelated.exists())
        if symlink is not None:
            self.assertTrue(symlink.is_symlink())

    def test_posix_timeout_terminates_process_group(self) -> None:
        factory = FakeProcessFactory()
        factory.timeout_conversion = True
        executable = "/opt/docsense/soffice"
        preparer = LibreOfficeLegacyOfficePreparer(
            self.config(executable=executable),
            platform_name="darwin",
            process_factory=factory,
            path_is_file=lambda value: value == executable,
            path_is_executable=lambda value: value == executable,
        )
        sent_signals: list[tuple[int, int]] = []

        def record_signal(pgid: int, sent_signal: int) -> None:
            sent_signals.append((pgid, sent_signal))
            if sent_signal == getattr(signal, "SIGKILL", 9):
                factory.last_process.kill()

        with patch(
            "app.modules.document_processing.libreoffice.os.killpg",
            side_effect=record_signal,
            create=True,
        ):
            self.assert_error_code(
                "conversion_timeout",
                lambda: preparer.prepare(self.source(".xls"), job_id="timeout"),
            )
        timed_out_pid = factory.last_process.pid
        self.assertIn((timed_out_pid, signal.SIGTERM), sent_signals)
        self.assertIn(
            (timed_out_pid, getattr(signal, "SIGKILL", 9)),
            sent_signals,
        )
        self.assertEqual(factory.last_process.returncode, -9)

    def test_windows_uses_new_process_group_and_taskkill_tree(self) -> None:
        factory = FakeProcessFactory()
        factory.timeout_conversion = True
        taskkill_calls: list[tuple[list[str], dict]] = []
        config = self.config()
        preparer = LibreOfficeLegacyOfficePreparer(
            config,
            platform_name="win32",
            environment={
                "SystemRoot": r"C:\Windows",
                "PATH": r"C:\Windows\System32",
                "TEMP": r"C:\Temp",
                "TMP": r"C:\HostTmp",
                "USERPROFILE": r"C:\Users\Host",
                "APPDATA": r"C:\Users\Host\AppData\Roaming",
                "LOCALAPPDATA": r"C:\Users\Host\AppData\Local",
                "ANYTHINGLLM_API_KEY": "must-not-reach-child",
            },
            process_factory=factory,
            taskkill_runner=lambda command, **kwargs: taskkill_calls.append(
                (list(command), kwargs)
            ),
            path_is_file=lambda value: value == str(self.executable),
            path_is_executable=lambda value: True,
        )
        self.assert_error_code(
            "conversion_timeout",
            lambda: preparer.prepare(self.source(".doc"), job_id="win-timeout"),
        )
        conversion_kwargs = factory.conversion_commands
        # A timed-out fake raises before recording the conversion command, so
        # inspect the final Popen call directly.
        _, kwargs = factory.calls[-1]
        self.assertEqual(kwargs["creationflags"], 0x00000200)
        self.assertNotIn("start_new_session", kwargs)
        self.assertEqual(kwargs["env"]["SYSTEMROOT"], r"C:\Windows")
        self.assertNotIn("ANYTHINGLLM_API_KEY", kwargs["env"])
        conversion_job = Path(factory.calls[-1][0][-1]).parent
        self.assertEqual(
            Path(kwargs["env"]["USERPROFILE"]),
            conversion_job / "home",
        )
        self.assertEqual(
            Path(kwargs["env"]["TEMP"]),
            conversion_job / "tmp",
        )
        self.assertEqual(
            Path(kwargs["env"]["TMP"]),
            conversion_job / "tmp",
        )
        self.assertEqual(
            Path(kwargs["env"]["APPDATA"]),
            conversion_job / "home" / "AppData" / "Roaming",
        )
        self.assertEqual(
            Path(kwargs["env"]["LOCALAPPDATA"]),
            conversion_job / "home" / "AppData" / "Local",
        )
        command, taskkill_kwargs = taskkill_calls[0]
        self.assertEqual(command[0], "taskkill")
        self.assertIn("/T", command)
        self.assertIn("/F", command)
        self.assertFalse(taskkill_kwargs["shell"])
        self.assertEqual(conversion_kwargs, [])

    def test_thread_semaphore_limits_conversions(self) -> None:
        factory = FakeProcessFactory()
        factory.conversion_delay = 0.01
        validation_lock = threading.Lock()
        active_validations = 0
        max_active_validations = 0

        def validation_runner(command, **kwargs):
            nonlocal active_validations, max_active_validations
            with validation_lock:
                active_validations += 1
                max_active_validations = max(
                    max_active_validations,
                    active_validations,
                )
            try:
                time.sleep(0.08)
                return subprocess.CompletedProcess(command, 0)
            finally:
                with validation_lock:
                    active_validations -= 1

        preparer = LibreOfficeLegacyOfficePreparer(
            self.config(max_concurrency=1),
            process_factory=factory,
            ooxml_validation_runner=validation_runner,
        )
        sources = [self.root / f"source-{index}.doc" for index in range(2)]
        for source in sources:
            write_ole2(source)

        def convert(source: Path) -> bool:
            with preparer.prepare(source, job_id=source.stem) as result:
                return result.converted

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(convert, sources))
        self.assertEqual(results, [True, True])
        self.assertEqual(factory.max_active_conversions, 1)
        self.assertEqual(max_active_validations, 1)

    def test_diagnostics_are_redacted_flat_and_truncated(self) -> None:
        private_path = "/Users/private/runtime/office_conversion/jobs/job-secret"
        diagnostic = _sanitize_for_log(
            f"failed at {private_path}\n" + "x" * 5000,
            sensitive_values=(private_path,),
        )
        self.assertNotIn("Users/private", diagnostic)
        self.assertNotIn("\n", diagnostic)
        self.assertLessEqual(len(diagnostic), 2048)


if __name__ == "__main__":
    unittest.main()
