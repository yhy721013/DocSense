#!/usr/bin/env python3
"""Build a verified, platform-specific LibreOffice offline bundle."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fetch_assets import DEFAULT_OUTPUT_DIR as DEFAULT_DOWNLOAD_DIR
from fetch_assets import configure_cli_logging, load_lock, sha256_file


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_DIST_DIR = PROJECT_ROOT / "dist" / "legacy-office"
TEMPLATE_PATH = SCRIPT_DIR / "bundle_manifest.template.json"
OLE2_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
MAX_SAMPLE_BYTES = 128 * 1024 * 1024
logger = logging.getLogger(__name__)

PLATFORM_ASSETS = {
    "windows-x64": {
        "archive": "docsense-libreoffice-26.2.5-windows-x64-offline.zip",
        "scripts": {
            "Install-Windows.ps1": "Install.ps1",
            "Preflight-Windows.ps1": "Preflight.ps1",
        },
    },
    "macos-arm64": {
        "archive": "docsense-libreoffice-26.2.5-macos-arm64-offline.zip",
        "scripts": {
            "install_macos.sh": "install.sh",
            "preflight_macos.sh": "preflight.sh",
            "smoke_test_macos.py": "smoke_test.py",
        },
    },
}


def _resolve_sample(samples_dir: Path, filename: str) -> Path:
    direct = samples_dir / filename
    nested = samples_dir / "legacy" / filename
    if direct.is_file():
        return direct
    if nested.is_file():
        return nested
    raise FileNotFoundError(f"缺少 smoke 样本：{filename}")


def _verify_file(path: Path, expected_sha256: str, *, label: str) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"缺少{label}：{path}")
    actual = sha256_file(path)
    if actual != expected_sha256.lower():
        raise RuntimeError(
            f"{label} SHA-256 不匹配：{path.name}；"
            f"期望 {expected_sha256.lower()}，实际 {actual}"
        )
    return actual


def _verify_sample(path: Path, expected_sha256: str) -> str:
    size = path.stat().st_size
    if size <= 8:
        raise RuntimeError(f"smoke 样本为空或过短：{path.name}")
    if size > MAX_SAMPLE_BYTES:
        raise RuntimeError(f"smoke 样本超过 128 MiB：{path.name}")
    with path.open("rb") as handle:
        if handle.read(len(OLE2_MAGIC)) != OLE2_MAGIC:
            raise RuntimeError(f"smoke 样本不是 Office OLE2 二进制文件：{path.name}")
    return _verify_file(path, expected_sha256, label="smoke 样本")


def _copy_file(source: Path, destination: Path, *, executable: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if executable:
        destination.chmod(destination.stat().st_mode | 0o755)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_sources(path: Path, sample_records: list[dict[str, Any]]) -> None:
    lines = [
        "# Smoke 样本来源",
        "",
        "三份二进制样本只随离线交付包分发，不进入 DocSense Git/LFS。",
        "构建脚本已按锁定 SHA-256 验证每个文件。",
        "",
    ]
    for record in sample_records:
        lines.extend(
            [
                f"## {record['filename']}",
                "",
                f"- 格式：Office 97–2003 `{record['format']}`",
                f"- 来源类型：`{record['origin']}`",
                f"- SHA-256：`{record['sha256']}`",
            ]
        )
        if record.get("repository"):
            lines.append(f"- 仓库：{record['repository']}")
        if record.get("commit"):
            lines.append(f"- 固定提交：`{record['commit']}`")
        if record.get("sourcePath"):
            lines.append(f"- 原路径：`{record['sourcePath']}`")
        if record.get("url"):
            lines.append(f"- 固定原件 URL：{record['url']}")
        if record.get("license"):
            lines.append(f"- 许可证：`{record['license']}`")
        if record["origin"] == "project-local-generated":
            lines.append("- 说明：项目方本地生成的最小 smoke 样本，无外部素材来源。")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_checksums(bundle_root: Path) -> None:
    entries: list[str] = []
    checksum_path = bundle_root / "SHA256SUMS"
    for path in sorted(bundle_root.rglob("*")):
        if path.is_file() and path != checksum_path:
            relative = path.relative_to(bundle_root).as_posix()
            entries.append(f"{sha256_file(path)}  {relative}")
    checksum_path.write_text("\n".join(entries) + "\n", encoding="utf-8")


def _write_archive(bundle_root: Path, archive_path: Path) -> None:
    temporary_path = archive_path.with_name(f".{archive_path.name}.{os.getpid()}.part")
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for path in sorted(bundle_root.rglob("*")):
                if path.is_file():
                    archive.write(
                        path,
                        arcname=(Path(bundle_root.name) / path.relative_to(bundle_root)).as_posix(),
                    )
        os.replace(temporary_path, archive_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def build_bundle(
    *,
    platform: str,
    downloads_dir: Path,
    samples_dir: Path,
    output_dir: Path,
    overwrite: bool,
) -> Path:
    lock = load_lock()
    platform_config = PLATFORM_ASSETS[platform]
    installer = lock["installers"][platform]
    archive_path = output_dir / platform_config["archive"]
    if archive_path.exists() and not overwrite:
        raise FileExistsError(
            f"输出已存在：{archive_path}；确认替换时显式传入 --overwrite"
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    installer_path = downloads_dir / installer["filename"]
    _verify_file(installer_path, installer["sha256"], label="LibreOffice 安装包")

    license_paths: list[tuple[dict[str, str], Path]] = []
    for license_record in lock["licenses"]:
        license_path = downloads_dir / license_record["filename"]
        _verify_file(license_path, license_record["sha256"], label="许可证文件")
        license_paths.append((license_record, license_path))

    sample_paths: list[tuple[str, dict[str, Any], Path]] = []
    for filename, record in lock["smokeSamples"].items():
        sample_path = _resolve_sample(samples_dir, filename)
        _verify_sample(sample_path, record["sha256"])
        sample_paths.append((filename, record, sample_path))

    with tempfile.TemporaryDirectory(
        prefix=f".docsense-{platform}-", dir=output_dir
    ) as temporary_dir:
        bundle_root = Path(temporary_dir) / archive_path.stem
        bundle_root.mkdir()
        _copy_file(installer_path, bundle_root / "installer" / installer["filename"])
        for license_record, license_path in license_paths:
            _copy_file(
                license_path,
                bundle_root / "licenses" / license_record["filename"],
            )
        sample_records: list[dict[str, Any]] = []
        for filename, record, sample_path in sample_paths:
            _copy_file(sample_path, bundle_root / "samples" / filename)
            sample_records.append({"filename": filename, **record})

        for source_name, destination_name in platform_config["scripts"].items():
            _copy_file(
                SCRIPT_DIR / source_name,
                bundle_root / destination_name,
                executable=destination_name.endswith((".sh", ".py")),
            )
        _copy_file(SCRIPT_DIR / "BUNDLE_README.md", bundle_root / "README.md")
        _copy_file(
            SCRIPT_DIR / "THIRD_PARTY_NOTICES.md",
            bundle_root / "THIRD_PARTY_NOTICES.md",
        )
        _write_sources(bundle_root / "samples" / "SOURCES.md", sample_records)

        with TEMPLATE_PATH.open("r", encoding="utf-8") as handle:
            manifest = copy.deepcopy(json.load(handle))
        manifest.update(
            {
                "libreOfficeVersion": lock["libreOfficeVersion"],
                "allowedVersionSeries": lock["allowedVersionSeries"],
                "platform": platform,
                "architecture": installer["architecture"],
                "certification": installer["certification"],
                "installer": {
                    "filename": installer["filename"],
                    "url": installer["url"],
                    "checksumUrl": installer["checksumUrl"],
                    "sha256": installer["sha256"],
                },
                "licenses": lock["licenses"],
                "smokeSamples": sample_records,
            }
        )
        _write_json(bundle_root / "MANIFEST.json", manifest)
        _write_checksums(bundle_root)
        _write_archive(bundle_root, archive_path)

    logger.info("离线包已生成：%s", archive_path)
    logger.info("离线包 SHA-256：%s", sha256_file(archive_path))
    return archive_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="使用已校验安装包和 Git 外 smoke 样本生成离线交付 zip"
    )
    parser.add_argument(
        "--platform",
        choices=tuple(PLATFORM_ASSETS),
        required=True,
    )
    parser.add_argument(
        "--downloads-dir",
        type=Path,
        default=DEFAULT_DOWNLOAD_DIR,
    )
    parser.add_argument("--samples-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_DIST_DIR)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="明确允许原子替换同名最终 zip；不删除其他输出",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_cli_logging()
    args = parse_args(argv)
    try:
        build_bundle(
            platform=args.platform,
            downloads_dir=args.downloads_dir.resolve(),
            samples_dir=args.samples_dir.resolve(),
            output_dir=args.output_dir.resolve(),
            overwrite=args.overwrite,
        )
    except (KeyError, OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
