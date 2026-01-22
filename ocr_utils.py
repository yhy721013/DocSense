from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def process_pdf_with_paddleocr(
    pdf_path: str,
    output_path: Optional[str] = None,
    lang: str = "ch",
    dpi: int = 150,
    num_workers: Optional[int] = None,
    use_gpu: bool = False,
) -> Optional[str]:
    # 返回 OCR 结果文本路径，失败返回 None
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from paddle_ocr_pdf import pdf_to_text_improved

        if output_path is None:
            base_name = Path(pdf_path).stem
            output_path = f"{base_name}_ocr.txt"

        pdf_to_text_improved(
            pdf_path=pdf_path,
            output_path=output_path,
            lang=lang,
            dpi=dpi,
            test_mode=False,
            num_workers=num_workers,
            use_gpu=use_gpu,
        )

        return output_path if os.path.exists(output_path) else None
    except Exception:
        return None


def process_image_with_paddleocr(
    image_path: str,
    output_path: Optional[str] = None,
    lang: str = "ch",
    use_gpu: bool = False,
) -> Optional[str]:
    # 对图片进行 OCR 并保存为文本文件
    try:
        from paddleocr import PaddleOCR

        if output_path is None:
            base_name = Path(image_path).stem
            output_path = f"{base_name}_ocr.txt"

        ocr_kwargs = {
            "use_angle_cls": True,
            "lang": lang,
        }
        if use_gpu:
            ocr_kwargs["use_gpu"] = True

        ocr = PaddleOCR(**ocr_kwargs)
        result = ocr.ocr(str(image_path))

        texts = []
        if result:
            for line in result[0]:
                if line and len(line) >= 2:
                    text = line[1][0] if isinstance(line[1], (list, tuple)) else str(line[1])
                    texts.append(text)

        output_text = "\n".join(texts)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(output_text)

        return output_path if os.path.exists(output_path) else None
    except Exception:
        return None
