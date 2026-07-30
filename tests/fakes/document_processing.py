"""共享文档处理用例的严格 Fake。

每个 Fake 都要求测试先登记精确调用；未登记、参数不一致或测试结束仍有未消费期望时
立即失败，避免宽松 Mock 把额外 Processor/Store/Record 副作用悄悄放过。
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from typing import Any, BinaryIO, ContextManager, Iterator

from app.modules.document_processing.domain import (
    ArtifactRef,
    DocumentProcessingRequest,
    LineageEvent,
)
from app.modules.document_processing.ports import (
    ArtifactContent,
    ArtifactPublication,
    ProcessingAcquireResult,
    ProcessingRecordSnapshot,
    ProcessorOutput,
)


class BytesArtifactContentFake:
    """不暴露路径、每次都返回新 reader 的小内容 Fake。"""

    def __init__(self, payload: bytes) -> None:
        if not isinstance(payload, bytes):
            raise TypeError("payload 必须是 bytes")
        self._payload = payload

    def open_reader(self) -> ContextManager[BinaryIO]:
        return BytesIO(self._payload)


@dataclass(frozen=True)
class _Expectation:
    arguments: tuple[Any, ...]
    result: Any = None
    error: BaseException | None = None


class _StrictQueue:
    def __init__(self, owner: str) -> None:
        self._owner = owner
        self._items: deque[_Expectation] = deque()

    def expect(
        self,
        *arguments: Any,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        if error is not None and result is not None:
            raise ValueError("result 与 error 不能同时设置")
        self._items.append(_Expectation(arguments, result, error))

    def consume(self, *arguments: Any) -> Any:
        if not self._items:
            raise AssertionError(f"{self._owner} 收到未登记调用: {arguments!r}")
        expected = self._items.popleft()
        if expected.arguments != arguments:
            raise AssertionError(
                f"{self._owner} 参数不一致: "
                f"expected={expected.arguments!r} actual={arguments!r}"
            )
        if expected.error is not None:
            raise expected.error
        return expected.result

    def assert_complete(self) -> None:
        if self._items:
            raise AssertionError(
                f"{self._owner} 尚有 {len(self._items)} 个未消费期望"
            )


class StrictDocumentProcessorFake:
    def __init__(self) -> None:
        self._process = _StrictQueue("DocumentProcessor.process")

    def expect_process(
        self,
        request: DocumentProcessingRequest,
        *,
        result: ProcessorOutput | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._process.expect(request, result=result, error=error)

    def process(self, request: DocumentProcessingRequest) -> ProcessorOutput:
        return self._process.consume(request)

    def assert_complete(self) -> None:
        self._process.assert_complete()


class StrictArtifactStoreFake:
    def __init__(self) -> None:
        self._publish = _StrictQueue("ArtifactStore.publish")
        self._verify = _StrictQueue("ArtifactStore.verify")
        self._open = _StrictQueue("ArtifactStore.open_reader")
        self._delete = _StrictQueue("ArtifactStore.delete_if_owned")

    def expect_publish(
        self,
        publication: ArtifactPublication,
        content: ArtifactContent,
        *,
        result: ArtifactRef | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._publish.expect(
            publication,
            content,
            result=result,
            error=error,
        )

    def publish(
        self,
        publication: ArtifactPublication,
        content: ArtifactContent,
    ) -> ArtifactRef:
        return self._publish.consume(publication, content)

    def expect_verify(
        self,
        artifact: ArtifactRef,
        *,
        result: bool | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._verify.expect(artifact, result=result, error=error)

    def verify(self, artifact: ArtifactRef) -> bool:
        return self._verify.consume(artifact)

    def expect_open(
        self,
        artifact: ArtifactRef,
        *,
        payload: bytes | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._open.expect(artifact, result=payload, error=error)

    @contextmanager
    def open_reader(self, artifact: ArtifactRef) -> Iterator[BinaryIO]:
        payload = self._open.consume(artifact)
        with BytesIO(payload) as reader:
            yield reader

    def expect_delete(
        self,
        artifact: ArtifactRef,
        *,
        result: bool | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._delete.expect(artifact, result=result, error=error)

    def delete_if_owned(self, artifact: ArtifactRef) -> bool:
        return self._delete.consume(artifact)

    def assert_complete(self) -> None:
        self._publish.assert_complete()
        self._verify.assert_complete()
        self._open.assert_complete()
        self._delete.assert_complete()


class StrictProcessingRecordFake:
    def __init__(self) -> None:
        self._acquire = _StrictQueue("ProcessingRecord.acquire")
        self._complete = _StrictQueue("ProcessingRecord.complete")
        self._fail = _StrictQueue("ProcessingRecord.fail")
        self._unknown = _StrictQueue("ProcessingRecord.mark_outcome_unknown")
        self._get = _StrictQueue("ProcessingRecord.get")

    def expect_acquire(
        self,
        request: DocumentProcessingRequest,
        *,
        result: ProcessingAcquireResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._acquire.expect(request, result=result, error=error)

    def acquire(
        self,
        request: DocumentProcessingRequest,
    ) -> ProcessingAcquireResult:
        return self._acquire.consume(request)

    def expect_complete(
        self,
        request: DocumentProcessingRequest,
        claim_token: str,
        artifact: ArtifactRef,
        lineage: LineageEvent,
        *,
        error: BaseException | None = None,
    ) -> None:
        self._complete.expect(
            request,
            claim_token,
            artifact,
            lineage,
            error=error,
        )

    def complete(
        self,
        request: DocumentProcessingRequest,
        *,
        claim_token: str,
        artifact: ArtifactRef,
        lineage: LineageEvent,
    ) -> None:
        self._complete.consume(request, claim_token, artifact, lineage)

    def expect_fail(
        self,
        request: DocumentProcessingRequest,
        claim_token: str,
        error_code: str,
        *,
        error: BaseException | None = None,
    ) -> None:
        self._fail.expect(
            request,
            claim_token,
            error_code,
            error=error,
        )

    def fail(
        self,
        request: DocumentProcessingRequest,
        *,
        claim_token: str,
        error_code: str,
    ) -> None:
        self._fail.consume(request, claim_token, error_code)

    def expect_unknown(
        self,
        request: DocumentProcessingRequest,
        error_code: str,
        claim_token: str | None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self._unknown.expect(
            request,
            error_code,
            claim_token,
            error=error,
        )

    def mark_outcome_unknown(
        self,
        request: DocumentProcessingRequest,
        *,
        error_code: str,
        claim_token: str | None = None,
    ) -> None:
        self._unknown.consume(request, error_code, claim_token)

    def expect_get(
        self,
        step_key: str,
        *,
        result: ProcessingRecordSnapshot | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._get.expect(step_key, result=result, error=error)

    def get(self, step_key: str) -> ProcessingRecordSnapshot | None:
        return self._get.consume(step_key)

    def assert_complete(self) -> None:
        self._acquire.assert_complete()
        self._complete.assert_complete()
        self._fail.assert_complete()
        self._unknown.assert_complete()
        self._get.assert_complete()


__all__ = [
    "BytesArtifactContentFake",
    "StrictArtifactStoreFake",
    "StrictDocumentProcessorFake",
    "StrictProcessingRecordFake",
]
