"""AnythingLLM 文档 RAG 与永久知识库的任务级依赖工厂。

本模块是新集成链路中唯一负责创建供应商对象图的位置。工厂自身只保存不可变配置和
线程安全协调依赖，不持有 ``requests.Session``；每次进入 ``create`` 返回的上下文时，
都会创建独立 Transport、所需原子 Client 和一个 Gateway，并在退出任务作用域时关闭
Transport。
"""

from __future__ import annotations

import logging
import threading
from contextlib import AbstractContextManager, contextmanager
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Optional

from app.integrations.anythingllm.documents import AnythingLLMDocumentClient
from app.integrations.anythingllm.knowledge_gateway import (
    AnythingLLMKnowledgeGateway,
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
from app.integrations.anythingllm.rag_gateway import AnythingLLMRagGateway
from app.integrations.anythingllm.threads import AnythingLLMThreadClient
from app.integrations.anythingllm.transport import AnythingLLMTransport
from app.integrations.anythingllm.workspaces import AnythingLLMWorkspaceClient
from app.ports import DocumentRagPort, KnowledgeIndexPort
from app.services.core.config import AnythingLLMConfig
from app.services.core.database import DatabaseService
from app.services.llm_service.knowledge_index_operation_service import (
    KnowledgeIndexOperationService,
)


logger = logging.getLogger(__name__)


class _CollectionLockRegistry:
    """为永久集合提供进程内细粒度可重入锁。

    同一集合的绑定、替换和解绑仍严格串行，不同 architecture 则可以并行执行。字典只按
    已实际访问的永久集合增长，集合数量受业务分类规模限制，不按任务或文档数量增长。
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def lock_for(self, key: str) -> threading.RLock:
        """返回指定集合的稳定锁实例，并原子创建首次访问项。"""
        normalized_key = str(key or "").strip()
        if not normalized_key:
            raise ValueError("集合锁 key 不能为空")
        with self._guard:
            return self._locks.setdefault(normalized_key, threading.RLock())


class AnythingLLMGatewayFactory:
    """使用不可变配置为每个任务创建隔离的文档 RAG 对象图。

    工厂可以安全地保存在 Flask 应用容器中并由多个线程共享，因为实例不保存任务状态，
    也不缓存 Transport、Client 或 Gateway。所有有状态对象只存在于单次 ``create`` 租约
    内，禁止调用方把租约产出的 Port 保存为应用级单例。
    """

    def __init__(
        self,
        config: AnythingLLMConfig,
        *,
        user_id: Optional[int] = 1,
        workspace_settings: Optional[Mapping[str, Any]] = None,
        upload_max_retries: int = DEFAULT_UPLOAD_RETRIES,
        upload_retry_base_delay: float = DEFAULT_UPLOAD_RETRY_BASE_DELAY_SECONDS,
        embedding_max_attempts: int = DEFAULT_EMBEDDING_ATTEMPTS,
        transport_factory: Callable[..., AnythingLLMTransport] = AnythingLLMTransport,
    ) -> None:
        """校验工厂级策略并保存不可变配置，不创建任何网络会话。

        ``transport_factory`` 仅用于离线测试替换 Transport 构造过程。生产装配不传该参数，
        因此每个任务都会由 ``AnythingLLMTransport`` 创建新的 ``requests.Session``。
        重试参数在工厂入口再次限制硬上限，避免错误配置绕过原子 Client 或 Gateway 的
        资源保护契约。
        """
        if not isinstance(config, AnythingLLMConfig):
            raise TypeError("config 必须是 AnythingLLMConfig")
        if user_id is not None and (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id < 1
        ):
            raise ValueError("user_id 必须是正整数或 None")
        validated_upload_retries = validate_upload_max_retries(
            upload_max_retries
        )
        validated_retry_base_delay = validate_upload_retry_base_delay(
            upload_retry_base_delay
        )
        validated_embedding_attempts = validate_embedding_max_attempts(
            embedding_max_attempts
        )
        if not callable(transport_factory):
            raise TypeError("transport_factory 必须可调用")

        self._config = config
        self._user_id = user_id
        resolved_workspace_settings = (
            document_rag_workspace_settings()
            if workspace_settings is None
            else dict(workspace_settings)
        )
        self._workspace_settings = MappingProxyType(resolved_workspace_settings)
        self._upload_max_retries = validated_upload_retries
        self._upload_retry_base_delay = validated_retry_base_delay
        self._embedding_max_attempts = validated_embedding_attempts
        self._transport_factory = transport_factory

    def create(self) -> AbstractContextManager[DocumentRagPort]:
        """返回一次惰性任务租约；真正对象创建发生在进入 ``with`` 时。"""
        return self._create_lease()

    @contextmanager
    def _create_lease(self) -> Iterator[DocumentRagPort]:
        """创建对象图并确保 Transport 在任意退出路径上只关闭一次。

        如果业务代码本身正在抛出异常，而底层自定义 Session 的 ``close`` 又发生异常，
        清理异常只记录日志，不覆盖原始业务异常；正常退出时的关闭异常则继续抛出，使资源
        泄漏不会被误报为任务成功。
        """
        transport: Optional[AnythingLLMTransport] = None
        task_failed = False
        try:
            transport = self._transport_factory(
                base_url=self._config.base_url,
                api_key=self._config.api_key,
                timeout=self._config.timeout,
            )
            document_client = AnythingLLMDocumentClient(
                transport,
                upload_max_retries=self._upload_max_retries,
                upload_retry_base_delay=self._upload_retry_base_delay,
            )
            workspace_client = AnythingLLMWorkspaceClient(transport)
            thread_client = AnythingLLMThreadClient(transport)
            gateway = AnythingLLMRagGateway(
                document_client,
                workspace_client,
                thread_client,
                user_id=self._user_id,
                workspace_settings=self._workspace_settings,
                embedding_max_attempts=self._embedding_max_attempts,
            )
            logger.debug(
                "创建 AnythingLLM 任务级 RAG 对象图: has_user_context=%s",
                self._user_id is not None,
            )
            yield gateway
        except BaseException:
            task_failed = True
            raise
        finally:
            if transport is not None:
                try:
                    transport.close()
                    logger.debug("关闭 AnythingLLM 任务级 RAG 对象图")
                except Exception:
                    if task_failed:
                        logger.exception(
                            "关闭 AnythingLLM 任务级 Transport 失败，保留原始任务异常"
                        )
                    else:
                        raise


class AnythingLLMKnowledgeIndexFactory:
    """为每个任务创建永久知识库 Gateway，并共享集合级协调锁注册表。

    Factory 自身不持有网络连接，可以安全地作为应用单例共享。每次租约仍创建独立
    Transport 和原子 Client；同一永久集合内的复合状态转换由共享可重入锁串行化，不同
    architecture 可以并行执行。跨进程互斥继续由 SQLite 唯一约束和状态检查承担。
    """

    def __init__(
        self,
        config: AnythingLLMConfig,
        operation_service: KnowledgeIndexOperationService,
        database_service: DatabaseService,
        *,
        user_id: Optional[int] = 1,
        workspace_settings: Optional[Mapping[str, Any]] = None,
        upload_max_retries: int = DEFAULT_UPLOAD_RETRIES,
        upload_retry_base_delay: float = DEFAULT_UPLOAD_RETRY_BASE_DELAY_SECONDS,
        transport_factory: Callable[..., AnythingLLMTransport] = AnythingLLMTransport,
    ) -> None:
        """校验永久知识库对象图依赖，不在应用启动阶段创建 HTTP Session。"""
        if not isinstance(config, AnythingLLMConfig):
            raise TypeError("config 必须是 AnythingLLMConfig")
        if not isinstance(operation_service, KnowledgeIndexOperationService):
            raise TypeError("operation_service 类型无效")
        if not isinstance(database_service, DatabaseService):
            raise TypeError("database_service 类型无效")
        if user_id is not None and (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id < 1
        ):
            raise ValueError("user_id 必须是正整数或 None")
        if not callable(transport_factory):
            raise TypeError("transport_factory 必须可调用")
        self._config = config
        self._operation_service = operation_service
        self._database_service = database_service
        self._user_id = user_id
        resolved_workspace_settings = (
            document_rag_workspace_settings()
            if workspace_settings is None
            else dict(workspace_settings)
        )
        self._workspace_settings = MappingProxyType(resolved_workspace_settings)
        self._upload_max_retries = validate_upload_max_retries(upload_max_retries)
        self._upload_retry_base_delay = validate_upload_retry_base_delay(
            upload_retry_base_delay
        )
        self._transport_factory = transport_factory
        self._operation_locks = _CollectionLockRegistry()

    def create(self) -> AbstractContextManager[KnowledgeIndexPort]:
        """返回惰性任务租约，进入 ``with`` 后才创建网络对象图。"""
        return self._create_lease()

    @contextmanager
    def _create_lease(self) -> Iterator[KnowledgeIndexPort]:
        """创建永久知识库对象图，并在所有退出路径关闭 Transport。"""
        transport: Optional[AnythingLLMTransport] = None
        task_failed = False
        try:
            transport = self._transport_factory(
                base_url=self._config.base_url,
                api_key=self._config.api_key,
                timeout=self._config.timeout,
            )
            document_client = AnythingLLMDocumentClient(
                transport,
                upload_max_retries=self._upload_max_retries,
                upload_retry_base_delay=self._upload_retry_base_delay,
            )
            workspace_client = AnythingLLMWorkspaceClient(transport)
            gateway = AnythingLLMKnowledgeGateway(
                document_client,
                workspace_client,
                self._operation_service,
                self._database_service,
                operation_lock_factory=self._operation_locks.lock_for,
                user_id=self._user_id,
                workspace_settings=self._workspace_settings,
            )
            logger.debug(
                "创建 AnythingLLM 任务级永久知识库对象图: has_user_context=%s",
                self._user_id is not None,
            )
            yield gateway
        except BaseException:
            task_failed = True
            raise
        finally:
            if transport is not None:
                try:
                    transport.close()
                    logger.debug("关闭 AnythingLLM 任务级永久知识库对象图")
                except Exception:
                    if task_failed:
                        logger.exception(
                            "关闭永久知识库 Transport 失败，保留原始任务异常"
                        )
                    else:
                        raise
