from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

import fitz

from app.services.core.config import OCRConfig


logger = logging.getLogger(__name__)


def build_ocr_cache_key(path: Union[str, Path], size: int, mtime_ns: int) -> str:
    resolved_path = str(Path(path).resolve(strict=False)).replace("\\", "/")
    fingerprint = f"{resolved_path}|{size}|{mtime_ns}"
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def prepare_file_for_upload(file_path: str, ocr_config: OCRConfig) -> str:
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return str(path)

    if not ocr_config.enabled:
        return str(path)

    if path.suffix.lower() != ".pdf":
        return str(path)

    if not is_scanned_pdf(
        str(path),
        sample_pages=ocr_config.sample_pages,
        text_threshold=ocr_config.text_threshold,
    ):
        return str(path)

    try:
        markdown_path = ocr_pdf_to_markdown(path, ocr_config)
        logger.info("扫描件 OCR 完成，改为上传 Markdown: %s -> %s", path, markdown_path)
        return str(markdown_path)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("扫描件 OCR 失败，降级直传原 PDF: %s (%s)", path, exc)
        return str(path)


def ocr_pdf_to_markdown(pdf_path: Path, ocr_config: OCRConfig) -> Path:
    source_path = pdf_path.resolve(strict=True)
    source_stat = source_path.stat()

    cache_root = _resolve_cache_root(ocr_config.cache_dir)
    cache_key = build_ocr_cache_key(source_path, source_stat.st_size, source_stat.st_mtime_ns)
    markdown_path = _safe_cache_file(cache_root, f"{cache_key}.md")
    metadata_path = _safe_cache_file(cache_root, f"{cache_key}.meta.json")

    if markdown_path.exists() and markdown_path.stat().st_size > 0:
        return markdown_path

    _configure_tessdata(ocr_config)

    page_count = 0
    markdown_lines = []
    generated_at = datetime.now(timezone.utc).isoformat()

    with fitz.open(str(source_path)) as document:
        page_count = len(document)
        markdown_lines.extend(
            [
                "# OCR Markdown",
                "",
                f"- Source File: `{source_path.name}`",
                f"- Generated At (UTC): {generated_at}",
                f"- OCR Languages: `{ocr_config.languages}`",
                f"- OCR DPI: {ocr_config.dpi}",
                f"- Total Pages: {page_count}",
                "",
            ]
        )

        for page_index in range(page_count):
            page = document[page_index]
            textpage = page.get_textpage_ocr(language=ocr_config.languages, dpi=ocr_config.dpi)
            page_text = page.get_text("text", textpage=textpage).strip()

            markdown_lines.append(f"## Page {page_index + 1}")
            markdown_lines.append("")
            markdown_lines.append(page_text if page_text else "")
            markdown_lines.append("")

    markdown_text = "\n".join(markdown_lines).rstrip() + "\n"
    _atomic_write_text(markdown_path, markdown_text)

    metadata = {
        "cache_key": cache_key,
        "source_file": source_path.name,
        "source_path": str(source_path),
        "source_size": source_stat.st_size,
        "source_mtime_ns": source_stat.st_mtime_ns,
        "generated_at_utc": generated_at,
        "ocr_languages": ocr_config.languages,
        "ocr_dpi": ocr_config.dpi,
        "page_count": page_count,
        "markdown_path": str(markdown_path),
    }
    _atomic_write_text(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")

    return markdown_path


def _configure_tessdata(ocr_config: OCRConfig) -> None:
    if not ocr_config.tessdata_prefix:
        return
    os.environ["TESSDATA_PREFIX"] = ocr_config.tessdata_prefix


def _resolve_cache_root(cache_dir: str) -> Path:
    cache_root = Path(cache_dir).resolve(strict=False)
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root


def _safe_cache_file(cache_root: Path, file_name: str) -> Path:
    candidate = (cache_root / file_name).resolve(strict=False)
    candidate.relative_to(cache_root)
    return candidate


def _atomic_write_text(target: Path, content: str) -> None:
    temp_path = target.with_suffix(target.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(target)


def is_scanned_pdf(pdf_path: str, sample_pages: int = 3, text_threshold: int = 50) -> bool:
    # 通过抽样页文本长度判断是否为扫描件
    try:
        with fitz.open(pdf_path) as doc:
            total_pages = len(doc)
            pages_to_check = min(sample_pages, total_pages)
            if pages_to_check <= 0:
                return True

            total_text_length = 0
            for page_num in range(pages_to_check):
                page = doc[page_num]
                text = page.get_text().strip()
                total_text_length += len(text)

        avg_text_per_page = total_text_length / pages_to_check
        return avg_text_per_page < text_threshold
    except Exception:
        return True
