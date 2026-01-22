from __future__ import annotations

from pathlib import Path
from typing import Optional

import fitz


def is_image_file(file_path: str) -> bool:
    image_extensions = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".tif", ".webp"}
    return Path(file_path).suffix.lower() in image_extensions


def is_pdf_file(file_path: str) -> bool:
    return Path(file_path).suffix.lower() == ".pdf"


def is_scanned_pdf(pdf_path: str, sample_pages: int = 3, text_threshold: int = 50) -> bool:
    # 通过抽样页文本长度判断是否为扫描件
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        pages_to_check = min(sample_pages, total_pages)
        total_text_length = 0
        for page_num in range(pages_to_check):
            page = doc[page_num]
            text = page.get_text().strip()
            total_text_length += len(text)
        doc.close()

        avg_text_per_page = total_text_length / pages_to_check if pages_to_check > 0 else 0
        return avg_text_per_page < text_threshold
    except Exception:
        return True
