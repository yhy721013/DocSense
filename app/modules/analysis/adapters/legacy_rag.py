"""文件分析单任务 RAG Port 的遗留 Gateway 适配器。

``DocumentRagFactory`` 已经保证每次 ``create`` 建立独立 HTTP Transport。本模块进一步把
该租约限制为一个 ``AnalysisExecutionRef``，在 Application 持有的显式 SessionRef 上执行
``open -> execute -> close``。不会维护按 task_id 的进程全局字典，因此不会把旧 execution
的客户端、会话或文档误交给新任务。
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
from typing import ContextManager, Iterator

from app.modules.analysis.ports.rag import (
    AnalysisRagCloseOutcome,
    AnalysisRagCloseRequest,
    AnalysisRagCloseResult,
    AnalysisRagExecutionError,
    AnalysisRagLifecycleEvent,
    AnalysisRagLifecycleOutcome,
    AnalysisRagOperation,
    AnalysisRagPort,
    AnalysisRagPortFactory,
    AnalysisRagRequest,
    AnalysisRagResult,
    AnalysisRagSessionOpenError,
    AnalysisRagSessionOpenRequest,
    AnalysisRagSessionOpenResult,
    AnalysisRagSessionOpenStage,
    AnalysisRagSessionRef,
    AnalysisRagSource,
)
from app.modules.analysis.ports.common import AnalysisExecutionRef
from app.ports import (
    DocumentRagFactory,
    DocumentRagPort,
    DocumentRagSession,
    RagOperationError,
    RagPromptKind,
)


logger = logging.getLogger(__name__)


class LegacyAnalysisRagAdapterFactory(AnalysisRagPortFactory):
    """为每个 Analysis execution 创建独立的 RAG Adapter 与传输租约。"""

    def __init__(self, document_rag_factory: DocumentRagFactory) -> None:
        if not isinstance(document_rag_factory, DocumentRagFactory):
            raise TypeError("document_rag_factory 必须实现 DocumentRagFactory")
        self._document_rag_factory = document_rag_factory

    @contextmanager
    def create(
        self,
        execution: AnalysisExecutionRef,
    ) -> Iterator[AnalysisRagPort]:
        """返回只允许服务一个 execution 的 Adapter，退出时仅释放本地 Transport。"""

        adapter = LegacyAnalysisRagAdapter(
            execution=execution,
            document_rag_factory=self._document_rag_factory,
        )
        try:
            yield adapter
        finally:
            adapter.release_transport()


class LegacyAnalysisRagAdapter(AnalysisRagPort):
    """把旧单文档 Session 转换为显式生命周期的任务级 RAG Port。"""

    def __init__(
        self,
        *,
        execution: AnalysisExecutionRef,
        document_rag_factory: DocumentRagFactory,
    ) -> None:
        if not isinstance(document_rag_factory, DocumentRagFactory):
            raise TypeError("document_rag_factory 必须实现 DocumentRagFactory")
        self._execution = execution
        self._document_rag_factory = document_rag_factory
        self._lease: ContextManager[DocumentRagPort] | None = None
        self._gateway: DocumentRagPort | None = None
        self._native_session: DocumentRagSession | None = None
        self._session: AnalysisRagSessionRef | None = None
        self._upload_path = ""
        self._last_lifecycle_sequence = 0
        self._closed = False
        self._close_result: AnalysisRagCloseResult | None = None
        self._fresh_identity_attempted = False
        self._fresh_extraction_attempted = False

    def open_session(
        self,
        request: AnalysisRagSessionOpenRequest,
    ) -> AnalysisRagSessionOpenResult:
        """创建 Context/Conversation；首个 execute 才上传并绑定文档。"""

        self._require_execution(request.execution)
        if self._session is not None:
            raise RuntimeError("同一 Analysis RAG Adapter 只能打开一次会话")
        if self._closed:
            raise RuntimeError("已关闭的 Analysis RAG Adapter 不能再次打开")
        self._upload_path = request.upload_path
        try:
            self._lease = self._document_rag_factory.create()
            self._gateway = self._lease.__enter__()
            native_session = self._gateway.open_isolated_session(
                context_name=f"llm-file-{request.execution.task_id.value}",
                conversation_name=(
                    f"analysis-{self._safe_stem(request.execution.file_name)}"
                ),
            )
        except RagOperationError as exc:
            self._release_after_open_failure()
            raise self._open_error_from_native(request.execution, exc) from exc
        except Exception as exc:
            self._release_after_open_failure()
            logger.exception(
                "文件分析 RAG 打开会话失败: task_id=%s error_type=%s",
                request.execution.task_id,
                type(exc).__name__,
            )
            raise AnalysisRagSessionOpenError(
                "文件分析 RAG 打开会话失败",
                execution=request.execution,
                stage=AnalysisRagSessionOpenStage.CONTEXT_CREATE,
                lifecycle_events=(
                    AnalysisRagLifecycleEvent(
                        sequence_no=1,
                        operation="context_create",
                        attempt_number=1,
                        outcome=AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN,
                        error_code="rag_open_outcome_unknown",
                    ),
                ),
                outcome_unknown=True,
            ) from exc

        trace = native_session.trace
        context_ref = str(trace.context_ref or "").strip()
        conversation_ref = str(trace.conversation_ref or "").strip()
        if not context_ref or not conversation_ref:
            self._release_after_open_failure()
            logger.critical(
                "文件分析 RAG 打开调用返回后缺少会话引用，外部结果保持未知: "
                "task_id=%s context_ref_present=%s conversation_ref_present=%s",
                request.execution.task_id,
                bool(context_ref),
                bool(conversation_ref),
            )
            raise AnalysisRagSessionOpenError(
                "文件分析 RAG 打开结果缺少会话引用",
                execution=request.execution,
                stage=AnalysisRagSessionOpenStage.CONVERSATION_CREATE,
                lifecycle_events=(
                    AnalysisRagLifecycleEvent(
                        sequence_no=1,
                        operation="conversation_create",
                        attempt_number=1,
                        outcome=AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN,
                        error_code="rag_open_identity_missing",
                    ),
                ),
                outcome_unknown=True,
            )
        session = AnalysisRagSessionRef(
            execution=request.execution,
            session_ref=f"{context_ref}::{conversation_ref}",
            context_ref=context_ref,
            conversation_ref=conversation_ref,
        )
        self._native_session = native_session
        self._session = session
        events = self._new_lifecycle_events(trace)
        logger.info(
            "文件分析 RAG 会话已打开: task_id=%s lifecycle_count=%d",
            request.execution.task_id,
            len(events),
        )
        return AnalysisRagSessionOpenResult(session=session, lifecycle_events=events)

    def execute(self, request: AnalysisRagRequest) -> AnalysisRagResult:
        """执行一个显式操作；必要时创建旧 Gateway 的阶段隔离对话。"""

        self._require_session(request)
        native_session = self._require_native_session()
        attempt_count_before = len(native_session.trace.attempts)
        try:
            self._switch_conversation_if_required(
                request,
                attempt_count_before=attempt_count_before,
            )
            prompt_kind = self._prompt_kind(request.operation)
            if not self._session or not self._session.document_bound:
                native_result = native_session.analyse(
                    self._upload_path,
                    request.prompt,
                    prompt_kind=prompt_kind,
                    require_sources=True,
                    # 每个 Port execute 对应一个真实请求。JSON/领域 repair 由 Application
                    # 显式编排，避免 Gateway 的隐式重试绕过审计和阶段预算。
                    max_attempts=1,
                )
                prepared = native_result.prepared_document
                self._session = self._session.with_bound_document(
                    document_ref=prepared.document_ref,
                    document_location=prepared.external_location,
                    content_sha256=prepared.content_sha256,
                    ingested_file_name=prepared.ingested_file_name,
                )
            elif request.operation is AnalysisRagOperation.IDENTITY_RESELECT:
                native_result = native_session.ask_optional(
                    request.prompt,
                    prompt_kind=prompt_kind,
                    require_sources=True,
                    max_attempts=1,
                )
                if native_result is None:
                    raise self._execution_error_from_native(
                        request,
                        error_code="identity_reselect_query_failed",
                        attempt_count_before=attempt_count_before,
                    )
            else:
                native_result = native_session.ask(
                    request.prompt,
                    prompt_kind=prompt_kind,
                    require_sources=True,
                    max_attempts=1,
                )
        except AnalysisRagExecutionError:
            raise
        except RagOperationError as exc:
            raise self._execution_error_from_native(
                request,
                native_error=exc,
                attempt_count_before=attempt_count_before,
            ) from exc
        except Exception as exc:
            logger.exception(
                "文件分析 RAG 操作失败: task_id=%s operation=%s error_type=%s",
                request.execution.task_id,
                request.operation.value,
                type(exc).__name__,
            )
            raise self._execution_error_from_native(
                request,
                error_code="analysis_rag_execution_unknown",
                force_outcome_unknown=True,
                attempt_count_before=attempt_count_before,
            ) from exc

        try:
            assert self._session is not None
            # 先只读取未消费的生命周期，不提前推进水位。若 DTO 映射失败，下面的
            # _execution_error_from_native 仍能取得 upload/bind 等已发生事实并持久审计。
            lifecycle_events = self._events_from_trace(
                native_session.trace,
                start_after=self._last_lifecycle_sequence,
            )
            result = AnalysisRagResult(
                execution=request.execution,
                session=self._session,
                operation=request.operation,
                attempt_number=request.attempt_number,
                answer=native_result.text,
                sources=tuple(
                    self._source_from_native(item)
                    for item in native_result.sources
                ),
                lifecycle_events=lifecycle_events,
            )
            if lifecycle_events:
                self._last_lifecycle_sequence = lifecycle_events[-1].sequence_no
        except Exception as exc:
            # 查询、上传或绑定已经返回后再发现响应结构损坏，无法证明外部副作用未发生。
            # 统一转为 outcome_unknown，并携带当前已绑定 Session 供审计与 1F-6 恢复。
            logger.critical(
                "文件分析 RAG 成功响应映射失败，外部结果保持未知: "
                "task_id=%s operation=%s error_type=%s",
                request.execution.task_id,
                request.operation.value,
                type(exc).__name__,
                exc_info=True,
            )
            raise self._execution_error_from_native(
                request,
                error_code="analysis_rag_success_result_invalid",
                force_outcome_unknown=True,
                attempt_count_before=attempt_count_before,
            ) from exc
        logger.info(
            "文件分析 RAG 操作完成: task_id=%s operation=%s source_count=%d",
            request.execution.task_id,
            request.operation.value,
            len(result.sources),
        )
        return result

    def close_session(
        self,
        request: AnalysisRagCloseRequest,
    ) -> AnalysisRagCloseResult:
        """关闭外部 Session；关闭失败只返回恢复证据，不抛异常覆盖已提交终态。"""

        self._require_session(request)
        native_session = self._require_native_session()
        if self._close_result is not None:
            # close 是不可盲目重放的外部 DELETE 组合。第一次结果为 unknown 时，后续本地
            # 重复调用也没有资格把它升级成 known_not_applied；返回完全相同的结果还可让
            # 审计端按原 sequence_no 幂等去重。
            return self._close_result
        try:
            cleanup = native_session.close(retain_document=request.retain_document)
            events = self._new_lifecycle_events(native_session.trace)
        except Exception as exc:
            logger.exception(
                "文件分析 RAG 关闭发生异常，保留恢复现场: task_id=%s",
                request.execution.task_id,
            )
            events = self._new_lifecycle_events(native_session.trace)
            events = self._ensure_unknown_close_event(events)
            self._closed = True
            self._close_result = AnalysisRagCloseResult(
                execution=request.execution,
                session=request.session,
                outcome=AnalysisRagCloseOutcome.OUTCOME_UNKNOWN,
                lifecycle_events=events,
                detail_code="rag_close_outcome_unknown",
            )
            return self._close_result
        self._closed = True
        if cleanup.success:
            outcome = (
                AnalysisRagCloseOutcome.KNOWN_NOT_APPLIED
                if cleanup.already_closed
                else AnalysisRagCloseOutcome.CONFIRMED
            )
            self._close_result = AnalysisRagCloseResult(
                execution=request.execution,
                session=request.session,
                outcome=outcome,
                lifecycle_events=events,
                detail_code=("session_already_closed" if cleanup.already_closed else ""),
            )
            return self._close_result
        self._close_result = AnalysisRagCloseResult(
            execution=request.execution,
            session=request.session,
            outcome=AnalysisRagCloseOutcome.OUTCOME_UNKNOWN,
            lifecycle_events=self._ensure_unknown_close_event(events),
            detail_code="rag_close_outcome_unknown",
        )
        return self._close_result

    def release_transport(self) -> None:
        """仅退出任务级 HTTP 租约；绝不在未审计时偷偷删除外部资源。"""

        lease = self._lease
        self._lease = None
        self._gateway = None
        if lease is None:
            return
        try:
            lease.__exit__(None, None, None)
        except Exception:
            # 业务终态若已持久化，Transport close 故障只能记录恢复线索，不能反向改写
            # success/failed。这里不再次调用 RAG close，避免重复外部删除。
            logger.exception(
                "文件分析 RAG Transport 关闭失败，未重写任务终态: task_id=%s",
                getattr(getattr(self._execution, "task_id", None), "value", "-"),
            )

    def _switch_conversation_if_required(
        self,
        request: AnalysisRagRequest,
        *,
        attempt_count_before: int,
    ) -> None:
        native_session = self._require_native_session()
        if request.operation is AnalysisRagOperation.IDENTITY_RESELECT:
            if self._fresh_identity_attempted:
                raise RuntimeError("身份重选对话不能重复创建")
            self._fresh_identity_attempted = True
            ready = native_session.start_fresh_conversation(
                conversation_name=(
                    "analysis-identity-reselect-"
                    f"{self._safe_stem(request.execution.file_name)}"
                ),
                failure_is_fatal=False,
            )
            if not ready:
                raise self._execution_error_from_native(
                    request,
                    error_code="identity_reselect_conversation_unavailable",
                    attempt_count_before=attempt_count_before,
                )
            self._refresh_conversation_ref()
        elif request.operation is AnalysisRagOperation.EXTRACTION:
            if self._fresh_extraction_attempted:
                raise RuntimeError("字段抽取对话不能重复创建")
            # 首次调用就是直接抽取时，旧链路不创建第二对话；只有已经有分类或重选调用时
            # 才切换。是否存在前序模型调用由旧 Session 的 trace.attempts 判定，不能仅依据
            # 文档已绑定状态判断：首个 analyse 调用本身同样会完成文档绑定。
            if self._fresh_identity_attempted or self._has_prior_model_call():
                native_session.start_fresh_conversation(
                    conversation_name=(
                        f"analysis-extraction-{self._safe_stem(request.execution.file_name)}"
                    ),
                )
                self._refresh_conversation_ref()
            self._fresh_extraction_attempted = True

    def _has_prior_model_call(self) -> bool:
        session = self._require_native_session()
        return bool(session.trace.attempts)

    def _require_execution(self, execution: AnalysisExecutionRef) -> None:
        if not isinstance(execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if execution != self._execution:
            raise ValueError("RAG 请求 execution 不属于当前任务级 Adapter")

    def _require_session(
        self,
        request: AnalysisRagRequest | AnalysisRagCloseRequest,
    ) -> None:
        self._require_execution(request.execution)
        if self._session is None:
            raise RuntimeError("RAG 会话尚未打开")
        if request.session.execution != self._session.execution:
            raise ValueError("RAG Session execution 不一致")
        if request.session.session_ref != self._session.session_ref:
            raise ValueError("RAG SessionRef 不属于当前任务级会话")
        if request.session.document_bound and request.session != self._session:
            raise ValueError("RAG Session 文档引用与当前会话不一致")

    def _require_native_session(self) -> DocumentRagSession:
        if self._native_session is None:
            raise RuntimeError("RAG 原生会话尚未创建")
        return self._native_session

    def _open_error_from_native(
        self,
        execution: AnalysisExecutionRef,
        error: RagOperationError,
    ) -> AnalysisRagSessionOpenError:
        trace = error.trace
        stage = self._open_stage(trace.failure_stage)
        events = self._events_from_trace(trace, start_after=0)
        partial = None
        if trace.context_ref and trace.conversation_ref:
            partial = AnalysisRagSessionRef(
                execution=execution,
                session_ref=f"{trace.context_ref}::{trace.conversation_ref}",
                context_ref=trace.context_ref,
                conversation_ref=trace.conversation_ref,
            )
        unknown = any(
            item.outcome is AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN
            for item in events
        )
        return AnalysisRagSessionOpenError(
            "文件分析 RAG 打开会话失败",
            execution=execution,
            stage=stage,
            partial_session=partial,
            lifecycle_events=events,
            outcome_unknown=unknown,
        )

    def _execution_error_from_native(
        self,
        request: AnalysisRagRequest,
        *,
        native_error: RagOperationError | None = None,
        error_code: str = "",
        force_outcome_unknown: bool = False,
        attempt_count_before: int = 0,
    ) -> AnalysisRagExecutionError:
        trace = native_error.trace if native_error is not None else self._require_native_session().trace
        events = self._new_lifecycle_events(trace)
        has_unknown_event = any(
            item.outcome is AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN
            for item in events
        )
        if force_outcome_unknown and not has_unknown_event:
            events = self._ensure_unknown_close_event(events, operation="rag_operation")
            has_unknown_event = True
        unknown = has_unknown_event
        # ``trace.attempts`` 是整个 Session 的累计序列。阶段 Conversation 在真正查询前
        # 创建失败时，本次不会新增 attempt，绝不能把上一阶段的成功响应挂到当前失败操作。
        current_attempts = trace.attempts[attempt_count_before:]
        current_attempt = current_attempts[-1] if current_attempts else None
        raw_response = current_attempt.raw_response if current_attempt is not None else None
        sources = (
            tuple(self._source_from_native(item) for item in current_attempt.sources)
            if current_attempt is not None
            else ()
        )
        stable_code = error_code or self._stable_error_code(trace.failure_stage)
        return AnalysisRagExecutionError(
            "文件分析 RAG 操作失败",
            request=request,
            error_code=stable_code,
            raw_response=raw_response,
            sources=sources,
            lifecycle_events=events,
            outcome_unknown=unknown,
        )

    def _new_lifecycle_events(self, trace) -> tuple[AnalysisRagLifecycleEvent, ...]:  # type: ignore[no-untyped-def]
        events = self._events_from_trace(trace, start_after=self._last_lifecycle_sequence)
        if events:
            self._last_lifecycle_sequence = events[-1].sequence_no
        return events

    def _refresh_conversation_ref(self) -> None:
        """在 Gateway 成功创建阶段对话后同步不可变 SessionRef。"""

        session = self._session
        trace = self._require_native_session().trace
        # 共享 Gateway 的 ``trace.conversation_ref`` 按合同始终保存主 Conversation；阶段
        # 重选/抽取的活动引用只能从最新成功的 conversation_create lifecycle 中取得。
        conversation_ref = ""
        for event in reversed(trace.lifecycle_events):
            if event.operation == "conversation_create" and event.success:
                conversation_ref = str(event.external_ref or "").strip()
                if conversation_ref:
                    break
        if session is None or not conversation_ref:
            raise RuntimeError("RAG 阶段对话创建后缺少 conversation_ref")
        self._session = session.with_conversation_ref(conversation_ref)

    @staticmethod
    def _events_from_trace(trace, *, start_after: int) -> tuple[AnalysisRagLifecycleEvent, ...]:  # type: ignore[no-untyped-def]
        converted: list[AnalysisRagLifecycleEvent] = []
        for event in trace.lifecycle_events:
            if event.sequence_no <= start_after:
                continue
            stage = str(event.failure_stage or "").strip()
            outcome = (
                AnalysisRagLifecycleOutcome.SUCCEEDED
                if event.success
                else AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN
                if "outcome_unknown" in stage
                else AnalysisRagLifecycleOutcome.FAILED
            )
            converted.append(
                AnalysisRagLifecycleEvent(
                    sequence_no=event.sequence_no,
                    operation=event.operation,
                    attempt_number=event.attempt,
                    outcome=outcome,
                    external_ref=str(event.external_ref or ""),
                    error_code=("" if event.success else LegacyAnalysisRagAdapter._stable_error_code(stage)),
                )
            )
        return tuple(converted)

    def _ensure_unknown_close_event(
        self,
        events: tuple[AnalysisRagLifecycleEvent, ...],
        *,
        operation: str = "session_close",
    ) -> tuple[AnalysisRagLifecycleEvent, ...]:
        if any(item.outcome is AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN for item in events):
            return events
        sequence_no = (
            events[-1].sequence_no + 1
            if events
            else self._last_lifecycle_sequence + 1
        )
        self._last_lifecycle_sequence = sequence_no
        return events + (
            AnalysisRagLifecycleEvent(
                sequence_no=sequence_no,
                operation=operation,
                attempt_number=1,
                outcome=AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN,
                error_code="rag_side_effect_outcome_unknown",
            ),
        )

    @staticmethod
    def _source_from_native(source) -> AnalysisRagSource:  # type: ignore[no-untyped-def]
        return AnalysisRagSource(
            document_ref=source.document_ref,
            text=source.text,
            source_id=str(source.id or ""),
            title=str(source.title or ""),
            url=str(source.url or ""),
            score=source.score,
        )

    @staticmethod
    def _prompt_kind(operation: AnalysisRagOperation) -> RagPromptKind:
        mappings = {
            AnalysisRagOperation.COMBINED: RagPromptKind.ANALYSIS,
            AnalysisRagOperation.CLASSIFICATION: RagPromptKind.ARCHITECTURE_CLASSIFICATION,
            AnalysisRagOperation.CLASSIFICATION_REPAIR: RagPromptKind.ARCHITECTURE_REPAIR,
            AnalysisRagOperation.IDENTITY_RESELECT: RagPromptKind.ARCHITECTURE_RESELECT,
            AnalysisRagOperation.EXTRACTION: RagPromptKind.ANALYSIS_EXTRACTION,
            AnalysisRagOperation.EXTRACTION_REPAIR: RagPromptKind.JSON_REPAIR,
        }
        return mappings[operation]

    @staticmethod
    def _open_stage(value: object) -> AnalysisRagSessionOpenStage:
        text = str(value or "")
        if "conversation" in text:
            return AnalysisRagSessionOpenStage.CONVERSATION_CREATE
        if "document_upload" in text:
            return AnalysisRagSessionOpenStage.DOCUMENT_UPLOAD
        if "document_bind" in text:
            return AnalysisRagSessionOpenStage.DOCUMENT_BIND
        return AnalysisRagSessionOpenStage.CONTEXT_CREATE

    @staticmethod
    def _stable_error_code(value: object) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return "analysis_rag_operation_failed"
        compact = "".join(
            character if character.isalnum() or character == "_" else "_"
            for character in text
        ).strip("_")
        return compact[:120] or "analysis_rag_operation_failed"

    @staticmethod
    def _safe_stem(file_name: str) -> str:
        stem = Path(file_name).stem.strip()
        return stem or "file"

    def _release_after_open_failure(self) -> None:
        self._native_session = None
        self._session = None
        self.release_transport()


__all__ = ("LegacyAnalysisRagAdapter", "LegacyAnalysisRagAdapterFactory")
