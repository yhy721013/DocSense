"""翻译已准备 Artifact 的框架无关应用用例。"""

from __future__ import annotations

import logging
import time

from app.modules.translation.domain import (
    TranslationError,
    TranslationFailurePolicy,
    TranslationMode,
    TranslationProfile,
    TranslationRequest,
    TranslationResult,
    TranslationUnit,
    is_mostly_chinese,
)
from app.modules.translation.ports import (
    PreparedArtifactReaderPort,
    TranslationEnginePort,
    TranslationProgressPort,
    TranslationRendererPort,
)


logger = logging.getLogger(__name__)
_PROFILE_PARAMETERS = {
    "inputEncoding": "utf-8",
    "segmentationPolicy": "blank-line-v1",
    "sourceSkipPolicy": "mostly-chinese-80-v1",
}


class _NoopTranslationProgress:
    def started(self, request, *, total_items: int) -> None:
        del request, total_items

    def advanced(self, request, *, completed_items: int) -> None:
        del request, completed_items

    def completed(self, request) -> None:
        del request

    def failed(self, request, *, error_code: str) -> None:
        del request, error_code


def build_translation_profile(
    *,
    engine: TranslationEnginePort,
    renderer: TranslationRendererPort,
    mode: TranslationMode,
    failure_policy: TranslationFailurePolicy = (
        TranslationFailurePolicy.PLACEHOLDER
    ),
) -> TranslationProfile:
    """从已装配 Adapter 身份构造冻结 Profile。"""

    if not isinstance(engine, TranslationEnginePort):
        raise TypeError("engine 必须实现 TranslationEnginePort")
    if not isinstance(renderer, TranslationRendererPort):
        raise TypeError("renderer 必须实现 TranslationRendererPort")
    return TranslationProfile.create(
        engine_id=engine.engine_id,
        engine_fingerprint=engine.engine_fingerprint,
        renderer_id=renderer.renderer_id,
        renderer_fingerprint=renderer.renderer_fingerprint,
        mode=mode,
        failure_policy=failure_policy,
        parameters=_PROFILE_PARAMETERS,
    )


class TranslatePreparedDocument:
    """读取、分段、翻译和渲染 prepared Artifact。

    本用例没有 Converter、MIME 探测、OCR、MinerU、浏览器或宿主路径能力。读取和分段
    发生在引擎 Adapter 的临界区外；线程不安全引擎只需保护单次 ``translate`` 调用。
    """

    def __init__(
        self,
        *,
        reader: PreparedArtifactReaderPort,
        engine: TranslationEnginePort,
        renderer: TranslationRendererPort,
        progress: TranslationProgressPort | None = None,
    ) -> None:
        if not isinstance(reader, PreparedArtifactReaderPort):
            raise TypeError("reader 必须实现 PreparedArtifactReaderPort")
        if not isinstance(engine, TranslationEnginePort):
            raise TypeError("engine 必须实现 TranslationEnginePort")
        if not isinstance(renderer, TranslationRendererPort):
            raise TypeError("renderer 必须实现 TranslationRendererPort")
        if progress is not None and not isinstance(
            progress,
            TranslationProgressPort,
        ):
            raise TypeError("progress 必须实现 TranslationProgressPort")
        self._reader = reader
        self._engine = engine
        self._renderer = renderer
        self._progress = progress or _NoopTranslationProgress()

    def execute(self, request: TranslationRequest) -> TranslationResult:
        if not isinstance(request, TranslationRequest):
            raise TypeError("request 必须是 TranslationRequest")
        started_at = time.monotonic()
        self._validate_profile(request)
        try:
            with self._reader.open_reader(
                request.prepared_artifact
            ) as source_reader:
                payload = source_reader.read(
                    request.prepared_artifact.metadata.size_bytes + 1
                )
        except Exception as exc:
            self._progress.failed(
                request,
                error_code="translation_artifact_read_failed",
            )
            raise TranslationError(
                "translation_artifact_read_failed",
                "读取 prepared Artifact 失败",
            ) from exc
        if len(payload) != request.prepared_artifact.metadata.size_bytes:
            raise TranslationError(
                "translation_artifact_size_mismatch",
                "prepared Artifact 实际长度与元数据不一致",
            )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TranslationError(
                "translation_input_encoding_invalid",
                "prepared Artifact 不是合法 UTF-8",
            ) from exc
        try:
            source_units = tuple(
                self._renderer.extract_units(
                    request=request,
                    source_text=text,
                )
            )
        except Exception as exc:
            self._progress.failed(
                request,
                error_code="translation_source_structure_failed",
            )
            raise TranslationError(
                "translation_source_structure_failed",
                "无法安全解析 prepared Artifact 的文档结构",
            ) from exc
        if not source_units:
            raise TranslationError(
                "translation_input_empty",
                "prepared Artifact 没有可翻译文本",
            )

        process_count = (
            len(source_units)
            if request.item_limit == 0
            else min(request.item_limit, len(source_units))
        )
        self._progress.started(request, total_items=process_count)
        units: list[TranslationUnit] = []
        translated_count = 0
        failed_count = 0
        for ordinal, source_text in enumerate(source_units, start=1):
            should_translate = (
                ordinal <= process_count
                and not is_mostly_chinese(source_text)
            )
            translated_text = source_text
            translated = False
            failed = False
            if should_translate:
                try:
                    translated_text = self._engine.translate(
                        source_text,
                        target_language=request.target_language,
                        mode=request.profile.mode,
                    )
                    if not translated_text.strip():
                        raise TranslationError(
                            "translation_engine_empty_result",
                            "翻译引擎返回空文本",
                        )
                    translated = True
                    translated_count += 1
                except Exception as exc:
                    if (
                        request.profile.failure_policy
                        is TranslationFailurePolicy.FAIL_DOCUMENT
                    ):
                        self._progress.failed(
                            request,
                            error_code="translation_engine_failed",
                        )
                        raise TranslationError(
                            "translation_engine_failed",
                            "翻译引擎执行失败",
                        ) from exc
                    # 可降级失败必须保留原文可读性，不能让单语结果只剩内部异常类型。
                    # 具体失败分类通过结构化日志和 failed_count 审计，不写入公开正文。
                    translated_text = source_text
                    failed = True
                    failed_count += 1
                    logger.warning(
                        "翻译单元失败，已按冻结策略写入占位文本: "
                        "task_id=%s translation_key=%s ordinal=%d "
                        "error_type=%s",
                        request.task_id,
                        request.translation_key[:12],
                        ordinal,
                        type(exc).__name__,
                    )
            if ordinal <= process_count:
                self._progress.advanced(
                    request,
                    completed_items=ordinal,
                )
            units.append(
                TranslationUnit(
                    ordinal=ordinal,
                    source_text=source_text,
                    translated_text=translated_text,
                    translated=translated,
                    failed=failed,
                )
            )
        try:
            rendered = self._renderer.render(
                request=request,
                source_text=text,
                units=tuple(units),
            )
        except Exception as exc:
            self._progress.failed(
                request,
                error_code="translation_renderer_failed",
            )
            raise TranslationError(
                "translation_renderer_failed",
                "翻译 Renderer 执行失败",
            ) from exc
        self._progress.completed(request)
        logger.info(
            "prepared Artifact 翻译完成: task_id=%s translation_key=%s "
            "unit_count=%d translated_count=%d failed_count=%d duration_ms=%d",
            request.task_id,
            request.translation_key[:12],
            len(units),
            translated_count,
            failed_count,
            int((time.monotonic() - started_at) * 1000),
        )
        return TranslationResult(
            translation_key=request.translation_key,
            rendered=rendered,
            units=tuple(units),
            translated_count=translated_count,
            failed_count=failed_count,
        )

    def _validate_profile(self, request: TranslationRequest) -> None:
        profile = request.profile
        if (
            profile.engine_id != self._engine.engine_id
            or profile.engine_fingerprint != self._engine.engine_fingerprint
            or profile.renderer_id != self._renderer.renderer_id
            or profile.renderer_fingerprint
            != self._renderer.renderer_fingerprint
        ):
            raise TranslationError(
                "translation_profile_runtime_mismatch",
                "冻结 Translation Profile 与当前 Adapter 不一致",
            )
        parameters = profile.parameters_json
        expected = TranslationProfile.create(
            engine_id=profile.engine_id,
            engine_fingerprint=profile.engine_fingerprint,
            renderer_id=profile.renderer_id,
            renderer_fingerprint=profile.renderer_fingerprint,
            mode=profile.mode,
            failure_policy=profile.failure_policy,
            parameters=_PROFILE_PARAMETERS,
        )
        if parameters != expected.parameters_json:
            raise TranslationError(
                "translation_profile_policy_mismatch",
                "Translation Profile 纯规则策略不受支持",
            )


__all__ = [
    "TranslatePreparedDocument",
    "build_translation_profile",
]
