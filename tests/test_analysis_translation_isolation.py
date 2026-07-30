"""阶段 1F-2：文件分析翻译任务隔离与串行执行的离线测试。"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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
from app.services.llm_service import analysis_service
from app.services.llm_service.translation_service import LLMTranslationService


class _BlockingDocumentTranslator:
    """仅用于验证 LLMTranslationService 临界区的无网络 DocumentTranslator 替身。"""

    def __init__(self, bilingual_path: Path, monolingual_path: Path) -> None:
        self.bilingual_path = bilingual_path
        self.monolingual_path = monolingual_path
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.active_calls = 0
        self.max_active_calls = 0
        self.calls = 0

    def convert_to_html(self, **_: object) -> tuple[str, str]:
        with self._lock:
            self.calls += 1
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
            self.entered.set()
        if not self.release.wait(timeout=3):
            raise TimeoutError("测试未释放文档翻译阻塞器")
        with self._lock:
            self.active_calls -= 1
        return str(self.bilingual_path), str(self.monolingual_path)


class _BlockingTextTranslator:
    """记录是否进入真实文本翻译调用，用于证明跨方法共用同一执行锁。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def translate_text(
        self,
        text: str,
        target_lang: str,
        *,
        fast_translate: bool,
    ) -> str:
        del target_lang, fast_translate
        with self._lock:
            self.calls += 1
            self.entered.set()
        return f"译文:{text}"


class _BlockingAdapterTranslationService:
    """无内部锁的服务替身，用来单独验证 Adapter 注入协调器的效果。"""

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


class _NoCallbackTranslationService:
    """没有 set_progress_callback 的最小替身，防止旧可变回调重新引入调用链。"""

    def translate_document(self, **_: object) -> tuple[str, str]:
        return "双语结果", "单语结果"


class _EmptyTranslationService:
    """模拟供应商无异常但返回空内容，防止 Adapter 把空结果误记为成功。"""

    def translate_document(self, **_: object) -> tuple[str, str]:
        return "", ""

    def translate_text_only(self, text: str, **_: object) -> str:
        del text
        return ""


def _execution(task_suffix: str) -> AnalysisExecutionRef:
    """生成一个与公开响应隔离的内部执行身份。"""

    return AnalysisExecutionRef(
        task_id=TaskId(f"analysis-translation-{task_suffix}"),
        file_name=f"{task_suffix}.txt",
        batch_id="3" * 31 + str(len(task_suffix) % 10),
        batch_sequence=1,
    )


class LegacyTranslationIsolationTests(unittest.TestCase):
    """验证遗留单例不再保存任务回调，并跨全文/摘要调用串行化。"""

    def test_service_serializes_document_and_text_translation(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "shared-document.pdf"
            bilingual_path = temporary_path / "bilingual.html"
            monolingual_path = temporary_path / "monolingual.html"
            source_path.write_text("source", encoding="utf-8")
            bilingual_path.write_text("双语", encoding="utf-8")
            monolingual_path.write_text("单语", encoding="utf-8")

            document_translator = _BlockingDocumentTranslator(
                bilingual_path,
                monolingual_path,
            )
            text_translator = _BlockingTextTranslator()
            service = LLMTranslationService()
            # 直接注入替身，避免惰性初始化创建模型、网络或 MinerU 依赖。
            service._translator = text_translator  # type: ignore[assignment]
            service._document_translator = document_translator  # type: ignore[assignment]
            results: list[object] = []
            errors: list[BaseException] = []

            def translate_document() -> None:
                try:
                    results.append(service.translate_document(str(source_path)))
                except BaseException as exc:  # pragma: no cover - 仅记录线程异常
                    errors.append(exc)

            def translate_summary() -> None:
                try:
                    results.append(service.translate_text_only("摘要"))
                except BaseException as exc:  # pragma: no cover - 仅记录线程异常
                    errors.append(exc)

            document_thread = threading.Thread(target=translate_document)
            summary_thread = threading.Thread(target=translate_summary)
            document_thread.start()
            self.assertTrue(document_translator.entered.wait(timeout=1))
            summary_thread.start()
            # 若没有共享执行锁，文本替身会在全文转换未结束时进入真实调用。
            self.assertFalse(text_translator.entered.wait(timeout=0.2))
            document_translator.release.set()
            document_thread.join(timeout=3)
            summary_thread.join(timeout=3)

            self.assertFalse(document_thread.is_alive())
            self.assertFalse(summary_thread.is_alive())
            self.assertFalse(errors)
            self.assertEqual(1, document_translator.max_active_calls)
            self.assertEqual(1, text_translator.calls)
            self.assertIn(("双语", "单语"), results)
            self.assertIn('<div class="translated-text">译文:摘要</div>', results)

    def test_analysis_full_translation_does_not_require_mutable_callback_setter(self) -> None:
        mapped_result = {
            "fileDataItem": {
                "originalText": "原文",
                "summary": "摘要",
            }
        }
        with patch(
            "app.services.llm_service.analysis_service.get_translation_service",
            return_value=_NoCallbackTranslationService(),
        ):
            enriched = analysis_service.enrich_with_translations(
                mapped_result,
                "C:/analysis/demo.pdf",
                enable_full_translation=True,
            )
        self.assertEqual("单语结果", enriched["fileDataItem"]["documentTranslationOne"])
        self.assertEqual("双语结果", enriched["fileDataItem"]["documentTranslationTwo"])

    def test_source_has_no_global_progress_callback_state(self) -> None:
        translation_source = Path(
            "app/services/llm_service/translation_service.py"
        ).read_text(encoding="utf-8")
        analysis_source = Path(
            "app/services/llm_service/analysis_service.py"
        ).read_text(encoding="utf-8")
        self.assertIn("self._execution_lock", translation_source)
        self.assertNotIn("set_progress_callback", translation_source)
        self.assertNotIn("_progress_callback", translation_source)
        self.assertNotIn("set_progress_callback", analysis_source)
        self.assertNotIn("translation_progress_callback", analysis_source)


class AnalysisTranslationAdapterIsolationTests(unittest.TestCase):
    """验证未来 Application 可注入同一协调器，不再依赖进度回调全局状态。"""

    def test_adapter_serializes_document_and_summary_requests(self) -> None:
        service = _BlockingAdapterTranslationService()
        adapter = SerializedAnalysisTranslationAdapter(
            service,
            AnalysisTranslationExecutionCoordinator(),
        )
        document_request = AnalysisTranslationRequest(
            execution=_execution("document"),
            kind=AnalysisTranslationKind.DOCUMENT,
            source_path="C:/analysis/document.pdf",
        )
        summary_request = AnalysisTranslationRequest(
            execution=_execution("summary"),
            kind=AnalysisTranslationKind.SUMMARY,
            text="摘要原文",
        )
        results: list[object] = []

        document_thread = threading.Thread(
            target=lambda: results.append(adapter.translate(document_request))
        )
        summary_thread = threading.Thread(
            target=lambda: results.append(adapter.translate(summary_request))
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
        self.assertTrue(
            any(
                result.outcome is AnalysisTranslationOutcome.SUCCEEDED
                and result.document_translation_one == "单语 HTML"
                for result in results
            )
        )
        self.assertTrue(
            any(
                result.outcome is AnalysisTranslationOutcome.SUCCEEDED
                and result.document_translation_two == "摘要原文\n译文:摘要原文"
                for result in results
            )
        )

    def test_adapter_classifies_empty_document_and_summary_as_failures(self) -> None:
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
