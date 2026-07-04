"""AnythingLLM 文档 RAG Gateway。

本模块是供应商原子 Client 与应用层 ``DocumentRagPort`` 之间的适配器。Gateway 只负责编排
隔离工作区、线程、上传、嵌入、Pin、查询和来源校验，不解析业务 JSON，也不判断
architecture 等领域规则。

状态机严格禁止通过工作区详情接口反查文档 ID，查询时也不发送文件列表。模型是否真正
使用目标文档，只通过原子 Client 已归一化的 ``document_ref`` 做精确来源确认。任何失败
都会转换为携带不可变执行轨迹的 ``RagOperationError``，便于上层在清理前完成审计。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.integrations.anythingllm.documents import AnythingLLMDocumentClient
from app.integrations.anythingllm.errors import (
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
    AnythingLLMTransportError,
)
from app.integrations.anythingllm.models import (
    AnythingLLMDocument,
    AnythingLLMSource,
    AnythingLLMThread,
    AnythingLLMWorkspace,
)
from app.integrations.anythingllm.threads import AnythingLLMThreadClient
from app.integrations.anythingllm.workspaces import AnythingLLMWorkspaceClient
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


logger = logging.getLogger(__name__)

_MAX_EMBEDDING_ATTEMPTS = 3
"""工作区嵌入调用次数硬上限，包含首次调用。"""


class AnythingLLMRagGateway:
    """使用三个原子 Client 创建任务级隔离 RAG 会话。

    Gateway 不拥有原子 Client 及其底层传输对象的生命周期。阶段 6 的任务级 Factory 将
    为每个后台任务创建独立 Client 组合，并在任务结束后统一关闭传输对象。本类只负责
    尚未返回 Session 时的部分创建回滚。
    """

    def __init__(
        self,
        document_client: AnythingLLMDocumentClient,
        workspace_client: AnythingLLMWorkspaceClient,
        thread_client: AnythingLLMThreadClient,
        *,
        user_id: int | None = None,
        workspace_settings: Optional[Mapping[str, Any]] = None,
        embedding_max_attempts: int = 2,
    ) -> None:
        """保存任务级依赖并固定嵌入重试上限。

        ``embedding_max_attempts`` 包含首次调用。只对标准暂态网关状态码执行重试，且不在
        Gateway 内休眠，以避免后台线程出现不可观察的固定等待。上传的指数退避仍由
        Document Client 负责。
        """
        if not 1 <= embedding_max_attempts <= _MAX_EMBEDDING_ATTEMPTS:
            raise ValueError(
                "embedding_max_attempts 必须介于 1 和 "
                f"{_MAX_EMBEDDING_ATTEMPTS} 之间"
            )
        self._document_client = document_client
        self._workspace_client = workspace_client
        self._thread_client = thread_client
        self._user_id = user_id
        self._workspace_settings = dict(workspace_settings or {})
        self._embedding_max_attempts = embedding_max_attempts

    def open_isolated_session(
        self,
        *,
        context_name: str,
        conversation_name: str,
    ) -> DocumentRagSession:
        """创建工作区和线程，并在第二步失败时立即回滚已创建工作区。

        调用方只会拿到完整可用的 Session。若工作区已经创建但线程创建失败，本方法在
        抛出异常前自行尝试删除工作区；无论回滚成功或失败，轨迹都会保留工作区引用和
        清理结果，避免要求业务层关闭一个从未成功返回的 Session。
        """
        normalized_context_name = self._required_text(context_name, name="context_name")
        normalized_conversation_name = self._required_text(
            conversation_name,
            name="conversation_name",
        )
        try:
            workspace = self._workspace_client.create_workspace(
                normalized_context_name,
                settings=self._workspace_settings or None,
                user_id=self._user_id,
            )
            context_ref = self._resource_ref(workspace, attribute="slug")
            if not context_ref:
                raise AnythingLLMProtocolError(
                    "AnythingLLM 创建工作区结果缺少有效 slug"
                )
        except Exception as exc:
            error_message = self._safe_error_message(exc, fallback="创建隔离上下文失败")
            trace = RagExecutionTrace(
                context_name=normalized_context_name,
                context_ref=None,
                conversation_ref=None,
                attempts=(),
                failure_stage="context_create",
                error_message=error_message,
                lifecycle_events=(
                    self._lifecycle_event(
                        sequence_no=1,
                        operation="context_create",
                        success=False,
                        failure_stage="context_create",
                        error_message=error_message,
                    ),
                ),
            )
            logger.error(
                "anythingllm.context.create_failed: operation=open_session "
                "context_name=%s stage=context_create error_type=%s",
                normalized_context_name,
                type(exc).__name__,
            )
            raise RagOperationError(error_message, trace) from exc

        logger.info(
            "anythingllm.context.created: operation=open_session context_ref=%s",
            context_ref,
        )
        try:
            thread = self._thread_client.create_thread(
                context_ref,
                normalized_conversation_name,
                user_id=self._user_id,
            )
            conversation_ref = self._resource_ref(thread, attribute="slug")
            if not conversation_ref:
                raise AnythingLLMProtocolError(
                    "AnythingLLM 创建线程结果缺少有效 slug"
                )
        except Exception as exc:
            raise self._rollback_open_failure(
                context_name=normalized_context_name,
                context_ref=context_ref,
                cause=exc,
            ) from exc

        logger.info(
            "anythingllm.conversation.created: operation=open_session "
            "context_ref=%s conversation_ref=%s",
            context_ref,
            conversation_ref,
        )
        lifecycle_events = (
            self._lifecycle_event(
                sequence_no=1,
                operation="context_create",
                success=True,
                external_ref=context_ref,
            ),
            self._lifecycle_event(
                sequence_no=2,
                operation="conversation_create",
                success=True,
                external_ref=conversation_ref,
            ),
        )
        return _AnythingLLMRagSession(
            document_client=self._document_client,
            workspace_client=self._workspace_client,
            thread_client=self._thread_client,
            context_name=normalized_context_name,
            workspace=workspace,
            thread=thread,
            user_id=self._user_id,
            embedding_max_attempts=self._embedding_max_attempts,
            lifecycle_events=lifecycle_events,
        )

    def _rollback_open_failure(
        self,
        *,
        context_name: str,
        context_ref: str,
        cause: Exception,
    ) -> RagOperationError:
        """回滚线程创建失败前已经创建的工作区，并构造完整失败轨迹。"""
        create_error = self._safe_error_message(cause, fallback="创建隔离对话失败")
        lifecycle_events = [
            self._lifecycle_event(
                sequence_no=1,
                operation="context_create",
                success=True,
                external_ref=context_ref,
            ),
            self._lifecycle_event(
                sequence_no=2,
                operation="conversation_create",
                success=False,
                failure_stage="conversation_create",
                error_message=create_error,
            ),
        ]
        cleanup_error = ""
        try:
            self._workspace_client.delete_workspace(
                context_ref,
                user_id=self._user_id,
            )
            lifecycle_events.append(
                self._lifecycle_event(
                    sequence_no=3,
                    operation="context_rollback",
                    success=True,
                    external_ref=context_ref,
                )
            )
            logger.info(
                "anythingllm.context.rollback_completed: operation=open_session "
                "context_ref=%s stage=conversation_create",
                context_ref,
            )
        except Exception as cleanup_exc:
            cleanup_error = self._safe_error_message(
                cleanup_exc,
                fallback="回滚隔离上下文失败",
            )
            lifecycle_events.append(
                self._lifecycle_event(
                    sequence_no=3,
                    operation="context_rollback",
                    success=False,
                    external_ref=context_ref,
                    failure_stage="cleanup",
                    error_message=cleanup_error,
                )
            )
            logger.error(
                "anythingllm.context.rollback_failed: operation=open_session "
                "context_ref=%s stage=cleanup error_type=%s",
                context_ref,
                type(cleanup_exc).__name__,
            )

        error_message = create_error
        if cleanup_error:
            error_message = f"{create_error}；回滚失败：{cleanup_error}"
        trace = RagExecutionTrace(
            context_name=context_name,
            context_ref=context_ref,
            conversation_ref=None,
            attempts=(),
            failure_stage="conversation_create",
            error_message=error_message,
            lifecycle_events=tuple(lifecycle_events),
        )
        return RagOperationError(error_message, trace)

    @staticmethod
    def _lifecycle_event(
        *,
        sequence_no: int,
        operation: str,
        success: bool,
        external_ref: Optional[str] = None,
        failure_stage: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> RagLifecycleEvent:
        """构造一次与供应商协议字段无关的资源生命周期事件。"""
        return RagLifecycleEvent(
            sequence_no=sequence_no,
            operation=operation,
            attempt=1,
            success=success,
            external_ref=external_ref,
            failure_stage=failure_stage,
            error_message=error_message,
        )

    @staticmethod
    def _resource_ref(value: object, *, attribute: str) -> str:
        """从原子 Client DTO 中读取非空资源引用，不解释其内部格式。"""
        return str(getattr(value, attribute, "") or "").strip()

    @staticmethod
    def _required_text(value: str, *, name: str) -> str:
        """规范化并校验打开隔离会话所需的非空名称。"""
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return normalized

    @staticmethod
    def _safe_error_message(error: Exception, *, fallback: str) -> str:
        """生成适合写入轨迹的有界错误信息，并避免未知异常泄露敏感正文。"""
        if isinstance(error, AnythingLLMTransportError):
            message = str(error)
        elif isinstance(error, ValueError):
            message = str(error)
        elif isinstance(error, FileNotFoundError):
            message = "待分析文件不存在或不是普通文件"
        else:
            message = f"{fallback}（{type(error).__name__}）"
        compact = " ".join(str(message or fallback).split())
        return compact[:500]


class _AnythingLLMRagSession:
    """一个文件分析任务独占的纯方案 B 会话。

    Session 在构造时已经拥有完整工作区和线程。``analyse`` 负责一次性完成上传、加入、
    Pin 和首次查询；``ask`` 只在同一线程中追加查询。调用轨迹以不可变快照返回，清理则
    只删除临时工作区并保证最多执行一次。
    """

    _TRANSIENT_EMBEDDING_STATUS_CODES = frozenset({502, 503, 504})

    def __init__(
        self,
        *,
        document_client: AnythingLLMDocumentClient,
        workspace_client: AnythingLLMWorkspaceClient,
        thread_client: AnythingLLMThreadClient,
        context_name: str,
        workspace: AnythingLLMWorkspace,
        thread: AnythingLLMThread,
        user_id: int | None,
        embedding_max_attempts: int,
        lifecycle_events: Sequence[RagLifecycleEvent],
    ) -> None:
        """校验完整资源状态后保存任务级原子 Client，不执行外部操作。"""
        normalized_context_name = str(context_name or "").strip()
        context_ref = str(getattr(workspace, "slug", "") or "").strip()
        conversation_ref = str(getattr(thread, "slug", "") or "").strip()
        if not normalized_context_name:
            raise ValueError("context_name 不能为空")
        if not context_ref:
            raise ValueError("workspace.slug 不能为空")
        if not conversation_ref:
            raise ValueError("thread.slug 不能为空")
        if not 1 <= embedding_max_attempts <= _MAX_EMBEDDING_ATTEMPTS:
            raise ValueError(
                "embedding_max_attempts 必须介于 1 和 "
                f"{_MAX_EMBEDDING_ATTEMPTS} 之间"
            )
        self._document_client = document_client
        self._workspace_client = workspace_client
        self._thread_client = thread_client
        self._context_name = normalized_context_name
        self._context_ref = context_ref
        self._conversation_ref = conversation_ref
        self._user_id = user_id
        self._embedding_max_attempts = embedding_max_attempts
        self._attempts: list[RagAttempt] = []
        self._lifecycle_events = list(lifecycle_events)
        self._document_ref: Optional[str] = None
        self._uploaded_document: Optional[AnythingLLMDocument] = None
        self._global_document_cleanup_required = False
        self._analyse_started = False
        self._analyse_succeeded = False
        self._closed = False
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
        """一次性准备目标文档，并在同一线程内有限重试首次查询。

        查询失败重试只会再次调用线程问答，不会重新上传、嵌入或 Pin。这样可以避免模型
        短暂空响应或 sources 延迟导致重复文档和重复向量写入。
        """
        self._ensure_open()
        if self._analyse_started:
            raise self._operation_error(
                "analyse 只能调用一次",
                failure_stage="analyse_repeated",
            )
        normalized_file_path = self._required_text(file_path, name="file_path")
        normalized_prompt = self._required_text(prompt, name="prompt")
        self._validate_max_attempts(max_attempts)
        self._analyse_started = True

        document: Optional[AnythingLLMDocument] = None
        try:
            document = self._upload_document(normalized_file_path)
            self._uploaded_document = document
            self._bind_document(document.location)
            self._pin_document(document.location)
            self._document_ref = document.document_ref
            result = self._query(
                prompt=normalized_prompt,
                operation="analyse",
                prompt_kind="analysis",
                require_sources=require_sources,
                max_attempts=max_attempts,
            )
        except RagOperationError:
            if document is not None:
                self._schedule_failed_document_cleanup(document)
            raise
        except Exception:
            if document is not None:
                self._schedule_failed_document_cleanup(document)
            raise

        self._analyse_succeeded = True
        return result

    def ask(
        self,
        prompt: str,
        *,
        require_sources: bool = True,
        max_attempts: int = 1,
    ) -> RagResult:
        """在已准备的线程中继续查询，不重复任何文档准备操作。"""
        self._ensure_open()
        normalized_prompt = self._required_text(prompt, name="prompt")
        self._validate_max_attempts(max_attempts)
        if not self._analyse_succeeded or not self._document_ref:
            raise self._operation_error(
                "ask 必须在 analyse 成功后调用",
                failure_stage="session_not_prepared",
            )
        try:
            return self._query(
                prompt=normalized_prompt,
                operation="ask",
                prompt_kind="follow_up",
                require_sources=require_sources,
                max_attempts=max_attempts,
            )
        except RagOperationError:
            if self._uploaded_document is not None:
                self._schedule_failed_document_cleanup(self._uploaded_document)
            raise
        except Exception:
            if self._uploaded_document is not None:
                self._schedule_failed_document_cleanup(self._uploaded_document)
            raise

    @property
    def trace(self) -> RagExecutionTrace:
        """返回截至当前时刻的独立、不可变执行轨迹快照。"""
        return self._trace()

    def close(self, *, retain_document: bool) -> CleanupResult:
        """最多执行一次资源清理，并稳定复用首次清理结果。

        ``_closed`` 在发起删除前设置，确保删除请求超时或失败后，重复调用也不会盲目重放
        具有外部副作用的请求。只有调用方明确传入 ``retain_document=True`` 且 Session
        内部未发生失败时，才保留全局文档供后续永久知识库和武器装备解析复用。RAG 内部
        失败会强制清理文档，即使调用方误传 ``True`` 也不能覆盖该安全状态。

        按审计硬前置契约，上层只能在交互审计成功后调用本方法。审计失败时不得 close，
        从而同时保留 Workspace、Conversation 和待补偿全局文档，供恢复与人工核查。
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
        cleanup_errors: list[str] = []
        should_delete_global_document = (
            self._uploaded_document is not None
            and (self._global_document_cleanup_required or not retain_document)
        )
        if should_delete_global_document:
            document_cleanup_error = self._delete_unretained_global_document(
                self._uploaded_document
            )
            if document_cleanup_error:
                cleanup_errors.append(document_cleanup_error)

        try:
            self._workspace_client.delete_workspace(
                self._context_ref,
                user_id=self._user_id,
            )
            self._record_lifecycle_event(
                operation="context_delete",
                attempt=1,
                success=True,
                external_ref=self._context_ref,
            )
            logger.info(
                "anythingllm.session.cleanup: operation=context_delete context_ref=%s "
                "conversation_ref=%s cleanup_status=succeeded",
                self._context_ref,
                self._conversation_ref,
            )
        except Exception as exc:
            error_message = AnythingLLMRagGateway._safe_error_message(
                exc,
                fallback="删除隔离上下文失败",
            )
            cleanup_errors.append(error_message)
            self._record_lifecycle_event(
                operation="context_delete",
                attempt=1,
                success=False,
                external_ref=self._context_ref,
                failure_stage="cleanup",
                error_message=error_message,
            )
            logger.error(
                "anythingllm.session.cleanup: operation=context_delete context_ref=%s "
                "conversation_ref=%s cleanup_status=failed error_type=%s",
                self._context_ref,
                self._conversation_ref,
                type(exc).__name__,
            )
        result = CleanupResult(
            success=not cleanup_errors,
            already_closed=False,
            error_message="；".join(cleanup_errors),
        )
        self._first_cleanup_result = result
        logger.log(
            logging.INFO if result.success else logging.WARNING,
            "anythingllm.session.closed: context_ref=%s conversation_ref=%s "
            "retain_document=%s cleanup_status=%s cleanup_error_count=%d",
            self._context_ref,
            self._conversation_ref,
            retain_document,
            "succeeded" if result.success else "failed",
            len(cleanup_errors),
        )
        return result

    def _upload_document(self, file_path: str) -> AnythingLLMDocument:
        """上传文档并防御性校验原子 Client 返回的三个关键身份字段。"""
        try:
            document = self._document_client.upload_document(
                file_path,
                user_id=self._user_id,
            )
        except AnythingLLMProtocolError as exc:
            error_message = self._safe_error(
                exc,
                fallback="文档上传响应不符合协议",
            )
            self._record_lifecycle_event(
                operation="document_upload",
                attempt=1,
                success=False,
                failure_stage="upload_protocol",
                error_message=error_message,
            )
            raise self._operation_error(
                error_message,
                failure_stage="upload_protocol",
            ) from exc
        except Exception as exc:
            error_message = self._safe_error(exc, fallback="文档上传失败")
            self._record_lifecycle_event(
                operation="document_upload",
                attempt=1,
                success=False,
                failure_stage="upload",
                error_message=error_message,
            )
            raise self._operation_error(
                error_message,
                failure_stage="upload",
            ) from exc

        document_id = str(getattr(document, "id", "") or "").strip()
        location = str(getattr(document, "location", "") or "").strip()
        document_ref = str(getattr(document, "document_ref", "") or "").strip()
        if not document_id or not location or not document_ref:
            error_message = "文档上传结果缺少有效 id、location 或 document_ref"
            self._record_lifecycle_event(
                operation="document_upload",
                attempt=1,
                success=False,
                external_ref=location or None,
                failure_stage="upload_protocol",
                error_message=error_message,
            )
            if location:
                # 上传已经产生了可定位的全局实体。即使其他身份字段不完整，也必须保留
                # location 供审计成功后的 close 执行补偿，避免协议漂移制造孤儿文档。
                self._schedule_failed_document_cleanup(document)
            raise self._operation_error(
                error_message,
                failure_stage="upload_protocol",
            )
        self._record_lifecycle_event(
            operation="document_upload",
            attempt=1,
            success=True,
            external_ref=location,
        )
        logger.info(
            "anythingllm.document.uploaded: operation=analyse context_ref=%s "
            "upload_id=%s location=%s document_ref=%s file_name=%s",
            self._context_ref,
            document_id,
            location,
            document_ref,
            Path(file_path).name,
        )
        return document

    def _bind_document(self, location: str) -> None:
        """把真实上传位置加入工作区，并仅对标准暂态网关错误有限重试。"""
        first_lifecycle_attempt = self._next_lifecycle_attempt("document_bind")
        for local_attempt in range(1, self._embedding_max_attempts + 1):
            lifecycle_attempt = first_lifecycle_attempt + local_attempt - 1
            try:
                workspace = self._workspace_client.update_embeddings(
                    self._context_ref,
                    adds=(location,),
                    user_id=self._user_id,
                )
                returned_ref = str(getattr(workspace, "slug", "") or "").strip()
                if not returned_ref:
                    raise AnythingLLMProtocolError(
                        "AnythingLLM 更新嵌入结果缺少有效工作区"
                    )
                if returned_ref.casefold() != self._context_ref.casefold():
                    raise AnythingLLMProtocolError(
                        "AnythingLLM 更新嵌入结果的工作区与目标不一致"
                    )
                self._record_lifecycle_event(
                    operation="document_bind",
                    attempt=lifecycle_attempt,
                    success=True,
                    external_ref=location,
                )
                logger.info(
                    "anythingllm.embedding.accepted: operation=analyse context_ref=%s "
                    "attempt=%d location=%s",
                    self._context_ref,
                    lifecycle_attempt,
                    location,
                )
                return
            except AnythingLLMProtocolError as exc:
                error_message = self._safe_error(
                    exc,
                    fallback="嵌入响应不符合协议",
                )
                self._record_lifecycle_event(
                    operation="document_bind",
                    attempt=lifecycle_attempt,
                    success=False,
                    external_ref=location,
                    failure_stage="embedding_protocol",
                    error_message=error_message,
                )
                raise self._operation_error(
                    error_message,
                    failure_stage="embedding_protocol",
                ) from exc
            except AnythingLLMHTTPError as exc:
                error_message = self._safe_error(
                    exc,
                    fallback="文档加入隔离上下文失败",
                )
                self._record_lifecycle_event(
                    operation="document_bind",
                    attempt=lifecycle_attempt,
                    success=False,
                    external_ref=location,
                    failure_stage="embedding",
                    error_message=error_message,
                )
                can_retry = (
                    exc.status_code in self._TRANSIENT_EMBEDDING_STATUS_CODES
                    and local_attempt < self._embedding_max_attempts
                )
                if can_retry:
                    logger.warning(
                        "anythingllm.embedding.retry: operation=analyse context_ref=%s "
                        "attempt=%d/%d http_status=%s",
                        self._context_ref,
                        local_attempt,
                        self._embedding_max_attempts,
                        exc.status_code,
                    )
                    continue
                raise self._operation_error(
                    error_message,
                    failure_stage="embedding",
                ) from exc
            except Exception as exc:
                error_message = self._safe_error(
                    exc,
                    fallback="文档加入隔离上下文失败",
                )
                self._record_lifecycle_event(
                    operation="document_bind",
                    attempt=lifecycle_attempt,
                    success=False,
                    external_ref=location,
                    failure_stage="embedding",
                    error_message=error_message,
                )
                raise self._operation_error(
                    error_message,
                    failure_stage="embedding",
                ) from exc

        raise AssertionError("嵌入重试循环异常结束")

    def _pin_document(self, location: str) -> None:
        """固定目标文档；首次 404 时重新加入一次文档后再重试 Pin。

        该恢复策略不查询工作区详情，也不猜测或反查文档 ID。第二次 Pin 仍返回 404 时
        立即以 ``pin_not_found`` 失败，避免循环恢复掩盖一致性问题。
        """
        for pin_attempt in (1, 2):
            try:
                self._workspace_client.update_pin(
                    self._context_ref,
                    location,
                    pinned=True,
                    user_id=self._user_id,
                )
                self._record_lifecycle_event(
                    operation="document_pin",
                    attempt=pin_attempt,
                    success=True,
                    external_ref=location,
                )
                logger.info(
                    "anythingllm.document.pinned: operation=analyse context_ref=%s "
                    "attempt=%d location=%s",
                    self._context_ref,
                    pin_attempt,
                    location,
                )
                return
            except AnythingLLMHTTPError as exc:
                failure_stage = "pin_not_found" if exc.status_code == 404 else "pin"
                error_message = self._safe_error(exc, fallback="文档 Pin 失败")
                self._record_lifecycle_event(
                    operation="document_pin",
                    attempt=pin_attempt,
                    success=False,
                    external_ref=location,
                    failure_stage=failure_stage,
                    error_message=error_message,
                )
                if exc.status_code == 404 and pin_attempt == 1:
                    logger.warning(
                        "anythingllm.pin.recover: operation=analyse context_ref=%s "
                        "attempt=1 http_status=404 action=rebind",
                        self._context_ref,
                    )
                    self._bind_document(location)
                    continue
                raise self._operation_error(
                    error_message,
                    failure_stage=failure_stage,
                ) from exc
            except AnythingLLMProtocolError as exc:
                error_message = self._safe_error(
                    exc,
                    fallback="Pin 响应不符合协议",
                )
                self._record_lifecycle_event(
                    operation="document_pin",
                    attempt=pin_attempt,
                    success=False,
                    external_ref=location,
                    failure_stage="pin_protocol",
                    error_message=error_message,
                )
                raise self._operation_error(
                    error_message,
                    failure_stage="pin_protocol",
                ) from exc
            except Exception as exc:
                error_message = self._safe_error(exc, fallback="文档 Pin 失败")
                self._record_lifecycle_event(
                    operation="document_pin",
                    attempt=pin_attempt,
                    success=False,
                    external_ref=location,
                    failure_stage="pin",
                    error_message=error_message,
                )
                raise self._operation_error(
                    error_message,
                    failure_stage="pin",
                ) from exc

        raise AssertionError("Pin 恢复循环异常结束")

    def _query(
        self,
        *,
        prompt: str,
        operation: str,
        prompt_kind: str,
        require_sources: bool,
        max_attempts: int,
    ) -> RagResult:
        """执行有限次同线程查询，并记录每一次回答或异常。

        本方法故意不向 ``ThreadClient.ask`` 传入 ``document_ids``，从调用边界保证请求体
        不会生成 ``files`` 字段。来源验证只比较原子 Client 已归一化的稳定引用。
        """
        target_ref = self._document_ref
        uploaded_document = self._uploaded_document
        external_location = str(
            getattr(uploaded_document, "location", "") or ""
        ).strip()
        if not target_ref or not external_location:
            raise self._operation_error(
                "当前会话缺少目标文档引用或外部位置",
                failure_stage="session_not_prepared",
            )

        last_stage = "query"
        last_error = "模型查询失败"
        for attempt_number in range(1, max_attempts + 1):
            try:
                answer = self._thread_client.ask(
                    self._context_ref,
                    self._conversation_ref,
                    prompt,
                    user_id=self._user_id,
                )
                text = str(getattr(answer, "text", "") or "").strip()
                raw_response = str(
                    getattr(answer, "raw_text", "") or text
                )
                sources, unresolved_count = self._adapt_sources(
                    getattr(answer, "sources", ()),
                )
                matched = any(source.document_ref == target_ref for source in sources)
                failure_stage: Optional[str] = None
                error_message: Optional[str] = None
                if not text:
                    failure_stage = "query"
                    error_message = "模型返回空文本"
                elif require_sources and not sources:
                    failure_stage = "sources"
                    error_message = "模型回答缺少可识别来源"
                elif require_sources and not matched:
                    failure_stage = "sources"
                    error_message = "模型来源未匹配目标文档"

                self._attempts.append(
                    RagAttempt(
                        operation=operation,
                        attempt=attempt_number,
                        prompt_kind=prompt_kind,
                        raw_response=raw_response,
                        sources=sources,
                        failure_stage=failure_stage,
                        error_message=error_message,
                    )
                )
                if failure_stage is None:
                    self._failure_stage = None
                    self._error_message = None
                    result = RagResult(
                        text=text,
                        sources=sources,
                        prepared_document=PreparedDocumentRef(
                            document_ref=target_ref,
                            external_location=external_location,
                        ),
                        trace=self._trace(),
                    )
                    logger.info(
                        "anythingllm.query.completed: operation=%s context_ref=%s "
                        "conversation_ref=%s attempt=%d response_length=%d "
                        "sources_count=%d unresolved_sources_count=%d "
                        "matched_document_ref=%s",
                        operation,
                        self._context_ref,
                        self._conversation_ref,
                        attempt_number,
                        len(text),
                        len(sources),
                        unresolved_count,
                        target_ref if matched else "",
                    )
                    return result
                last_stage = failure_stage
                last_error = error_message or "模型回答不符合 RAG 契约"
            except RagOperationError:
                raise
            except AnythingLLMTransportError as exc:
                last_stage = "query"
                last_error = self._safe_error(exc, fallback="模型查询失败")
                self._attempts.append(
                    RagAttempt(
                        operation=operation,
                        attempt=attempt_number,
                        prompt_kind=prompt_kind,
                        raw_response=None,
                        sources=(),
                        failure_stage=last_stage,
                        error_message=last_error,
                    )
                )
            except Exception as exc:
                # 未知异常可能代表适配器编程错误，不能自动重放模型调用；但它仍然发生在
                # 一次真实查询边界内，必须形成单次失败 attempt 后立即终止，供审计定位。
                last_stage = "query"
                last_error = self._safe_error(exc, fallback="模型查询发生未知异常")
                self._attempts.append(
                    RagAttempt(
                        operation=operation,
                        attempt=attempt_number,
                        prompt_kind=prompt_kind,
                        raw_response=None,
                        sources=(),
                        failure_stage=last_stage,
                        error_message=last_error,
                    )
                )
                raise self._operation_error(
                    last_error,
                    failure_stage=last_stage,
                ) from exc

            logger.warning(
                "anythingllm.query.retry_or_fail: operation=%s context_ref=%s "
                "conversation_ref=%s attempt=%d/%d stage=%s",
                operation,
                self._context_ref,
                self._conversation_ref,
                attempt_number,
                max_attempts,
                last_stage,
            )

        raise self._operation_error(last_error, failure_stage=last_stage)

    @staticmethod
    def _adapt_sources(
        sources: Sequence[AnythingLLMSource],
    ) -> tuple[tuple[RagSource, ...], int]:
        """把供应商来源 DTO 转为业务 DTO，并统计无法识别身份的来源。

        ``RagSource`` 要求 ``document_ref`` 非空，因此无法归一化身份的供应商来源不会伪造
        引用，而是从业务来源集合中排除。调用方仍可通过计数日志发现上游字段漂移。
        """
        adapted: list[RagSource] = []
        unresolved_count = 0
        for source in tuple(sources or ()):
            document_ref = str(getattr(source, "document_ref", "") or "").strip()
            if not document_ref:
                unresolved_count += 1
                continue
            adapted.append(
                RagSource(
                    document_ref=document_ref,
                    text=str(getattr(source, "text", "") or ""),
                    id=getattr(source, "id", None),
                    title=getattr(source, "title", None),
                    url=getattr(source, "url", None),
                    score=getattr(source, "score", None),
                )
            )
        return tuple(adapted), unresolved_count

    def _schedule_failed_document_cleanup(
        self,
        document: AnythingLLMDocument,
    ) -> None:
        """标记失败流程的全局文档必须在审计成功后的 close 中删除。

        本方法不立即执行外部删除。阶段 7 的业务编排必须先持久化失败 trace，审计成功后
        才调用 ``close``；若审计失败则不调用 close，从而保留完整上游现场。
        """
        self._uploaded_document = document
        self._global_document_cleanup_required = True
        self._analyse_succeeded = False

    def _delete_unretained_global_document(
        self,
        document: AnythingLLMDocument,
    ) -> str:
        """永久删除尚未完成所有权转交的文档，并返回空串或安全错误信息。

        删除 API 会清除源文档、向量缓存和所有 Workspace 关联。该方法只能由 ``close``
        在审计成功后调用；既覆盖 RAG 内部失败，也覆盖 RAG 成功但后续业务契约或永久
        知识库转交失败的路径。已经完成转交的文档必须通过 ``retain_document=True`` 保留。
        """
        location = str(getattr(document, "location", "") or "").strip()
        cleanup_error = ""
        try:
            self._document_client.delete_document(
                location,
                user_id=self._user_id,
            )
            self._record_lifecycle_event(
                operation="global_document_delete",
                attempt=1,
                success=True,
                external_ref=location,
            )
            logger.info(
                "anythingllm.document.compensated: operation=global_document_delete "
                "context_ref=%s location=%s cleanup_status=succeeded",
                self._context_ref,
                location,
            )
            self._uploaded_document = None
            self._document_ref = None
            self._global_document_cleanup_required = False
        except Exception as exc:
            cleanup_error = self._safe_error(
                exc,
                fallback="失败流程的全局文档删除失败",
            )
            self._record_lifecycle_event(
                operation="global_document_delete",
                attempt=1,
                success=False,
                external_ref=location or None,
                failure_stage="global_document_cleanup",
                error_message=cleanup_error,
            )
            logger.error(
                "anythingllm.document.compensation_failed: "
                "operation=global_document_delete context_ref=%s location=%s "
                "cleanup_status=failed error_type=%s",
                self._context_ref,
                location,
                type(exc).__name__,
            )
        return cleanup_error

    def _record_lifecycle_event(
        self,
        *,
        operation: str,
        attempt: int,
        success: bool,
        external_ref: Optional[str] = None,
        failure_stage: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """按真实发生顺序追加一次资源操作事件。"""
        self._lifecycle_events.append(
            RagLifecycleEvent(
                sequence_no=len(self._lifecycle_events) + 1,
                operation=operation,
                attempt=attempt,
                success=success,
                external_ref=external_ref,
                failure_stage=failure_stage,
                error_message=error_message,
            )
        )

    def _next_lifecycle_attempt(self, operation: str) -> int:
        """返回同名生命周期操作的下一个连续尝试序号。

        Pin 404 恢复会再次进入 Embedding 方法。如果每次方法调用都从 1 编号，审计中会
        出现两个无法区分的 ``document_bind attempt=1``。该辅助方法基于既有不可变事件
        语义递增编号，使跨方法重入仍保持稳定顺序。
        """
        existing_attempts = (
            event.attempt
            for event in self._lifecycle_events
            if event.operation == operation
        )
        return max(existing_attempts, default=0) + 1

    def _ensure_open(self) -> None:
        """会话关闭后以可审计异常拒绝任何模型或准备操作。"""
        if self._closed:
            raise self._operation_error(
                "RAG Session 已关闭",
                failure_stage="session_closed",
            )

    def _operation_error(self, message: str, *, failure_stage: str) -> RagOperationError:
        """更新总体失败状态，并返回携带当前轨迹快照的稳定异常。"""
        self._failure_stage = failure_stage
        self._error_message = message
        logger.error(
            "anythingllm.rag.failed: context_ref=%s conversation_ref=%s stage=%s",
            self._context_ref,
            self._conversation_ref,
            failure_stage,
        )
        return RagOperationError(message, self._trace())

    def _trace(self) -> RagExecutionTrace:
        """根据当前内部状态创建独立、不可变的轨迹快照。"""
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
    def _safe_error(error: Exception, *, fallback: str) -> str:
        """复用 Gateway 的安全错误规范，避免轨迹保存未知异常正文。"""
        return AnythingLLMRagGateway._safe_error_message(error, fallback=fallback)

    @staticmethod
    def _required_text(value: str, *, name: str) -> str:
        """规范化并校验会话操作所需的非空文本参数。"""
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return normalized

    @staticmethod
    def _validate_max_attempts(max_attempts: int) -> None:
        """在产生任何外部副作用前校验模型调用次数上限。"""
        if not 1 <= max_attempts <= MAX_RAG_QUERY_ATTEMPTS:
            raise ValueError(
                f"max_attempts 必须介于 1 和 {MAX_RAG_QUERY_ATTEMPTS} 之间"
            )
