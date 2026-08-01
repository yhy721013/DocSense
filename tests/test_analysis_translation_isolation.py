"""Analysis Translation Adapter 的任务隔离与失败分类门禁。"""

from __future__ import annotations

import threading
import unittest

from app.modules.analysis.adapters import (
    AnalysisTranslationExecutionCoordinator,
    SerializedAnalysisTranslationAdapter,
)
from app.modules.analysis.ports import (
    AnalysisExecutionRef,
    AnalysisTranslationKind,
    AnalysisTranslationOutcome,
    AnalysisTranslationRequest,
)
from app.modules.tasks.domain import TaskId


class _BlockingTranslationService:
    """无内部锁的严格替身，用于证明 Adapter 统一串行全文与摘要调用。"""

    def __init__(self) -> None:
        self.document_entered = threading.Event()
        self.summary_entered = threading.Event()
        self.release_document = threading.Event()
        self._lock = threading.Lock()
        self.active_calls = 0
        self.max_active_calls = 0

    def _enter(self) -> None:
        with self._lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)

    def _leave(self) -> None:
        with self._lock:
            self.active_calls -= 1

    def translate_document(self, **_: object) -> tuple[str, str]:
        self._enter()
        self.document_entered.set()
        if not self.release_document.wait(timeout=3):
            raise TimeoutError("测试未释放 Adapter 文档翻译阻塞器")
        self._leave()
        return "双语 HTML", "单语 HTML"

    def translate_text_only(self, text: str, **_: object) -> str:
        self._enter()
        self.summary_entered.set()
        self._leave()
        return f"译文:{text}"


class _EmptyTranslationService:
    """模拟供应商无异常但返回空内容。"""

    def translate_document(self, **_: object) -> tuple[str, str]:
        return "", ""

    def translate_text_only(self, text: str, **_: object) -> str:
        del text
        return ""


def _execution(task_suffix: str) -> AnalysisExecutionRef:
    return AnalysisExecutionRef(
        task_id=TaskId(f"analysis-translation-{task_suffix}"),
        file_name=f"{task_suffix}.txt",
        batch_id="3" * 31 + str(len(task_suffix) % 10),
        batch_sequence=1,
    )


class AnalysisTranslationAdapterIsolationTests(unittest.TestCase):
    def test_adapter_serializes_document_and_summary_requests(self) -> None:
        service = _BlockingTranslationService()
        adapter = SerializedAnalysisTranslationAdapter(
            service,
            AnalysisTranslationExecutionCoordinator(),
        )
        requests = (
            AnalysisTranslationRequest(
                execution=_execution("document"),
                kind=AnalysisTranslationKind.DOCUMENT,
                source_path="C:/analysis/document.pdf",
            ),
            AnalysisTranslationRequest(
                execution=_execution("summary"),
                kind=AnalysisTranslationKind.SUMMARY,
                text="摘要原文",
            ),
        )
        results: list[object] = []
        document_thread = threading.Thread(
            target=lambda: results.append(adapter.translate(requests[0]))
        )
        summary_thread = threading.Thread(
            target=lambda: results.append(adapter.translate(requests[1]))
        )
        document_thread.start()
        self.assertTrue(service.document_entered.wait(timeout=1))
        summary_thread.start()
        self.assertFalse(service.summary_entered.wait(timeout=0.2))
        service.release_document.set()
        document_thread.join(timeout=3)
        summary_thread.join(timeout=3)

        self.assertFalse(document_thread.is_alive())
        self.assertFalse(summary_thread.is_alive())
        self.assertEqual(1, service.max_active_calls)
        self.assertEqual(2, len(results))

    def test_adapter_classifies_empty_results_as_failures(self) -> None:
        adapter = SerializedAnalysisTranslationAdapter(
            _EmptyTranslationService(),
            AnalysisTranslationExecutionCoordinator(),
        )
        document = adapter.translate(
            AnalysisTranslationRequest(
                execution=_execution("empty-document"),
                kind=AnalysisTranslationKind.DOCUMENT,
                source_path="C:/analysis/empty-document.pdf",
            )
        )
        summary = adapter.translate(
            AnalysisTranslationRequest(
                execution=_execution("empty-summary"),
                kind=AnalysisTranslationKind.SUMMARY,
                text="摘要原文",
            )
        )

        self.assertIs(AnalysisTranslationOutcome.FAILED, document.outcome)
        self.assertEqual("document_translation_empty_result", document.error_code)
        self.assertIs(AnalysisTranslationOutcome.FAILED, summary.outcome)
        self.assertEqual("summary_translation_empty_result", summary.error_code)


if __name__ == "__main__":
    unittest.main()
