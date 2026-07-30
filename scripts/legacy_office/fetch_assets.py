#!/usr/bin/env python3
"""Download and verify pinned LibreOffice installers and license texts."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
LOCK_PATH = SCRIPT_DIR / "artifacts.lock.json"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR.parent.parent / "dist" / "legacy-office" / "downloads"
USER_AGENT = "DocSense-Legacy-Office-Packager/1.0"
CHUNK_SIZE = 1024 * 1024
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
logger = logging.getLogger(__name__)


def configure_cli_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_lock(path: Path = LOCK_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("schemaVersion") != 1:
        raise ValueError("不支持的 artifacts.lock.json schemaVersion")
    return data


def _download_verified(*, url: str, destination: Path, expected_sha256: str) -> None:
    expected = expected_sha256.lower()
    if destination.is_file():
        actual = sha256_file(destination)
        if actual == expected:
            logger.info("已验证，跳过下载：%s", destination.name)
            return
        raise RuntimeError(
            f"现有文件校验失败：{destination.name}；期望 {expected}，实际 {actual}。"
            "请人工移走该文件后重试。"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".part",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            digest = hashlib.sha256()
            total = 0
            with urllib.request.urlopen(request, timeout=120) as response:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError(f"下载超过 2 GiB 安全上限：{destination.name}")
                    digest.update(chunk)
                    temporary.write(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())

        actual = digest.hexdigest()
        if actual != expected:
            raise RuntimeError(
                f"下载校验失败：{destination.name}；期望 {expected}，实际 {actual}"
            )
        os.replace(temporary_path, destination)
        temporary_path = None
        logger.info("下载并验证完成：%s", destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def selected_assets(lock: dict[str, Any], platform: str) -> Iterable[dict[str, str]]:
    installers = lock["installers"]
    selected_platforms = installers.keys() if platform == "all" else (platform,)
    for platform_name in selected_platforms:
        yield installers[platform_name]
    yield from lock["licenses"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="下载并按锁定 SHA-256 校验 LibreOffice 26.2.5 离线交付资产"
    )
    parser.add_argument(
        "--platform",
        choices=("all", "windows-x64", "macos-arm64"),
        default="all",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="下载目录；默认 dist/legacy-office/downloads",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="只校验已存在文件，不访问网络",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_cli_logging()
    args = parse_args(argv)
    lock = load_lock()
    output_dir = args.output_dir.resolve()
    failures: list[str] = []
    for asset in selected_assets(lock, args.platform):
        destination = output_dir / asset["filename"]
        try:
            if args.verify_only:
                if not destination.is_file():
                    raise FileNotFoundError(f"缺少文件：{destination}")
                actual = sha256_file(destination)
                if actual != asset["sha256"].lower():
                    raise RuntimeError(
                        f"校验失败：{destination.name}；期望 {asset['sha256']}，实际 {actual}"
                    )
                logger.info("校验通过：%s", destination)
            else:
                _download_verified(
                    url=asset["url"],
                    destination=destination,
                    expected_sha256=asset["sha256"],
                )
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append(str(exc))

    if failures:
        for failure in failures:
            logger.error("%s", failure)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
