from __future__ import annotations

import unittest
from pathlib import Path

from app.services.utils.mhtml_normalizer import extract_text_from_mhtml, is_mhtml_file, normalize_mhtml_file
from tests import workspace_tempdir


MHTML_SAMPLE = """From: <Saved by Blink>
Subject: sample
MIME-Version: 1.0
Content-Type: multipart/related; boundary="----=_NextPart_000_0000"

------=_NextPart_000_0000
Content-Type: text/html; charset="utf-8"
Content-Transfer-Encoding: 8bit

<html>
  <head>
    <title>Test Title</title>
    <style>.hidden { display:none; }</style>
  </head>
  <body>
    <h1>Hello MHTML</h1>
    <p>Second line.</p>
    <script>console.log('ignore');</script>
  </body>
</html>
------=_NextPart_000_0000--
"""


class MHTMLNormalizerTests(unittest.TestCase):
    def test_is_mhtml_file_recognizes_mhtml_and_mht(self):
        # 当前实现先确认文件真实存在，再检查扩展名或 MIME 文件头。用临时真实文件
        # 固定该语义，避免把一个并不存在的路径误当作可处理输入。
        with workspace_tempdir() as tmp:
            mhtml_path = Path(tmp) / "demo.mhtml"
            mht_path = Path(tmp) / "demo.mht"
            text_path = Path(tmp) / "demo.txt"
            for path in (mhtml_path, mht_path, text_path):
                path.write_text("plain text", encoding="utf-8")

            self.assertTrue(is_mhtml_file(str(mhtml_path)))
            self.assertTrue(is_mhtml_file(str(mht_path)))
            self.assertFalse(is_mhtml_file(str(text_path)))

        self.assertFalse(is_mhtml_file("missing-demo.mhtml"))

    def test_extract_text_from_mhtml_returns_clean_text(self):
        with workspace_tempdir() as tmp:
            sample = Path(tmp) / "sample.mhtml"
            sample.write_text(MHTML_SAMPLE, encoding="utf-8")

            text = extract_text_from_mhtml(str(sample))

        self.assertIn("Test Title", text)
        self.assertIn("Hello MHTML", text)
        self.assertIn("Second line.", text)
        self.assertNotIn("console.log", text)

    def test_normalize_mhtml_file_extracts_html_text_to_markdown(self):
        with workspace_tempdir() as tmp:
            sample = Path(tmp) / "sample.mhtml"
            sample.write_text(MHTML_SAMPLE, encoding="utf-8")

            # 显式关闭可选 PDF 转换，稳定验证无需浏览器/外部进程的 Markdown 降级路径。
            # PDF 转换是否可用取决于开发机安装，不能成为本单元测试的隐含前提。
            output = normalize_mhtml_file(
                str(sample),
                use_pdf_conversion=False,
            )

            self.assertTrue(output.endswith(".normalized.md"))
            text = Path(output).read_text(encoding="utf-8")

        self.assertIn("Test Title", text)
        self.assertIn("Hello MHTML", text)
        self.assertIn("Second line.", text)
