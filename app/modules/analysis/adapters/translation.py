"""文件分析全文翻译 Port 的适配器。

底层文档翻译引擎会在转换期间修改 MinerU 的共享输出目录，因此尚不能证明其可重入。
这里把互斥范围收敛为可注入的进程内协调器：任务身份不通过全局回调传递，具体
翻译调用仍由每个 ``AnalysisTranslationRequest`` 明确绑定。该机制只解决当前单实例内的
共享对象安全，不能替代分布式锁、可靠队列或多实例 fencing。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Protocol, TypeVar

from app.modules.translation.application import (
    TranslatePreparedDocument,
    build_translation_profile,
)
from app.modules.translation.domain import (
    TranslationFailurePolicy,
    TranslationMode,
    TranslationRequest,
)
from app.modules.translation.ports import (
    TranslationEnginePort,
    TranslationRendererPort,
)
from app.modules.analysis.ports.translation import (
    AnalysisTranslationOutcome,
    AnalysisTranslationPort,
    AnalysisTranslationRequest,
    AnalysisTranslationResult,
)


logger = logging.getLogger(__name__)
_TranslationValue = TypeVar("_TranslationValue")


class LegacyAnalysisTranslationService(Protocol):
    """遗留全文翻译服务所需的最小能力，避免 Adapter 反向依赖具体单例实现。"""

    def translate_document(
        self,
        file_path: str,
        target_lang: str = "Chinese",
        translate_all: int = 0,
        fast_translate: bool | None = None,
        use_minerU: bool = True,
    ) -> tuple[str, str]:
        ...


class AnalysisTranslationExecutionCoordinator:
    """协调同一进程内的翻译临界区，不能被某个请求替换或覆盖。"""

    def __init__(self) -> None:
        # 同一任务未来可能由上层组合多次翻译；RLock 可避免组合逻辑递归调用时自锁。
        self._lock = threading.RLock()

    def execute(
        self,
        *,
        task_id: str,
        operation: str,
        callback: Callable[[], _TranslationValue],
    ) -> _TranslationValue:
        """串行执行一次翻译，并只记录可审计的内部身份和等待时间。"""

        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id 必须是非空 str")
        if not isinstance(operation, str) or not operation:
            raise ValueError("operation 必须是非空 str")
        started_at = time.perf_counter()
        with self._lock:
            wait_milliseconds = round((time.perf_counter() - started_at) * 1000)
            logger.debug(
                "文件分析翻译进入串行执行区: task_id=%s operation=%s wait_ms=%d",
                task_id,
                operation,
                wait_milliseconds,
            )
            return callback()


def _failed_result(
    request: AnalysisTranslationRequest,
    error_code: str,
) -> AnalysisTranslationResult:
    """统一生成可审计失败结果，避免把异常误标记为成功空翻译。"""

    return AnalysisTranslationResult(
        execution=request.execution,
        outcome=AnalysisTranslationOutcome.FAILED,
        error_code=error_code,
    )


class SerializedAnalysisTranslationAdapter(AnalysisTranslationPort):
    """把遗留翻译服务适配为不持有任务回调的任务级 Port。"""

    def __init__(
        self,
        translation_service: LegacyAnalysisTranslationService,
        coordinator: AnalysisTranslationExecutionCoordinator,
    ) -> None:
        if not isinstance(coordinator, AnalysisTranslationExecutionCoordinator):
            raise TypeError("coordinator 必须是 AnalysisTranslationExecutionCoordinator")
        self._translation_service = translation_service
        self._coordinator = coordinator

    def translate(
        self,
        request: AnalysisTranslationRequest,
    ) -> AnalysisTranslationResult:
        """执行当前任务的全文翻译，失败以明确 Port 结果交给上层处理。"""

        if not isinstance(request, AnalysisTranslationRequest):
            raise TypeError("request 必须是 AnalysisTranslationRequest")
        return self._translate_document(request)

    def _translate_document(
        self,
        request: AnalysisTranslationRequest,
    ) -> AnalysisTranslationResult:
        """维持旧链路的单语/双语字段映射，不在 Adapter 发出进度回调。"""

        try:
            bilingual_html, monolingual_html = self._coordinator.execute(
                task_id=str(request.execution.task_id),
                operation="document",
                callback=lambda: self._translation_service.translate_document(
                    file_path=request.source_path,
                    target_lang=request.target_language,
                    translate_all=0,
                    use_minerU=True,
                ),
            )
        except Exception as exc:
            logger.warning(
                "文件分析全文翻译失败: task_id=%s file_name=%s error_type=%s",
                request.execution.task_id,
                Path(request.source_path).name,
                type(exc).__name__,
            )
            return _failed_result(request, "document_translation_failed")
        if not isinstance(bilingual_html, str) or not isinstance(monolingual_html, str):
            logger.warning(
                "文件分析全文翻译返回类型无效: task_id=%s file_name=%s",
                request.execution.task_id,
                Path(request.source_path).name,
            )
            return _failed_result(request, "document_translation_invalid_result")
        if not bilingual_html or not monolingual_html:
            logger.warning(
                "文件分析全文翻译返回空结果，按可降级失败记录: task_id=%s file_name=%s",
                request.execution.task_id,
                Path(request.source_path).name,
            )
            return _failed_result(request, "document_translation_empty_result")
        return AnalysisTranslationResult(
            execution=request.execution,
            outcome=AnalysisTranslationOutcome.SUCCEEDED,
            document_translation_one=monolingual_html,
            document_translation_two=bilingual_html,
        )


class ArtifactAnalysisTranslationAdapter(AnalysisTranslationPort):
    """Analysis 到独立 Translation 模块的生产适配器。

    该适配器只接受 prepared Artifact，并调用 Translation Application 完成全文翻译。
    注入的 Engine 与 Renderer 仅用于构建不可变执行 Profile；线程安全范围只覆盖单次
    引擎调用，不串行化 Artifact 读取、分段、Renderer 或 DocumentProcessing。
    """

    def __init__(
        self,
        *,
        document_translation: TranslatePreparedDocument,
        engine: TranslationEnginePort,
        renderer: TranslationRendererPort,
        mode_resolver: Callable[[], TranslationMode],
    ) -> None:
        if not isinstance(document_translation, TranslatePreparedDocument):
            raise TypeError(
                "document_translation 必须是 TranslatePreparedDocument"
            )
        if not isinstance(engine, TranslationEnginePort):
            raise TypeError("engine 必须实现 TranslationEnginePort")
        if not isinstance(renderer, TranslationRendererPort):
            raise TypeError("renderer 必须实现 TranslationRendererPort")
        if not callable(mode_resolver):
            raise TypeError("mode_resolver 必须可调用")
        self._document_translation = document_translation
        self._engine = engine
        self._renderer = renderer
        self._mode_resolver = mode_resolver

    def translate(
        self,
        request: AnalysisTranslationRequest,
    ) -> AnalysisTranslationResult:
        if not isinstance(request, AnalysisTranslationRequest):
            raise TypeError("request 必须是 AnalysisTranslationRequest")
        return self._translate_prepared_document(request)

    def _translate_prepared_document(
        self,
        request: AnalysisTranslationRequest,
    ) -> AnalysisTranslationResult:
        artifact = request.prepared_artifact
        if artifact is None:
            return _failed_result(
                request,
                "document_translation_artifact_missing",
            )
        try:
            profile = build_translation_profile(
                engine=self._engine,
                renderer=self._renderer,
                mode=self._resolve_mode(),
                failure_policy=TranslationFailurePolicy.PLACEHOLDER,
            )
            result = self._document_translation.execute(
                TranslationRequest(
                    task_id=request.execution.task_id,
                    prepared_artifact=artifact,
                    target_language=request.target_language,
                    item_limit=0,
                    profile=profile,
                    trace_id=(
                        f"analysis:{request.execution.task_id.value}:document"
                    ),
                )
            )
        except Exception as exc:
            logger.warning(
                "文件分析 prepared Artifact 全文翻译失败: task_id=%s "
                "artifact_id=%s error_type=%s",
                request.execution.task_id,
                artifact.artifact_id[:12],
                type(exc).__name__,
                exc_info=True,
            )
            return _failed_result(request, "document_translation_failed")
        return AnalysisTranslationResult(
            execution=request.execution,
            outcome=AnalysisTranslationOutcome.SUCCEEDED,
            document_translation_one=result.rendered.monolingual_html,
            document_translation_two=result.rendered.bilingual_html,
        )

    def _resolve_mode(self) -> TranslationMode:
        mode = self._mode_resolver()
        if not isinstance(mode, TranslationMode):
            raise TypeError("mode_resolver 必须返回 TranslationMode")
        return mode


__all__ = (
    "ArtifactAnalysisTranslationAdapter",
    "AnalysisTranslationExecutionCoordinator",
    "LegacyAnalysisTranslationService",
    "SerializedAnalysisTranslationAdapter",
)
