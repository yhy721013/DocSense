"""AnythingLLM 文档 RAG 的任务级依赖工厂。

本模块是新文档 RAG 链路中唯一负责创建供应商对象图的位置。工厂自身只保存不可变配置，
不持有 ``requests.Session``；每次进入 ``create`` 返回的上下文时，都会创建独立
Transport、三个原子 Client 和一个 Gateway，并在退出任务作用域时关闭 Transport。
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager, contextmanager
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Optional

from app.integrations.anythingllm.documents import AnythingLLMDocumentClient
from app.integrations.anythingllm.policies import (
    DEFAULT_EMBEDDING_ATTEMPTS,
    DEFAULT_UPLOAD_RETRIES,
    DEFAULT_UPLOAD_RETRY_BASE_DELAY_SECONDS,
    validate_embedding_max_attempts,
    validate_upload_max_retries,
    validate_upload_retry_base_delay,
)
from app.integrations.anythingllm.rag_gateway import AnythingLLMRagGateway
from app.integrations.anythingllm.threads import AnythingLLMThreadClient
from app.integrations.anythingllm.transport import AnythingLLMTransport
from app.integrations.anythingllm.workspaces import AnythingLLMWorkspaceClient
from app.ports import DocumentRagPort
from app.services.core.config import AnythingLLMConfig


logger = logging.getLogger(__name__)


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
        self._workspace_settings = MappingProxyType(dict(workspace_settings or {}))
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
