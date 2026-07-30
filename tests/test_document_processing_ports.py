"""阶段 1H-1 严格 Fake 与端口形状门禁。"""

from __future__ import annotations

import unittest

from tests.fakes.document_processing import StrictDocumentProcessorFake


class DocumentProcessingPortTests(unittest.TestCase):
    def test_strict_fake_rejects_unexpected_call(self) -> None:
        fake = StrictDocumentProcessorFake()
        with self.assertRaisesRegex(AssertionError, "未登记调用"):
            fake.process(object())  # type: ignore[arg-type]

    def test_strict_fake_rejects_unconsumed_expectation(self) -> None:
        fake = StrictDocumentProcessorFake()
        fake.expect_process(  # type: ignore[arg-type]
            object(),
            result=object(),  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(AssertionError, "未消费期望"):
            fake.assert_complete()


if __name__ == "__main__":
    unittest.main()
