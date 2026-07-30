"""独立 Translation Application 的严格 Fake。"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from io import BytesIO
from typing import BinaryIO, ContextManager, Iterator, Sequence

from app.modules.document_processing.domain import ArtifactRef
from app.modules.translation.domain import (
    RenderedTranslation,
    TranslationMode,
    TranslationRequest,
    TranslationUnit,
    split_translation_units,
)


class StrictPreparedArtifactReaderFake:
    def __init__(self) -> None:
        self._expected: deque[tuple[ArtifactRef, bytes | BaseException]] = deque()

    def expect(
        self,
        artifact: ArtifactRef,
        *,
        payload: bytes | None = None,
        error: BaseException | None = None,
    ) -> None:
        if (payload is None) == (error is None):
            raise ValueError("payload/error 必须且只能提供一个")
        self._expected.append((artifact, error if error is not None else payload))

    @contextmanager
    def open_reader(self, artifact: ArtifactRef) -> Iterator[BinaryIO]:
        if not self._expected:
            raise AssertionError("PreparedArtifactReader 收到未登记调用")
        expected_artifact, result = self._expected.popleft()
        if artifact != expected_artifact:
            raise AssertionError("PreparedArtifactReader 参数不一致")
        if isinstance(result, BaseException):
            raise result
        with BytesIO(result) as reader:
            yield reader

    def assert_complete(self) -> None:
        if self._expected:
            raise AssertionError("PreparedArtifactReader 尚有未消费期望")


class StrictTranslationEngineFake:
    engine_id = "strict-engine"
    engine_fingerprint = "strict-engine-v1"

    def __init__(self) -> None:
        self._expected: deque[
            tuple[str, str, TranslationMode, str | BaseException]
        ] = deque()

    def expect(
        self,
        text: str,
        *,
        target_language: str,
        mode: TranslationMode,
        result: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        if (result is None) == (error is None):
            raise ValueError("result/error 必须且只能提供一个")
        self._expected.append(
            (
                text,
                target_language,
                mode,
                error if error is not None else result,
            )
        )

    def translate(
        self,
        text: str,
        *,
        target_language: str,
        mode: TranslationMode,
    ) -> str:
        if not self._expected:
            raise AssertionError("TranslationEngine 收到未登记调用")
        expected = self._expected.popleft()
        if expected[:3] != (text, target_language, mode):
            raise AssertionError(
                f"TranslationEngine 参数不一致: expected={expected[:3]!r} "
                f"actual={(text, target_language, mode)!r}"
            )
        result = expected[3]
        if isinstance(result, BaseException):
            raise result
        return result

    def assert_complete(self) -> None:
        if self._expected:
            raise AssertionError("TranslationEngine 尚有未消费期望")


class StrictTranslationRendererFake:
    renderer_id = "strict-renderer"
    renderer_fingerprint = "strict-renderer-v1"

    def __init__(self) -> None:
        self._expected: deque[
            tuple[
                TranslationRequest,
                tuple[TranslationUnit, ...],
                RenderedTranslation | BaseException,
            ]
        ] = deque()

    def expect(
        self,
        request: TranslationRequest,
        units: Sequence[TranslationUnit],
        *,
        result: RenderedTranslation | None = None,
        error: BaseException | None = None,
    ) -> None:
        if (result is None) == (error is None):
            raise ValueError("result/error 必须且只能提供一个")
        self._expected.append(
            (
                request,
                tuple(units),
                error if error is not None else result,
            )
        )

    def render(
        self,
        *,
        request: TranslationRequest,
        source_text: str,
        units: Sequence[TranslationUnit],
    ) -> RenderedTranslation:
        del source_text
        if not self._expected:
            raise AssertionError("TranslationRenderer 收到未登记调用")
        expected_request, expected_units, result = self._expected.popleft()
        if request != expected_request or tuple(units) != expected_units:
            raise AssertionError("TranslationRenderer 参数不一致")
        if isinstance(result, BaseException):
            raise result
        return result

    def extract_units(
        self,
        *,
        request: TranslationRequest,
        source_text: str,
    ) -> Sequence[str]:
        del request
        return split_translation_units(source_text)

    def assert_complete(self) -> None:
        if self._expected:
            raise AssertionError("TranslationRenderer 尚有未消费期望")


__all__ = [
    "StrictPreparedArtifactReaderFake",
    "StrictTranslationEngineFake",
    "StrictTranslationRendererFake",
]
