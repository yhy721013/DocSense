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
    AnalysisTranslationOutcome,
    AnalysisTranslationRequest,
)
from app.modules.tasks.domain import TaskId


class _BlockingTranslationService:
    """无内部锁的严格替身，用于证明 Adapter 会串行不同任务的全文翻译。"""

    def __init__(self) -> None:
        self.first_document_entered = threading.Event()
        self.second_document_entered = threading.Event()
        self.release_first_document = threading.Event()
        self._lock = threading.Lock()
        self.document_calls = 0
        self.active_calls = 0
        self.max_active_calls = 0

    def _enter(self) -> int:
        with self._lock:
            self.document_calls += 1
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
            return self.document_calls

    def _leave(self) -> None:
        with self._lock:
            self.active_calls -= 1

    def translate_document(self, **_: object) -> tuple[str, str]:
        call_number = self._enter()
        if call_number == 1:
            self.first_document_entered.set()
            if not self.release_first_document.wait(timeout=3):
                raise TimeoutError("测试未释放 Adapter 首次全文翻译阻塞器")
        else:
            self.second_document_entered.set()
        self._leave()
        return "双语 HTML", "单语 HTML"


class _EmptyTranslationService:
    """模拟供应商无异常但返回空内容。"""

    def translate_document(self, **_: object) -> tuple[str, str]:
        return "", ""


def _execution(task_suffix: str) -> AnalysisExecutionRef:
    return AnalysisExecutionRef(
        task_id=TaskId(f"analysis-translation-{task_suffix}"),
        file_name=f"{task_suffix}.txt",
        batch_id="3" * 31 + str(len(task_suffix) % 10),
        batch_sequence=1,
    )


class AnalysisTranslationAdapterIsolationTests(unittest.TestCase):
    def test_adapter_serializes_document_requests_from_different_tasks(self) -> None:
        service = _BlockingTranslationService()
        adapter = SerializedAnalysisTranslationAdapter(
            service,
            AnalysisTranslationExecutionCoordinator(),
        )
        requests = (
            AnalysisTranslationRequest(
                execution=_execution("document-one"),
                source_path="C:/analysis/document-one.pdf",
            ),
            AnalysisTranslationRequest(
                execution=_execution("document-two"),
                source_path="C:/analysis/document-two.pdf",
            ),
        )
        results: list[object] = []
        first_thread = threading.Thread(
            target=lambda: results.append(adapter.translate(requests[0]))
        )
        second_thread = threading.Thread(
            target=lambda: results.append(adapter.translate(requests[1]))
        )
        first_thread.start()
        self.assertTrue(service.first_document_entered.wait(timeout=1))
        second_thread.start()
        self.assertFalse(service.second_document_entered.wait(timeout=0.2))
        service.release_first_document.set()
        first_thread.join(timeout=3)
        second_thread.join(timeout=3)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
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
                source_path="C:/analysis/empty-document.pdf",
            )
        )

        self.assertIs(AnalysisTranslationOutcome.FAILED, document.outcome)
        self.assertEqual("document_translation_empty_result", document.error_code)


if __name__ == "__main__":
    unittest.main()
