from __future__ import annotations

import unittest
from pathlib import Path

from app.utils.mhtml_normalizer import extract_text_from_mhtml, is_mhtml_file, normalize_mhtml_file
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
        self.assertTrue(is_mhtml_file("demo.mhtml"))
        self.assertTrue(is_mhtml_file("demo.mht"))
        self.assertFalse(is_mhtml_file("demo.txt"))

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

            output = normalize_mhtml_file(str(sample))

            self.assertTrue(output.endswith(".normalized.md"))
            text = Path(output).read_text(encoding="utf-8")

        self.assertIn("Test Title", text)
        self.assertIn("Hello MHTML", text)
        self.assertIn("Second line.", text)
