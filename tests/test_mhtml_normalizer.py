from __future__ import annotations

import unittest
from pathlib import Path

from app.modules.document_processing.adapters.path_compat import (
    extract_retrieval_text_from_mhtml,
    extract_text_from_mhtml,
    is_mhtml_file,
    normalize_mhtml_file,
    normalize_mhtml_file_for_retrieval,
)
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


MHTML_RETRIEVAL_SAMPLE = """From: <Saved by Blink>
Subject: retrieval sample
MIME-Version: 1.0
Content-Type: multipart/related; boundary="----=_RetrievalBoundary"

------=_RetrievalBoundary
Content-Type: text/html; charset="utf-8"
Content-Transfer-Encoding: 8bit

<html>
  <body>
    <nav>首页 相关推荐 无关导航</nav>
    <main>
      <h1>尼米兹级航空母舰</h1>
      <p>该级舰艇属于航空母舰，正文包含可检索的装备事实。</p>
      <p>该级舰艇属于航空母舰，正文包含可检索的装备事实。</p>
      <div class="reflist columns references-column-width">
        USNI News 2019 2020 2021 2022 2023 https://reference.example
      </div>
    </main>
    <footer>版权信息和站点链接</footer>
  </body>
</html>
------=_RetrievalBoundary--
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

    def test_extract_retrieval_text_prefers_main_and_removes_reference_noise(self):
        with workspace_tempdir() as tmp:
            sample = Path(tmp) / "retrieval.mhtml"
            sample.write_text(MHTML_RETRIEVAL_SAMPLE, encoding="utf-8")

            text = extract_retrieval_text_from_mhtml(str(sample))

        self.assertIn("尼米兹级航空母舰", text)
        self.assertIn("正文包含可检索的装备事实", text)
        self.assertEqual(1, text.count("正文包含可检索的装备事实"))
        for noise in ("相关推荐", "USNI News", "reference.example", "版权信息"):
            self.assertNotIn(noise, text)

    def test_retrieval_text_falls_back_to_clean_body_without_main(self):
        sample_value = MHTML_SAMPLE.replace("<body>", "<body><nav>无关导航</nav>")
        with workspace_tempdir() as tmp:
            sample = Path(tmp) / "fallback.mhtml"
            sample.write_text(sample_value, encoding="utf-8")

            text = extract_retrieval_text_from_mhtml(str(sample))

        self.assertIn("Hello MHTML", text)
        self.assertNotIn("无关导航", text)

    def test_normalize_mhtml_file_for_retrieval_writes_dedicated_markdown(self):
        with workspace_tempdir() as tmp:
            sample = Path(tmp) / "retrieval.mhtml"
            sample.write_text(MHTML_RETRIEVAL_SAMPLE, encoding="utf-8")

            output = normalize_mhtml_file_for_retrieval(str(sample))
            output_path = Path(output)

            self.assertTrue(output_path.name.endswith(".retrieval.md"))
            self.assertIn("尼米兹级航空母舰", output_path.read_text(encoding="utf-8"))
