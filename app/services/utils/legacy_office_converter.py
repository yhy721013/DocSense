from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _OfficeFormat:
    target_suffix: str
    convert_filter: str
    required_member: str


_LEGACY_OFFICE_FORMATS = {
    ".doc": _OfficeFormat(
        target_suffix=".docx",
        convert_filter="docx:Office Open XML Text",
        required_member="word/document.xml",
    ),
    ".xls": _OfficeFormat(
        target_suffix=".xlsx",
        convert_filter="xlsx:Calc Office Open XML",
        required_member="xl/workbook.xml",
    ),
    ".ppt": _OfficeFormat(
        target_suffix=".pptx",
        convert_filter="pptx:Impress MS PowerPoint 2007 XML",
        required_member="ppt/presentation.xml",
    ),
}


class LegacyOfficeConversionError(RuntimeError):
    """旧版 Office 文件无法转换为 OOXML 格式。"""


def convert_legacy_office_file(
    file_path: str,
    *,
    soffice_path: str | None = None,
    timeout_seconds: int = 120,
) -> str:
    """将 .doc/.xls/.ppt 转换为对应的 OOXML 文件，其他格式原样返回。"""
    source_path = Path(file_path)
    office_format = _LEGACY_OFFICE_FORMATS.get(source_path.suffix.lower())
    if office_format is None:
        return str(source_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"待转换的旧版 Office 文件不存在: {source_path}")
    if timeout_seconds <= 0:
        raise ValueError("Office 转换超时时间必须大于 0")

    soffice = _find_soffice(soffice_path)
    target_path = source_path.with_suffix(office_format.target_suffix)

    with (
        tempfile.TemporaryDirectory(
            prefix=".docsense-office-output-",
            dir=str(source_path.parent),
        ) as output_dir,
        tempfile.TemporaryDirectory(prefix="docsense-office-profile-") as profile_dir,
    ):
        profile_uri = Path(profile_dir).resolve().as_uri()
        command = [
            str(soffice),
            f"-env:UserInstallation={profile_uri}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to",
            office_format.convert_filter,
            "--outdir",
            output_dir,
            str(source_path),
        ]
        process_env = os.environ.copy()
        if os.name != "nt":
            process_env["XDG_CACHE_HOME"] = str(Path(profile_dir) / "cache")

        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                env=process_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LegacyOfficeConversionError(
                f"旧版 Office 文件转换执行失败: {source_path.name}"
            ) from exc

        generated_path = Path(output_dir) / f"{source_path.stem}{office_format.target_suffix}"
        validation_error = _validate_ooxml(
            generated_path,
            office_format.required_member,
        )
        converter_output = (completed.stdout or "").strip()
        if completed.returncode != 0:
            detail = converter_output or f"退出码 {completed.returncode}"
            raise LegacyOfficeConversionError(
                f"旧版 Office 文件转换失败: {source_path.name}; {detail}"
            )
        if validation_error:
            raise LegacyOfficeConversionError(
                f"旧版 Office 文件转换失败: {source_path.name}; "
                f"{validation_error}"
            )

        generated_path.replace(target_path)

    logger.info(
        "旧版 Office 文件已转换: input_file=%s output_file=%s",
        source_path.name,
        target_path.name,
    )
    return str(target_path)


def _find_soffice(explicit_path: str | None = None) -> Path:
    checked: list[str] = []
    seen: set[str] = set()
    for candidate in _soffice_candidates(explicit_path):
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        checked.append(str(candidate))
        executable = os.name == "nt" or os.access(candidate, os.X_OK)
        if candidate.is_file() and executable:
            return candidate

    raise LegacyOfficeConversionError(
        "检测到旧版 Office 文件，但找不到 LibreOffice soffice。"
        "请按部署平台安装离线包，并配置 SOFFICE_PATH。"
        f" 已检查: {', '.join(checked)}"
    )


def _soffice_candidates(explicit_path: str | None) -> Iterable[Path]:
    if explicit_path:
        yield Path(explicit_path).expanduser()

    configured_path = os.environ.get("SOFFICE_PATH", "").strip()
    if configured_path:
        yield Path(configured_path).expanduser()

    for command in ("soffice", "libreoffice"):
        discovered = shutil.which(command)
        if discovered:
            yield Path(discovered)

    system_name = platform.system()
    if system_name == "Windows":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            install_root = os.environ.get(variable, "").strip()
            if install_root:
                yield Path(install_root) / "LibreOffice" / "program" / "soffice.exe"
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            yield Path(local_app_data) / "Programs" / "LibreOffice" / "program" / "soffice.exe"
    elif system_name == "Darwin":
        yield Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        yield Path("/Applications/LibreOfficeDev.app/Contents/MacOS/soffice")
    else:
        yield Path("/usr/bin/soffice")
        yield Path("/usr/bin/libreoffice")
        yield Path("/usr/lib64/libreoffice/program/soffice")
        yield Path("/usr/lib/libreoffice/program/soffice")
        yield Path("/opt/libreoffice/program/soffice")
        for candidate in sorted(Path("/opt").glob("libreoffice*/program/soffice")):
            yield candidate


def _validate_ooxml(output_path: Path, required_member: str) -> str:
    if not output_path.is_file():
        return "LibreOffice 未生成目标文件"
    if output_path.stat().st_size == 0:
        return "LibreOffice 生成的目标文件为空"
    if not zipfile.is_zipfile(output_path):
        return "LibreOffice 生成的目标文件不是有效 OOXML"

    try:
        with zipfile.ZipFile(output_path) as archive:
            missing = {"[Content_Types].xml", required_member} - set(archive.namelist())
            if missing:
                return f"OOXML 缺少必要内容: {', '.join(sorted(missing))}"
            damaged_member = archive.testzip()
            if damaged_member:
                return f"OOXML 包含损坏内容: {damaged_member}"
    except (OSError, zipfile.BadZipFile) as exc:
        return f"OOXML 校验失败: {exc}"
    return ""
