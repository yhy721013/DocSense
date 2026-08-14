"""报告专用的 AnythingLLM 多文档 RAG 适配器。

报告生成需要把一个请求中的全部文件按顺序上传到同一临时 Workspace，再执行一次模型
调用。该语义不同于单文档 ``DocumentRagSession``，因此这里直接编排已经稳定的原子
Document/Workspace/Thread Client，并向业务层返回供应商无关的完整 trace。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Protocol
from uuid import uuid4

from app.integrations.anythingllm.documents import (
    AnythingLLMDocumentClient,
    XlsxFolderCleanupToken,
)
from app.integrations.anythingllm.errors import (
    AnythingLLMConnectionError,
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
    AnythingLLMTimeoutError,
    AnythingLLMTransportError,
    AnythingLLMTransportClosedError,
    AnythingLLMUploadRejectedError,
)
from app.integrations.anythingllm.models import (
    AnythingLLMDocument,
    parse_xlsx_sheet_location,
)
from app.integrations.anythingllm.policies import (
    DEFAULT_EMBEDDING_ATTEMPTS,
    DEFAULT_UPLOAD_RETRIES,
    DEFAULT_UPLOAD_RETRY_BASE_DELAY_SECONDS,
    document_rag_workspace_settings,
    validate_embedding_max_attempts,
    validate_upload_max_retries,
    validate_upload_retry_base_delay,
)
from app.integrations.anythingllm.threads import AnythingLLMThreadClient
from app.integrations.anythingllm.transport import AnythingLLMTransport
from app.integrations.anythingllm.workspaces import AnythingLLMWorkspaceClient
from app.modules.report.domain.errors import (
    ReportArtifactError,
    ReportTaskPersistenceError,
)
from app.modules.report.ports import (
    CleanupReportRag,
    ReportArtifactRef,
    ReportRagAttempt,
    ReportRagCleanupRef,
    ReportRagExecutionError,
    ReportRagLifecycleEvent,
    ReportRagRequest,
    ReportRagResponse,
    ReportRagSource,
    ReportRagTrace,
)
from app.modules.tasks.domain import TaskStepCheckpoint
from app.modules.tasks.ports import TaskExecutionStopRequested
from app.ports.rag import normalize_rag_prompt
from app.services.core.config import AnythingLLMConfig


logger = logging.getLogger(__name__)

_TRANSIENT_EMBEDDING_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_CLEANUP_TOKEN_VERSION = 2
_LEGACY_CLEANUP_TOKEN_VERSION = 1


@dataclass(frozen=True)
class ReportAnythingLLMClients:
    """一次任务级 Transport 上共享的三个原子 Client 与租约退出状态。"""

    documents: AnythingLLMDocumentClient
    workspaces: AnythingLLMWorkspaceClient
    threads: AnythingLLMThreadClient
    lease_state: "_TransportLeaseState" = field(
        default_factory=lambda: _TransportLeaseState(),
        compare=False,
        repr=False,
    )


@dataclass
class _TransportLeaseState:
    """在一次调用内暂存不会覆盖主阶段异常的 Transport 关闭故障。

    Context manager 在业务异常传播期间若再次抛出 close 异常，会覆盖更有价值的上传、绑定
    或模型阶段错误。本状态只服务于调用内 trace 组装，不进入领域 DTO、数据库或跨任务
    共享对象。
    """

    close_error: Exception | None = None


class ReportAnythingLLMClientFactoryProtocol(Protocol):
    """供适配器和离线测试共同使用的任务级 Client 租约协议。"""

    def create(self) -> AbstractContextManager[ReportAnythingLLMClients]:
        ...


class AnythingLLMReportClientFactory:
    """每次 ``create`` 都创建并关闭独立 HTTP Transport 的线程安全工厂。"""

    def __init__(
        self,
        config: AnythingLLMConfig,
        *,
        upload_max_retries: int = DEFAULT_UPLOAD_RETRIES,
        upload_retry_base_delay: float = DEFAULT_UPLOAD_RETRY_BASE_DELAY_SECONDS,
        transport_factory: Callable[..., AnythingLLMTransport] = AnythingLLMTransport,
    ) -> None:
        if not isinstance(config, AnythingLLMConfig):
            raise TypeError("config 必须是 AnythingLLMConfig")
        if not callable(transport_factory):
            raise TypeError("transport_factory 必须可调用")
        self._config = config
        self._upload_max_retries = validate_upload_max_retries(upload_max_retries)
        self._upload_retry_base_delay = validate_upload_retry_base_delay(
            upload_retry_base_delay
        )
        self._transport_factory = transport_factory

    def create(self) -> AbstractContextManager[ReportAnythingLLMClients]:
        return self._create_lease()

    @contextmanager
    def _create_lease(self) -> Iterator[ReportAnythingLLMClients]:
        transport: AnythingLLMTransport | None = None
        clients: ReportAnythingLLMClients | None = None
        task_failed = False
        try:
            transport = self._transport_factory(
                base_url=self._config.base_url,
                api_key=self._config.api_key,
                timeout=self._config.timeout,
            )
            clients = ReportAnythingLLMClients(
                documents=AnythingLLMDocumentClient(
                    transport,
                    upload_max_retries=self._upload_max_retries,
                    upload_retry_base_delay=self._upload_retry_base_delay,
                ),
                workspaces=AnythingLLMWorkspaceClient(transport),
                threads=AnythingLLMThreadClient(transport),
            )
            yield clients
        except BaseException:
            task_failed = True
            raise
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception as exc:
                    if task_failed:
                        # 当前异常携带更完整的业务阶段和 trace，关闭异常不能覆盖它；通过
                        # 调用级状态把两者同时交给 Adapter 形成可持久化生命周期证据。
                        if clients is not None:
                            clients.lease_state.close_error = exc
                        logger.exception(
                            "关闭报告 AnythingLLM 任务级 Transport 失败，保留原始异常"
                        )
                    else:
                        raise


class _StageFailure(RuntimeError):
    """内部控制流异常；对外统一转换为携带 trace 的报告 RAG 错误。"""

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        external_outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.safe_message = message
        self.external_outcome_unknown = external_outcome_unknown


class _LifecycleCheckpointError(RuntimeError):
    """资源恢复检查点写入失败；必须立即停止后续外部删除。"""


@dataclass
class _ExecutionState:
    request: ReportRagRequest
    context_ref: str | None = None
    conversation_ref: str | None = None
    lifecycle_events: list[ReportRagLifecycleEvent] = field(default_factory=list)
    attempts: list[ReportRagAttempt] = field(default_factory=list)
    documents: list[AnythingLLMDocument] = field(default_factory=list)
    folder_cleanup_tokens: list[str] = field(default_factory=list)
    source_markers: dict[str, str] = field(default_factory=dict)
    operation_attempts: dict[str, int] = field(default_factory=dict)
    transport_opened: bool = False
    active_step_key: str = ""


@dataclass(frozen=True)
class _CleanupState:
    next_sequence: int
    context_ref: str | None
    conversation_ref: str | None
    document_locations: tuple[str, ...]
    folder_cleanup_tokens: tuple[str, ...] = ()


class AnythingLLMReportRagAdapter:
    """按顺序上传多文档、记录完整轨迹并延后清理外部资源。"""

    def __init__(
        self,
        client_factory: ReportAnythingLLMClientFactoryProtocol,
        *,
        artifact_path_resolver: Callable[[ReportArtifactRef], Path],
        user_id: int | None = 1,
        workspace_settings: Mapping[str, Any] | None = None,
        embedding_max_attempts: int = DEFAULT_EMBEDDING_ATTEMPTS,
    ) -> None:
        if not callable(getattr(client_factory, "create", None)):
            raise TypeError("client_factory 必须提供 create()")
        if not callable(artifact_path_resolver):
            raise TypeError("artifact_path_resolver 必须可调用")
        if user_id is not None and (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id < 1
        ):
            raise ValueError("user_id 必须是正整数或 None")
        self._client_factory = client_factory
        self._resolve_artifact_path = artifact_path_resolver
        self._user_id = user_id
        self._workspace_settings = MappingProxyType(
            dict(
                document_rag_workspace_settings()
                if workspace_settings is None
                else workspace_settings
            )
        )
        self._embedding_max_attempts = validate_embedding_max_attempts(
            embedding_max_attempts
        )

    def generate(self, request: ReportRagRequest) -> ReportRagResponse:
        """生成一次报告；任何失败都携带已发生的模型及资源轨迹。"""

        if not isinstance(request, ReportRagRequest):
            raise TypeError("request 必须是 ReportRagRequest")
        state = _ExecutionState(request=request)
        prompt = normalize_rag_prompt(request.prompt)
        prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        raw_content: str | None = None
        failure: _StageFailure | None = None
        leased_clients: ReportAnythingLLMClients | None = None

        logger.info(
            "开始报告多文档 AnythingLLM RAG: task_id=%s trace_id=%s file_count=%d",
            request.task_id,
            request.trace_id,
            len(request.ordered_source_files),
        )
        try:
            with self._client_factory.create() as clients:
                leased_clients = clients
                state.transport_opened = True
                self._record_lifecycle(state, "transport_open", success=True)
                self._begin_observed_step(
                    state,
                    "rag.session.open",
                    f"report:{request.task_id.value}:rag-session",
                )
                self._create_context(state, clients)
                self._create_conversation(state, clients)
                session_digest = self._stable_digest(
                    (state.context_ref or "", state.conversation_ref or "")
                )
                self._succeed_observed_step(
                    state,
                    "rag.session.open",
                    TaskStepCheckpoint(
                        code="rag_session_opened_v1",
                        result_ref=f"report-rag-session:v1:{session_digest}",
                        result_digest=session_digest,
                        external_ref=(
                            f"{state.context_ref or ''}:{state.conversation_ref or ''}"
                        ),
                    ),
                )
                self._upload_and_bind_documents(state, clients)
                self._begin_observed_step(
                    state,
                    "rag.generate",
                    "report:"
                    f"{request.task_id.value}:generation:{prompt_digest}:"
                    f"{self._ordered_document_digest(state)}",
                )
                raw_content = self._query(
                    state,
                    clients,
                    prompt=prompt,
                    prompt_digest=prompt_digest,
                )
        except (TaskExecutionStopRequested, ReportTaskPersistenceError):
            # Authority/持久化失败不能被包装成供应商失败，否则旧 Worker 可能继续产生副作用。
            raise
        except _StageFailure as exc:
            failure = exc
            self._record_deferred_close_failure(state, leased_clients)
        except Exception as exc:
            stage = "transport" if not state.transport_opened else "transport_close"
            message = self._safe_error(exc, fallback="AnythingLLM 任务级连接失败")
            self._record_lifecycle(
                state,
                "transport_open" if not state.transport_opened else "transport_close",
                success=False,
                failure_stage=stage,
                error_message=message,
            )
            failure = _StageFailure(stage, message)
        else:
            self._record_lifecycle(state, "transport_close", success=True)
        finally:
            if failure is not None and state.transport_opened:
                # 工厂在异常退出时也执行 close。若 close 自身失败，工厂只记录日志以保留
                # 原始阶段异常；此处表达租约已经完成退出，不宣称底层 TCP 一定优雅关闭。
                if not any(
                    event.operation == "transport_close"
                    for event in state.lifecycle_events
                ):
                    self._record_lifecycle(state, "transport_close", success=True)

        cleanup_ref = self._cleanup_ref(state)
        if failure is not None:
            trace = self._trace(
                state,
                failure_stage=failure.stage,
                error_message=failure.safe_message,
            )
            logger.error(
                "报告多文档 AnythingLLM RAG 失败: task_id=%s trace_id=%s "
                "failure_stage=%s lifecycle_count=%d attempt_count=%d",
                request.task_id,
                request.trace_id,
                failure.stage,
                len(state.lifecycle_events),
                len(state.attempts),
            )
            raise ReportRagExecutionError(
                failure.safe_message,
                trace=trace,
                cleanup_ref=cleanup_ref,
                external_outcome_unknown=failure.external_outcome_unknown,
                active_step_key=state.active_step_key,
            )

        raw_digest = hashlib.sha256((raw_content or "").encode("utf-8")).hexdigest()
        final_call_id = state.attempts[-1].call_id if state.attempts else ""
        self._succeed_observed_step(
            state,
            "rag.generate",
            TaskStepCheckpoint(
                code="rag_generated_v1",
                result_ref=f"report-rag-response:v1:{raw_digest}",
                result_digest=raw_digest,
                external_ref=final_call_id,
                observation_ref=f"report-rag-trace:{request.trace_id}",
            ),
        )

        trace = self._trace(state)
        logger.info(
            "报告多文档 AnythingLLM RAG 完成: task_id=%s trace_id=%s "
            "document_count=%d response_chars=%d source_count=%d",
            request.task_id,
            request.trace_id,
            len(state.documents),
            len(raw_content or ""),
            trace.attempts[-1].source_count or 0,
        )
        return ReportRagResponse(
            raw_content=raw_content,
            trace=trace,
            cleanup_ref=cleanup_ref,
        )

    def cleanup(
        self,
        command: CleanupReportRag,
    ) -> tuple[ReportRagLifecycleEvent, ...]:
        """使用新任务级 Transport 尽力清理线程、Workspace 和全局文档。

        每个资源独立尝试，前一个失败不会阻止后续补偿。方法返回连续事件而不抛供应商
        异常，使 Application 能把部分成功/失败原子追加到已经提交的交互审计中。
        """

        if not isinstance(command, CleanupReportRag):
            raise TypeError("command 必须是 CleanupReportRag")
        cleanup = self._decode_cleanup_ref(command.cleanup_ref)
        events: list[ReportRagLifecycleEvent] = []
        sequence = (
            cleanup.next_sequence
            if command.sequence_start is None
            else command.sequence_start
        )
        if sequence < cleanup.next_sequence:
            raise ValueError("sequence_start 不得早于 cleanup ref 的初始序号")
        # 恢复调用必须从持久化基线继续递增，不能让第二轮 DELETE 再次伪装成 attempt=1。
        operation_attempts = dict(command.attempt_baselines)

        def record(
            operation: str,
            *,
            success: bool,
            external_ref: str | None = None,
            failure_stage: str | None = None,
            error_message: str | None = None,
        ) -> None:
            nonlocal sequence
            operation_attempts[operation] = operation_attempts.get(operation, 0) + 1
            event = ReportRagLifecycleEvent(
                sequence_no=sequence,
                operation=operation,
                attempt_no=operation_attempts[operation],
                success=success,
                external_ref=external_ref,
                failure_stage=failure_stage,
                error_message=error_message,
            )
            events.append(event)
            sequence += 1
            if command.event_checkpoint is not None:
                try:
                    command.event_checkpoint(event)
                except Exception as exc:
                    logger.exception(
                        "报告清理生命周期检查点持久化失败，停止后续删除: "
                        "operation=%s sequence_no=%d",
                        operation,
                        event.sequence_no,
                    )
                    raise _LifecycleCheckpointError(
                        "报告清理生命周期检查点持久化失败"
                    ) from exc

        def touch() -> None:
            if command.heartbeat is None:
                return
            try:
                command.heartbeat()
            except Exception as exc:
                logger.exception("报告外部清理心跳续租失败，停止后续删除")
                raise _LifecycleCheckpointError("报告外部清理心跳续租失败") from exc

        try:
            touch()
            with self._client_factory.create() as clients:
                record("cleanup_transport_open", success=True)
                if cleanup.context_ref and cleanup.conversation_ref:
                    touch()
                    self._cleanup_action(
                        lambda: clients.threads.delete_thread(
                            cleanup.context_ref,
                            cleanup.conversation_ref,
                            user_id=self._user_id,
                        ),
                        record=record,
                        operation="conversation_delete",
                        external_ref=cleanup.conversation_ref,
                        failure_stage="cleanup_conversation",
                    )
                if cleanup.context_ref:
                    touch()
                    self._cleanup_action(
                        lambda: clients.workspaces.delete_workspace(
                            cleanup.context_ref,
                            user_id=self._user_id,
                        ),
                        record=record,
                        operation="context_delete",
                        external_ref=cleanup.context_ref,
                        failure_stage="cleanup_context",
                    )
                for location in reversed(cleanup.document_locations):
                    touch()
                    self._cleanup_action(
                        lambda current=location: self._delete_global_document_artifact(
                            clients.documents,
                            current,
                        ),
                        record=record,
                        operation="global_document_delete",
                        external_ref=location,
                        failure_stage="cleanup_document",
                    )
                for token_value in reversed(cleanup.folder_cleanup_tokens):
                    touch()
                    self._cleanup_action(
                        lambda current=token_value: clients.documents.delete_xlsx_folder(
                            XlsxFolderCleanupToken(current),
                            user_id=self._user_id,
                        ),
                        record=record,
                        operation="global_document_folder_delete",
                        external_ref=None,
                        failure_stage="cleanup_document",
                    )
        except _LifecycleCheckpointError:
            # 检查点/心跳是继续产生外部副作用的硬门禁。恢复服务会关闭本次租约并基于
            # 已持久化事件重试幂等 DELETE；这里不得把存储故障误记成供应商删除失败。
            raise
        except Exception as exc:
            transport_open_failed = not events
            cleanup_error = self._safe_error(
                exc,
                fallback="AnythingLLM 清理连接失败",
            )
            record(
                "cleanup_transport_open" if transport_open_failed else "cleanup_transport_close",
                success=False,
                failure_stage="cleanup_transport",
                error_message=cleanup_error,
            )
            if transport_open_failed:
                # 即使 Transport 未创建成功，也要为每个仍待清理的实体形成明确失败事件。
                # 审计层据此保持 cleanup=failed，后续恢复 Worker 才能定位完整资源集合。
                if cleanup.context_ref and cleanup.conversation_ref:
                    record(
                        "conversation_delete",
                        success=False,
                        external_ref=cleanup.conversation_ref,
                        failure_stage="cleanup_transport",
                        error_message=cleanup_error,
                    )
                if cleanup.context_ref:
                    record(
                        "context_delete",
                        success=False,
                        external_ref=cleanup.context_ref,
                        failure_stage="cleanup_transport",
                        error_message=cleanup_error,
                    )
                for location in reversed(cleanup.document_locations):
                    record(
                        "global_document_delete",
                        success=False,
                        external_ref=location,
                        failure_stage="cleanup_transport",
                        error_message=cleanup_error,
                    )
                for _token_value in reversed(cleanup.folder_cleanup_tokens):
                    record(
                        "global_document_folder_delete",
                        success=False,
                        failure_stage="cleanup_transport",
                        error_message=cleanup_error,
                    )
        else:
            record("cleanup_transport_close", success=True)

        logger.log(
            logging.WARNING if any(not event.success for event in events) else logging.INFO,
            "报告 AnythingLLM 外部资源清理完成: event_count=%d failure_count=%d",
            len(events),
            sum(not event.success for event in events),
        )
        return tuple(events)

    def _create_context(
        self,
        state: _ExecutionState,
        clients: ReportAnythingLLMClients,
    ) -> None:
        try:
            workspace = clients.workspaces.create_workspace(
                state.request.context_name,
                settings=self._workspace_settings,
                user_id=self._user_id,
            )
            context_ref = str(getattr(workspace, "slug", "") or "").strip()
            if not context_ref:
                raise AnythingLLMProtocolError("创建结果缺少 Workspace slug")
        except Exception as exc:
            if self._side_effect_outcome_may_be_unknown(exc):
                message = self._safe_error(
                    exc,
                    fallback="报告临时 Workspace 创建结果未知",
                )
                self._record_lifecycle(
                    state,
                    "context_create",
                    success=False,
                    failure_stage="context_create_outcome_unknown",
                    error_message=message,
                )
                reconciled_ref = self._reconcile_context_after_ambiguous_create(
                    state,
                    clients,
                )
                if reconciled_ref is None:
                    raise _StageFailure(
                        "context_create_outcome_unknown",
                        message,
                        external_outcome_unknown=True,
                    ) from exc
                state.context_ref = reconciled_ref
                return
            self._fail_lifecycle(
                state,
                operation="context_create",
                stage="context_create",
                error=exc,
                fallback="报告临时 Workspace 创建失败",
            )
        state.context_ref = context_ref
        self._record_lifecycle(
            state,
            "context_create",
            success=True,
            external_ref=context_ref,
        )

    def _create_conversation(
        self,
        state: _ExecutionState,
        clients: ReportAnythingLLMClients,
    ) -> None:
        if not state.context_ref:
            raise _StageFailure("context_identity", "报告 RAG 缺少 Workspace 引用")
        try:
            thread = clients.threads.create_thread(
                state.context_ref,
                state.request.conversation_name,
                user_id=self._user_id,
            )
            conversation_ref = str(getattr(thread, "slug", "") or "").strip()
            if not conversation_ref:
                raise AnythingLLMProtocolError("创建结果缺少 Thread slug")
        except Exception as exc:
            self._fail_lifecycle(
                state,
                operation="conversation_create",
                stage="conversation_create",
                error=exc,
                fallback="报告临时会话创建失败",
            )
        state.conversation_ref = conversation_ref
        self._record_lifecycle(
            state,
            "conversation_create",
            success=True,
            external_ref=conversation_ref,
        )

    def _upload_and_bind_documents(
        self,
        state: _ExecutionState,
        clients: ReportAnythingLLMClients,
    ) -> None:
        if not state.context_ref:
            raise _StageFailure("context_identity", "报告 RAG 缺少 Workspace 引用")
        for artifact_sequence, artifact in enumerate(
            state.request.ordered_source_files,
            start=1,
        ):
            try:
                path = self._resolve_artifact_path(artifact)
                if not isinstance(path, Path):
                    path = Path(path)
                if not path.is_file():
                    raise FileNotFoundError("RAG 输入 Artifact 文件不存在")
            except Exception as exc:
                self._fail_lifecycle(
                    state,
                    operation="artifact_resolve",
                    stage="artifact_resolve",
                    error=exc,
                    fallback="报告 RAG 输入文件解析失败",
                    external_ref=artifact.artifact_id,
                )

            source_marker = f"docsense_ref:{uuid4().hex}"
            artifact_digest = artifact.checksum.strip().lower()
            if len(artifact_digest) != 64:
                raise _StageFailure(
                    "artifact_identity",
                    "报告 RAG 输入 Artifact 缺少 SHA-256 身份",
                )
            upload_step_key = f"rag.document.upload:{artifact_sequence}"
            self._begin_observed_step(
                state,
                upload_step_key,
                "report:"
                f"{state.request.task_id.value}:rag-upload:{artifact_sequence}:"
                f"{artifact_digest}",
            )
            try:
                document = clients.documents.upload_document(
                    str(path),
                    user_id=self._user_id,
                    metadata={"docSource": source_marker},
                )
                location = str(getattr(document, "location", "") or "").strip()
                document_ref = str(
                    getattr(document, "document_ref", "") or ""
                ).strip()
                if not location or not document_ref:
                    raise AnythingLLMProtocolError(
                        "上传结果缺少文档 location 或 document_ref"
                    )
            except AnythingLLMUploadRejectedError as exc:
                message = self._safe_error(
                    exc,
                    fallback="报告文档上传不符合单 Sheet 协议",
                )
                self._record_lifecycle(
                    state,
                    "document_upload",
                    success=False,
                    failure_stage="document_upload",
                    error_message=message,
                )
                if exc.cleanup_attempted:
                    self._record_lifecycle(
                        state,
                        "global_document_folder_delete",
                        success=exc.cleanup_confirmed,
                        failure_stage=(
                            None
                            if exc.cleanup_confirmed
                            else "document_upload_cleanup_outcome_unknown"
                        ),
                        error_message=(
                            None
                            if exc.cleanup_confirmed
                            else "XLSX 多 Sheet 上传清理结果未确认"
                        ),
                    )
                if (
                    exc.folder_cleanup_token
                    and exc.cleanup_attempted
                    and not exc.cleanup_confirmed
                ):
                    state.folder_cleanup_tokens.append(exc.folder_cleanup_token)
                raise _StageFailure(
                    (
                        "document_upload"
                        if exc.cleanup_confirmed
                        else "document_upload_cleanup_outcome_unknown"
                    ),
                    message,
                    external_outcome_unknown=not exc.cleanup_confirmed,
                ) from exc
            except Exception as exc:
                self._fail_lifecycle(
                    state,
                    operation="document_upload",
                    stage="document_upload",
                    error=exc,
                    fallback="报告文档上传失败",
                    external_outcome_unknown=self._side_effect_outcome_may_be_unknown(
                        exc
                    ),
                )
            state.documents.append(document)
            state.source_markers[source_marker] = document_ref
            self._record_lifecycle(
                state,
                "document_upload",
                success=True,
                external_ref=location,
            )
            upload_digest = self._stable_digest((location, document_ref))
            self._succeed_observed_step(
                state,
                upload_step_key,
                TaskStepCheckpoint(
                    code="rag_document_uploaded_v1",
                    result_ref=document_ref,
                    result_digest=upload_digest,
                    external_ref=location,
                ),
            )
            bind_step_key = f"rag.document.bind:{artifact_sequence}"
            self._begin_observed_step(
                state,
                bind_step_key,
                "report:"
                f"{state.request.task_id.value}:rag-bind:{state.context_ref}:"
                f"{artifact_sequence}:{location}",
            )
            self._bind_document(state, clients, location)
            bind_digest = self._stable_digest((state.context_ref or "", location))
            self._succeed_observed_step(
                state,
                bind_step_key,
                TaskStepCheckpoint(
                    code="rag_document_bound_v1",
                    result_ref=f"report-rag-bind:v1:{bind_digest}",
                    result_digest=bind_digest,
                    external_ref=location,
                ),
            )

    def _bind_document(
        self,
        state: _ExecutionState,
        clients: ReportAnythingLLMClients,
        location: str,
    ) -> None:
        assert state.context_ref is not None
        for local_attempt in range(1, self._embedding_max_attempts + 1):
            try:
                workspace = clients.workspaces.update_embeddings(
                    state.context_ref,
                    adds=(location,),
                    user_id=self._user_id,
                )
                returned_ref = str(getattr(workspace, "slug", "") or "").strip()
                if not returned_ref or returned_ref.casefold() != state.context_ref.casefold():
                    raise AnythingLLMProtocolError(
                        "更新嵌入结果的 Workspace 与目标不一致"
                    )
            except AnythingLLMHTTPError as exc:
                message = self._safe_error(exc, fallback="报告文档加入 Workspace 失败")
                retryable = (
                    exc.status_code in _TRANSIENT_EMBEDDING_STATUS_CODES
                    and local_attempt < self._embedding_max_attempts
                )
                self._record_lifecycle(
                    state,
                    "document_bind",
                    success=False,
                    external_ref=location,
                    failure_stage="document_bind",
                    error_message=message,
                )
                if retryable:
                    logger.warning(
                        "报告文档加入 Workspace 暂时失败，准备有限重试: "
                        "task_id=%s attempt=%d/%d status_code=%s",
                        state.request.task_id,
                        local_attempt,
                        self._embedding_max_attempts,
                        exc.status_code,
                    )
                    continue
                raise _StageFailure("document_bind", message) from exc
            except Exception as exc:
                self._fail_lifecycle(
                    state,
                    operation="document_bind",
                    stage="document_bind",
                    error=exc,
                    fallback="报告文档加入 Workspace 失败",
                    external_ref=location,
                )
            self._record_lifecycle(
                state,
                "document_bind",
                success=True,
                external_ref=location,
            )
            return
        raise AssertionError("文档绑定重试循环异常结束")

    def _query(
        self,
        state: _ExecutionState,
        clients: ReportAnythingLLMClients,
        *,
        prompt: str,
        prompt_digest: str,
    ) -> str | None:
        if not state.context_ref or not state.conversation_ref:
            raise _StageFailure("conversation_identity", "报告 RAG 会话引用不完整")
        call_id = f"report-call-{uuid4().hex}"
        document_ids = tuple(document.id for document in state.documents)
        try:
            answer = clients.threads.ask(
                state.context_ref,
                state.conversation_ref,
                prompt,
                mode="query",
                user_id=self._user_id,
                document_ids=document_ids,
            )
            answer_text = getattr(answer, "text", None)
            if answer_text is not None and not isinstance(answer_text, str):
                raise AnythingLLMProtocolError("报告模型回答 text 必须是字符串或空值")
            raw_sources = tuple(getattr(answer, "sources", ()) or ())
            (
                sources,
                missing_marker_count,
                mismatched_marker_count,
            ) = self._adapt_sources(raw_sources, marker_map=state.source_markers)
            source_count = len(raw_sources)
            state.attempts.append(
                ReportRagAttempt(
                    sequence_no=len(state.attempts) + 1,
                    operation="report_generation",
                    attempt_no=1,
                    prompt_kind="report_generation",
                    prompt_digest=prompt_digest,
                    raw_response=answer_text if answer_text is not None else "",
                    sources=sources,
                    query_mode="query",
                    source_count=source_count,
                    verified_source_count=len(sources),
                    missing_marker_count=missing_marker_count,
                    mismatched_marker_count=mismatched_marker_count,
                    call_id=call_id,
                )
            )
            logger.log(
                logging.INFO
                if source_count == len(sources)
                else logging.WARNING,
                "报告 RAG 来源校验完成: task_id=%s source_count=%d "
                "verified_count=%d missing_marker_count=%d mismatched_marker_count=%d",
                state.request.task_id,
                source_count,
                len(sources),
                missing_marker_count,
                mismatched_marker_count,
            )
            return answer_text
        except Exception as exc:
            if isinstance(exc, _StageFailure):
                raise
            message = self._safe_error(exc, fallback="报告模型调用失败")
            state.attempts.append(
                ReportRagAttempt(
                    sequence_no=len(state.attempts) + 1,
                    operation="report_generation",
                    attempt_no=1,
                    prompt_kind="report_generation",
                    prompt_digest=prompt_digest,
                    raw_response=None,
                    sources=(),
                    failure_stage="model_query",
                    error_message=message,
                    query_mode="query",
                    source_count=0,
                    verified_source_count=0,
                    call_id=call_id,
                )
            )
            raise _StageFailure("model_query", message) from exc

    @staticmethod
    def _adapt_sources(
        raw_sources: tuple[object, ...],
        *,
        marker_map: Mapping[str, str],
    ) -> tuple[tuple[ReportRagSource, ...], int, int]:
        verified: list[ReportRagSource] = []
        missing = 0
        mismatched = 0
        for source in raw_sources:
            marker = str(getattr(source, "source_marker", "") or "").strip()
            if not marker:
                missing += 1
                continue
            document_ref = marker_map.get(marker)
            if not document_ref:
                mismatched += 1
                continue
            verified.append(
                ReportRagSource(
                    document_ref=document_ref,
                    text=str(getattr(source, "text", "") or ""),
                    source_id=(
                        str(getattr(source, "id", "") or "").strip() or None
                    ),
                    title=(
                        str(getattr(source, "title", "") or "").strip() or None
                    ),
                    url=(str(getattr(source, "url", "") or "").strip() or None),
                    score=getattr(source, "score", None),
                )
            )
        return tuple(verified), missing, mismatched

    def _trace(
        self,
        state: _ExecutionState,
        *,
        failure_stage: str | None = None,
        error_message: str | None = None,
    ) -> ReportRagTrace:
        final_call_id = state.attempts[-1].call_id if state.attempts else ""
        summary_payload = {
            "trace_id": state.request.trace_id,
            "context_ref": state.context_ref or "",
            "conversation_ref": state.conversation_ref or "",
            "attempt_count": len(state.attempts),
            "lifecycle_count": len(state.lifecycle_events),
            "document_count": len(state.documents),
            "failure_stage": failure_stage or "",
        }
        summary = json.dumps(
            summary_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return ReportRagTrace(
            trace_id=state.request.trace_id,
            context_name=state.request.context_name,
            context_ref=state.context_ref,
            conversation_ref=state.conversation_ref,
            attempts=tuple(state.attempts),
            lifecycle_events=tuple(state.lifecycle_events),
            failure_stage=failure_stage,
            error_message=error_message,
            final_call_id=final_call_id,
            summary=summary,
        )

    def _cleanup_ref(self, state: _ExecutionState) -> ReportRagCleanupRef | None:
        locations = tuple(
            str(getattr(document, "location", "") or "").strip()
            for document in state.documents
            if str(getattr(document, "location", "") or "").strip()
        )
        folder_cleanup_tokens = tuple(state.folder_cleanup_tokens)
        if (
            not state.context_ref
            and not state.conversation_ref
            and not locations
            and not folder_cleanup_tokens
        ):
            return None
        payload = {
            "version": _CLEANUP_TOKEN_VERSION,
            "next_sequence": len(state.lifecycle_events) + 1,
            "context_ref": state.context_ref,
            "conversation_ref": state.conversation_ref,
            "document_locations": list(locations),
            "folder_cleanup_tokens": list(folder_cleanup_tokens),
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        return ReportRagCleanupRef(f"v{_CLEANUP_TOKEN_VERSION}.{encoded}")

    @staticmethod
    def _decode_cleanup_ref(cleanup_ref: ReportRagCleanupRef) -> _CleanupState:
        version: int | None = None
        prefix = ""
        for candidate in (
            _CLEANUP_TOKEN_VERSION,
            _LEGACY_CLEANUP_TOKEN_VERSION,
        ):
            candidate_prefix = f"v{candidate}."
            if cleanup_ref.value.startswith(candidate_prefix):
                version = candidate
                prefix = candidate_prefix
                break
        if version is None:
            raise ValueError("不支持的报告 RAG cleanup reference 版本")
        try:
            raw = base64.b64decode(
                cleanup_ref.value[len(prefix) :].encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("报告 RAG cleanup reference 无法解码") from exc
        expected_keys = {
            "version",
            "next_sequence",
            "context_ref",
            "conversation_ref",
            "document_locations",
        }
        if version == _CLEANUP_TOKEN_VERSION:
            expected_keys.add("folder_cleanup_tokens")
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise ValueError("报告 RAG cleanup reference 结构无效")
        if payload["version"] != version:
            raise ValueError("报告 RAG cleanup reference 版本冲突")
        next_sequence = payload["next_sequence"]
        if (
            isinstance(next_sequence, bool)
            or not isinstance(next_sequence, int)
            or next_sequence < 1
        ):
            raise ValueError("报告 RAG cleanup reference 序号无效")

        def optional_text(value: object, name: str) -> str | None:
            if value is None:
                return None
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"报告 RAG cleanup reference {name} 无效")
            return value.strip()

        locations = payload["document_locations"]
        if (
            not isinstance(locations, list)
            or any(not isinstance(item, str) or not item.strip() for item in locations)
        ):
            raise ValueError("报告 RAG cleanup reference 文档集合无效")
        normalized_locations = tuple(item.strip() for item in locations)
        if len(set(normalized_locations)) != len(normalized_locations):
            raise ValueError("报告 RAG cleanup reference 包含重复文档")
        raw_folder_tokens = (
            payload["folder_cleanup_tokens"]
            if version == _CLEANUP_TOKEN_VERSION
            else []
        )
        if (
            not isinstance(raw_folder_tokens, list)
            or any(
                not isinstance(item, str) or not item.strip()
                for item in raw_folder_tokens
            )
        ):
            raise ValueError("报告 RAG cleanup reference 文件夹集合无效")
        folder_cleanup_tokens = tuple(item.strip() for item in raw_folder_tokens)
        if len(set(folder_cleanup_tokens)) != len(folder_cleanup_tokens):
            raise ValueError("报告 RAG cleanup reference 包含重复文件夹 token")
        try:
            for token_value in folder_cleanup_tokens:
                XlsxFolderCleanupToken(token_value).parse()
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "报告 RAG cleanup reference 包含无效文件夹 token"
            ) from exc
        context_ref = optional_text(payload["context_ref"], "context_ref")
        conversation_ref = optional_text(
            payload["conversation_ref"],
            "conversation_ref",
        )
        if context_ref is None and (
            conversation_ref is not None
            or normalized_locations
            or folder_cleanup_tokens
        ):
            raise ValueError("报告 RAG cleanup reference 资源层级无效")
        return _CleanupState(
            next_sequence=next_sequence,
            context_ref=context_ref,
            conversation_ref=conversation_ref,
            document_locations=normalized_locations,
            folder_cleanup_tokens=folder_cleanup_tokens,
        )

    def _record_deferred_close_failure(
        self,
        state: _ExecutionState,
        clients: ReportAnythingLLMClients | None,
    ) -> None:
        """在不覆盖主阶段错误的前提下追加 Transport 关闭失败证据。"""

        if clients is None or clients.lease_state.close_error is None:
            return
        self._record_lifecycle(
            state,
            "transport_close",
            success=False,
            failure_stage="transport_close",
            error_message=self._safe_error(
                clients.lease_state.close_error,
                fallback="AnythingLLM 任务级连接关闭失败",
            ),
        )

    def _fail_lifecycle(
        self,
        state: _ExecutionState,
        *,
        operation: str,
        stage: str,
        error: Exception,
        fallback: str,
        external_ref: str | None = None,
        external_outcome_unknown: bool = False,
    ) -> None:
        message = self._safe_error(error, fallback=fallback)
        effective_stage = (
            f"{stage}_outcome_unknown" if external_outcome_unknown else stage
        )
        self._record_lifecycle(
            state,
            operation,
            success=False,
            external_ref=external_ref,
            failure_stage=effective_stage,
            error_message=message,
        )
        raise _StageFailure(
            effective_stage,
            message,
            external_outcome_unknown=external_outcome_unknown,
        ) from error

    def _reconcile_context_after_ambiguous_create(
        self,
        state: _ExecutionState,
        clients: ReportAnythingLLMClients,
    ) -> str | None:
        """按任务唯一名称查回可能已创建的 Workspace，避免盲目重放写请求。"""

        try:
            workspaces = tuple(
                clients.workspaces.list_workspaces(user_id=self._user_id)
            )
            matches = tuple(
                item
                for item in workspaces
                if str(getattr(item, "name", "") or "").strip()
                == state.request.context_name
            )
            if len(matches) != 1:
                raise AnythingLLMProtocolError(
                    "无法按任务唯一名称确定已创建的 Workspace"
                )
            context_ref = str(getattr(matches[0], "slug", "") or "").strip()
            if not context_ref:
                raise AnythingLLMProtocolError(
                    "查回的 Workspace 缺少 slug"
                )
        except Exception as exc:
            self._record_lifecycle(
                state,
                "context_reconcile",
                success=False,
                failure_stage="context_reconcile",
                error_message=self._safe_error(
                    exc,
                    fallback="报告临时 Workspace 查回失败",
                ),
            )
            logger.error(
                "报告 Workspace 创建结果未知且无法唯一查回，必须隔离: "
                "task_id=%s context_name_chars=%d",
                state.request.task_id,
                len(state.request.context_name),
            )
            return None

        self._record_lifecycle(
            state,
            "context_reconcile",
            success=True,
            external_ref=context_ref,
        )
        logger.warning(
            "报告 Workspace 创建响应不确定，已按唯一名称查回: task_id=%s",
            state.request.task_id,
        )
        return context_ref

    @staticmethod
    def _side_effect_outcome_may_be_unknown(error: Exception) -> bool:
        """判断写请求是否可能已在供应商侧执行，但响应未被本进程可靠接收。"""

        if isinstance(error, AnythingLLMTransportClosedError):
            return False
        if isinstance(
            error,
            (
                AnythingLLMTimeoutError,
                AnythingLLMConnectionError,
                AnythingLLMProtocolError,
            ),
        ):
            return True
        if isinstance(error, AnythingLLMHTTPError):
            status_code = error.status_code or 0
            return status_code >= 500 or status_code in {408, 425, 429}
        # 自定义 Transport 可能只抛稳定基类。对无法进一步分类的传输异常采取保守隔离；
        # 普通业务/测试 RuntimeError 不会被误判为远端副作用未知。
        return isinstance(error, AnythingLLMTransportError)

    def _delete_global_document_artifact(
        self,
        client: AnythingLLMDocumentClient,
        location: str,
    ) -> None:
        """保持普通删除合同，仅对严格 nested XLSX 切换到文件夹级清理。"""
        if parse_xlsx_sheet_location(location) is not None:
            client.delete_document_artifact(location, user_id=self._user_id)
            return
        client.delete_document(location, user_id=self._user_id)

    @staticmethod
    def _cleanup_action(
        action: Callable[[], None],
        *,
        record: Callable[..., None],
        operation: str,
        external_ref: str | None,
        failure_stage: str,
    ) -> None:
        try:
            action()
        except AnythingLLMHTTPError as exc:
            if exc.status_code == 404:
                # 删除类操作必须可重复执行：上一次调用可能已经成功删除资源，但进程在
                # 结果事件落库前崩溃。远端明确返回“不存在”即可证明目标已收敛。
                logger.info(
                    "报告外部资源已不存在，按幂等删除成功处理: "
                    "operation=%s external_ref=%s",
                    operation,
                    external_ref,
                )
                record(operation, success=True, external_ref=external_ref)
                return
            record(
                operation,
                success=False,
                external_ref=external_ref,
                failure_stage=failure_stage,
                error_message=AnythingLLMReportRagAdapter._safe_error(
                    exc,
                    fallback="AnythingLLM 外部资源清理失败",
                ),
            )
        except Exception as exc:
            record(
                operation,
                success=False,
                external_ref=external_ref,
                failure_stage=failure_stage,
                error_message=AnythingLLMReportRagAdapter._safe_error(
                    exc,
                    fallback="AnythingLLM 外部资源清理失败",
                ),
            )
        else:
            record(operation, success=True, external_ref=external_ref)

    @staticmethod
    def _stable_digest(parts: tuple[str, ...]) -> str:
        """对不含凭据的稳定引用计算摘要，避免把供应商响应正文放入 Step。"""

        canonical = json.dumps(
            list(parts),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _ordered_document_digest(cls, state: _ExecutionState) -> str:
        return cls._stable_digest(
            tuple(
                f"{document.id}:{document.location}:{document.document_ref}"
                for document in state.documents
            )
        )

    @staticmethod
    def _begin_observed_step(
        state: _ExecutionState,
        step_key: str,
        idempotency_key: str,
    ) -> None:
        """先写持久 intent，再把当前 Step 标记给失败 Trace。"""

        observer = state.request.step_observer
        if observer is not None:
            observer.begin(step_key, idempotency_key)
        state.active_step_key = step_key

    @staticmethod
    def _succeed_observed_step(
        state: _ExecutionState,
        step_key: str,
        checkpoint: TaskStepCheckpoint,
    ) -> None:
        if state.active_step_key != step_key:
            raise ReportTaskPersistenceError("报告 RAG Step 完成顺序发生漂移")
        observer = state.request.step_observer
        if observer is not None:
            observer.succeed(step_key, checkpoint)
        state.active_step_key = ""

    @staticmethod
    def _record_lifecycle(
        state: _ExecutionState,
        operation: str,
        *,
        success: bool,
        external_ref: str | None = None,
        failure_stage: str | None = None,
        error_message: str | None = None,
    ) -> None:
        state.operation_attempts[operation] = state.operation_attempts.get(operation, 0) + 1
        state.lifecycle_events.append(
            ReportRagLifecycleEvent(
                sequence_no=len(state.lifecycle_events) + 1,
                operation=operation,
                attempt_no=state.operation_attempts[operation],
                success=success,
                external_ref=external_ref,
                failure_stage=failure_stage,
                error_message=error_message,
            )
        )

    @staticmethod
    def _safe_error(error: Exception, *, fallback: str) -> str:
        """仅保留供应商传输层已经脱敏的错误，避免日志/审计泄露路径和 Prompt。"""

        if isinstance(error, (AnythingLLMTransportError, AnythingLLMProtocolError)):
            message = str(error).strip()
            return message[:500] if message else fallback
        if isinstance(error, (ReportArtifactError, FileNotFoundError)):
            return fallback
        return f"{fallback}（{type(error).__name__}）"


__all__ = [
    "AnythingLLMReportClientFactory",
    "AnythingLLMReportRagAdapter",
    "ReportAnythingLLMClients",
]
