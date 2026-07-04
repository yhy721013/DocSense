"""文档 RAG 端口的可编程内存测试替身。

本模块不读取文件、不访问网络，也不模拟任何具体供应商字段。测试通过预设结果队列控制
每次模型调用的成功或失败，并使用正式端口 DTO 生成与生产适配器一致的可观察结果。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional, Sequence

from app.ports import (
    CleanupResult,
    DocumentRagSession,
    MAX_RAG_QUERY_ATTEMPTS,
    PreparedDocumentRef,
    RagAttempt,
    RagExecutionTrace,
    RagLifecycleEvent,
    RagOperationError,
    RagResult,
    RagSource,
)


@dataclass(frozen=True)
class FakeRagOutcome:
    """测试替身一次模型调用的预设结果。

    ``failure_stage`` 非空表示本次尝试直接失败；否则 ``text`` 必须能够形成成功结果。
    当来源不满足调用时的 ``require_sources`` 要求，测试替身会自动将本次尝试记录为
    ``sources`` 阶段失败，从而复现生产 Gateway 应遵守的后置校验。
    """

    text: Optional[str]
    sources: tuple[RagSource, ...] = ()
    raw_response: Optional[str] = None
    failure_stage: Optional[str] = None
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        """复制来源序列，防止测试代码在执行期间修改预设结果。"""
        object.__setattr__(self, "sources", tuple(self.sources))


class FakeDocumentRagSession:
    """可编程、无外部副作用的文档 RAG 会话测试实现。

    测试替身保留生产会话的关键不变量：analyse 只能调用一次、ask 必须发生在 analyse
    成功之后、每次尝试进入 trace、关闭后禁止继续调用，并且 close 不重复执行清理。
    """

    def __init__(
        self,
        *,
        context_name: str,
        context_ref: str,
        conversation_ref: str,
        analyse_outcomes: Optional[Sequence[FakeRagOutcome]] = None,
        ask_outcomes: Optional[Sequence[FakeRagOutcome]] = None,
        cleanup_error_message: str = "",
        lifecycle_events: Optional[Sequence[RagLifecycleEvent]] = None,
    ) -> None:
        """创建测试会话，并复制全部预设结果队列和清理失败配置。"""
        self._context_name = self._required_text(context_name, name="context_name")
        self._context_ref = self._required_text(context_ref, name="context_ref")
        self._conversation_ref = self._required_text(
            conversation_ref,
            name="conversation_ref",
        )
        default_outcome = FakeRagOutcome(
            text="模拟结果",
            sources=(RagSource(document_ref="document:fake", text="模拟证据"),),
        )
        self._analyse_outcomes = deque(
            [default_outcome] if analyse_outcomes is None else analyse_outcomes
        )
        self._ask_outcomes = deque(
            [default_outcome] if ask_outcomes is None else ask_outcomes
        )
        self._cleanup_error_message = str(cleanup_error_message or "")
        self._attempts: list[RagAttempt] = []
        self._lifecycle_events = list(lifecycle_events or ())
        self._analyse_started = False
        self._analyse_succeeded = False
        self._closed = False
        self._retain_document_on_close: Optional[bool] = None
        self._first_cleanup_result: Optional[CleanupResult] = None
        self._failure_stage: Optional[str] = None
        self._error_message: Optional[str] = None

    def analyse(
        self,
        file_path: str,
        prompt: str,
        *,
        require_sources: bool = True,
        max_attempts: int = 2,
    ) -> RagResult:
        """消费 analyse 预设队列，并将每次尝试写入会话轨迹。"""
        self._ensure_open()
        if self._analyse_started:
            raise self._operation_error(
                "analyse 只能调用一次",
                failure_stage="analyse_repeated",
            )
        self._required_text(file_path, name="file_path")
        self._required_text(prompt, name="prompt")
        self._validate_max_attempts(max_attempts)
        self._analyse_started = True
        result = self._execute(
            operation="analyse",
            prompt_kind="analysis",
            outcomes=self._analyse_outcomes,
            require_sources=require_sources,
            max_attempts=max_attempts,
        )
        self._analyse_succeeded = True
        return result

    def ask(
        self,
        prompt: str,
        *,
        require_sources: bool = True,
        max_attempts: int = 1,
    ) -> RagResult:
        """在 analyse 成功后消费 ask 预设队列，并记录全部尝试。"""
        self._ensure_open()
        self._required_text(prompt, name="prompt")
        self._validate_max_attempts(max_attempts)
        if not self._analyse_succeeded:
            raise self._operation_error(
                "ask 必须在 analyse 成功后调用",
                failure_stage="session_not_prepared",
            )
        return self._execute(
            operation="ask",
            prompt_kind="follow_up",
            outcomes=self._ask_outcomes,
            require_sources=require_sources,
            max_attempts=max_attempts,
        )

    @property
    def trace(self) -> RagExecutionTrace:
        """返回不会随测试替身后续执行而变化的轨迹快照。"""
        return self._trace()

    @property
    def retain_document_on_close(self) -> Optional[bool]:
        """返回首次关闭时记录的文档处置选择；尚未关闭时返回 ``None``。"""
        return self._retain_document_on_close

    def close(self, *, retain_document: bool) -> CleanupResult:
        """记录一次逻辑清理和文档处置选择，并保证后续调用保持幂等。

        Fake 不拥有真实全局文档，因此不会执行删除；保存该参数是为了让业务服务测试可以
        断言成功路径明确保留文档、失败路径明确请求补偿删除。
        """
        if not isinstance(retain_document, bool):
            raise TypeError("retain_document 必须是 bool")
        if self._closed:
            previous = self._first_cleanup_result or CleanupResult(
                success=False,
                already_closed=False,
                error_message="未知清理状态",
            )
            return CleanupResult(
                success=previous.success,
                already_closed=True,
                error_message=previous.error_message,
            )

        self._closed = True
        self._retain_document_on_close = retain_document
        result = CleanupResult(
            success=not bool(self._cleanup_error_message),
            already_closed=False,
            error_message=self._cleanup_error_message,
        )
        self._first_cleanup_result = result
        return result

    def _execute(
        self,
        *,
        operation: str,
        prompt_kind: str,
        outcomes: deque[FakeRagOutcome],
        require_sources: bool,
        max_attempts: int,
    ) -> RagResult:
        """执行有限次预设调用，并按正式端口契约生成结果或轨迹异常。"""
        self._validate_max_attempts(max_attempts)

        last_failure_stage = "outcomes_exhausted"
        last_error_message = "测试替身未配置足够的调用结果"
        for attempt_number in range(1, max_attempts + 1):
            if outcomes:
                outcome = outcomes.popleft()
            else:
                outcome = FakeRagOutcome(
                    text=None,
                    failure_stage="outcomes_exhausted",
                    error_message="测试替身未配置足够的调用结果",
                )

            failure_stage = outcome.failure_stage
            error_message = outcome.error_message
            if failure_stage is None and not str(outcome.text or "").strip():
                failure_stage = "response"
                error_message = error_message or "模型返回空文本"
            if failure_stage is None and require_sources and not outcome.sources:
                failure_stage = "sources"
                error_message = error_message or "模型回答缺少来源"

            self._attempts.append(
                RagAttempt(
                    operation=operation,
                    attempt=attempt_number,
                    prompt_kind=prompt_kind,
                    raw_response=(
                        outcome.raw_response
                        if outcome.raw_response is not None
                        else outcome.text
                    ),
                    sources=outcome.sources,
                    failure_stage=failure_stage,
                    error_message=error_message,
                )
            )
            if failure_stage is None:
                self._failure_stage = None
                self._error_message = None
                return RagResult(
                    text=str(outcome.text),
                    sources=outcome.sources,
                    prepared_document=PreparedDocumentRef(
                        document_ref=(
                            outcome.sources[0].document_ref
                            if outcome.sources
                            else "document:fake"
                        ),
                        external_location="external:fake-document",
                    ),
                    trace=self._trace(),
                )

            last_failure_stage = failure_stage
            last_error_message = error_message or "RAG 操作失败"

        self._failure_stage = last_failure_stage
        self._error_message = last_error_message
        raise RagOperationError(last_error_message, self._trace())

    def _ensure_open(self) -> None:
        """会话关闭后以可审计异常拒绝继续使用。"""
        if self._closed:
            raise self._operation_error(
                "RAG Session 已关闭",
                failure_stage="session_closed",
            )

    def _operation_error(self, message: str, *, failure_stage: str) -> RagOperationError:
        """更新会话总体失败状态，并构造携带当前轨迹的稳定异常。"""
        self._failure_stage = failure_stage
        self._error_message = message
        return RagOperationError(message, self._trace())

    def _trace(self) -> RagExecutionTrace:
        """根据当前内部状态创建独立、不可变的执行轨迹快照。"""
        return RagExecutionTrace(
            context_name=self._context_name,
            context_ref=self._context_ref,
            conversation_ref=self._conversation_ref,
            attempts=tuple(self._attempts),
            failure_stage=self._failure_stage,
            error_message=self._error_message,
            lifecycle_events=tuple(self._lifecycle_events),
        )

    @staticmethod
    def _required_text(value: str, *, name: str) -> str:
        """校验测试会话构造和调用所需的非空文本参数。"""
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return normalized

    @staticmethod
    def _validate_max_attempts(max_attempts: int) -> None:
        """使用与生产 Gateway 相同的模型查询次数硬上限。"""
        if not 1 <= max_attempts <= MAX_RAG_QUERY_ATTEMPTS:
            raise ValueError(
                f"max_attempts 必须介于 1 和 {MAX_RAG_QUERY_ATTEMPTS} 之间"
            )


class FakeDocumentRagPort:
    """可模拟成功打开和部分创建失败回滚的文档 RAG 端口替身。"""

    def __init__(
        self,
        *,
        analyse_outcomes: Optional[Sequence[FakeRagOutcome]] = None,
        ask_outcomes: Optional[Sequence[FakeRagOutcome]] = None,
        open_failure_stage: Optional[str] = None,
        rollback_error_message: str = "",
        cleanup_error_message: str = "",
    ) -> None:
        """配置后续测试会话及打开过程的失败场景。

        ``open_failure_stage`` 仅接受 ``None``、``context_create`` 或
        ``conversation_create``。第二个资源创建失败时，本替身会在内部记录回滚尝试，并
        通过异常 trace 暴露已创建引用和清理结果。
        """
        allowed_failure_stages = {None, "context_create", "conversation_create"}
        if open_failure_stage not in allowed_failure_stages:
            raise ValueError("open_failure_stage 不是受支持的阶段")
        self._analyse_outcomes = analyse_outcomes
        self._ask_outcomes = ask_outcomes
        self._open_failure_stage = open_failure_stage
        self._rollback_error_message = str(rollback_error_message or "")
        self._cleanup_error_message = str(cleanup_error_message or "")
        self._open_count = 0
        self._sessions: list[FakeDocumentRagSession] = []

    @property
    def sessions(self) -> tuple[FakeDocumentRagSession, ...]:
        """返回已经成功创建的测试会话快照。"""
        return tuple(self._sessions)

    def open_isolated_session(
        self,
        *,
        context_name: str,
        conversation_name: str,
    ) -> DocumentRagSession:
        """创建测试会话，或模拟部分成功后的内部回滚。"""
        normalized_context_name = self._required_text(context_name, name="context_name")
        self._required_text(conversation_name, name="conversation_name")
        self._open_count += 1
        lifecycle_events: list[RagLifecycleEvent] = []

        if self._open_failure_stage == "context_create":
            lifecycle_events.append(
                RagLifecycleEvent(
                    sequence_no=1,
                    operation="context_create",
                    attempt=1,
                    success=False,
                    external_ref=None,
                    failure_stage="context_create",
                    error_message="创建隔离上下文失败",
                )
            )
            trace = RagExecutionTrace(
                context_name=normalized_context_name,
                context_ref=None,
                conversation_ref=None,
                attempts=(),
                failure_stage="context_create",
                error_message="创建隔离上下文失败",
                lifecycle_events=tuple(lifecycle_events),
            )
            raise RagOperationError("创建隔离上下文失败", trace)

        context_ref = f"context:{self._open_count}"
        lifecycle_events.append(
            RagLifecycleEvent(
                sequence_no=1,
                operation="context_create",
                attempt=1,
                success=True,
                external_ref=context_ref,
                failure_stage=None,
                error_message=None,
            )
        )
        if self._open_failure_stage == "conversation_create":
            lifecycle_events.append(
                RagLifecycleEvent(
                    sequence_no=2,
                    operation="conversation_create",
                    attempt=1,
                    success=False,
                    external_ref=None,
                    failure_stage="conversation_create",
                    error_message="创建隔离对话失败",
                )
            )
            rollback_failed = bool(self._rollback_error_message)
            lifecycle_events.append(
                RagLifecycleEvent(
                    sequence_no=3,
                    operation="context_rollback",
                    attempt=1,
                    success=not rollback_failed,
                    external_ref=context_ref,
                    failure_stage="cleanup" if rollback_failed else None,
                    error_message=self._rollback_error_message or None,
                )
            )
            error_message = "创建隔离对话失败"
            if rollback_failed:
                error_message = f"{error_message}；回滚失败：{self._rollback_error_message}"
            trace = RagExecutionTrace(
                context_name=normalized_context_name,
                context_ref=context_ref,
                conversation_ref=None,
                attempts=(),
                failure_stage="conversation_create",
                error_message=error_message,
                lifecycle_events=tuple(lifecycle_events),
            )
            raise RagOperationError(error_message, trace)

        conversation_ref = f"conversation:{self._open_count}"
        lifecycle_events.append(
            RagLifecycleEvent(
                sequence_no=2,
                operation="conversation_create",
                attempt=1,
                success=True,
                external_ref=conversation_ref,
                failure_stage=None,
                error_message=None,
            )
        )
        session = FakeDocumentRagSession(
            context_name=normalized_context_name,
            context_ref=context_ref,
            conversation_ref=conversation_ref,
            analyse_outcomes=self._analyse_outcomes,
            ask_outcomes=self._ask_outcomes,
            cleanup_error_message=self._cleanup_error_message,
            lifecycle_events=lifecycle_events,
        )
        self._sessions.append(session)
        return session

    @staticmethod
    def _required_text(value: str, *, name: str) -> str:
        """校验测试端口创建隔离资源所需的非空名称。"""
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return normalized
