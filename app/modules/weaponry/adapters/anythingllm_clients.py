"""武器谱 Adapter 共用的 AnythingLLM 任务级对象工厂。

工厂只保存不可变配置，不保存 ``requests.Session``。每次进入 ``create`` 租约都会创建独立
Transport 和原子 Client，因此不同 weaponry execution 不会共享 Cookie、连接级可变状态或
供应商会话。该文件只属于 Adapter 层，任何供应商 DTO 都不会穿过 Ports。
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol, runtime_checkable

from app.integrations.anythingllm import (
    AnythingLLMDocumentClient,
    AnythingLLMThreadClient,
    AnythingLLMTransport,
    AnythingLLMWorkspaceClient,
)
from app.services.core.config import AnythingLLMConfig


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeaponryAnythingLLMClients:
    """一次任务租约内共享同一 Transport 的原子 Client 集合。"""

    documents: AnythingLLMDocumentClient
    workspaces: AnythingLLMWorkspaceClient
    threads: AnythingLLMThreadClient


@runtime_checkable
class WeaponryAnythingLLMClientFactoryProtocol(Protocol):
    """允许生产 Transport 和离线 Fake Client 使用同一个 Adapter。"""

    def create(self) -> AbstractContextManager[WeaponryAnythingLLMClients]:
        ...


class AnythingLLMWeaponryClientFactory:
    """为每次武器谱外部调用建立独立 HTTP Transport。"""

    def __init__(
        self,
        config: AnythingLLMConfig,
        *,
        transport_factory: Callable[..., AnythingLLMTransport] = AnythingLLMTransport,
    ) -> None:
        if not isinstance(config, AnythingLLMConfig):
            raise TypeError("config 必须是 AnythingLLMConfig")
        if not callable(transport_factory):
            raise TypeError("transport_factory 必须可调用")
        self._config = config
        self._transport_factory = transport_factory

    def create(self) -> AbstractContextManager[WeaponryAnythingLLMClients]:
        return self._create_lease()

    @contextmanager
    def _create_lease(self) -> Iterator[WeaponryAnythingLLMClients]:
        transport: AnythingLLMTransport | None = None
        task_failed = False
        try:
            transport = self._transport_factory(
                base_url=self._config.base_url,
                api_key=self._config.api_key,
                timeout=self._config.timeout,
            )
            yield WeaponryAnythingLLMClients(
                documents=AnythingLLMDocumentClient(transport),
                workspaces=AnythingLLMWorkspaceClient(transport),
                threads=AnythingLLMThreadClient(transport),
            )
        except BaseException:
            task_failed = True
            raise
        finally:
            if transport is not None:
                try:
                    transport.close()
                    logger.debug("武器谱 AnythingLLM 任务级 Transport 已关闭")
                except Exception:
                    # 清理异常不能覆盖更早的业务异常；正常退出时则必须向上传播，避免把
                    # HTTP 连接泄漏误报为一次完整成功的外部操作。
                    if task_failed:
                        logger.exception(
                            "关闭武器谱 AnythingLLM Transport 失败，保留原始异常"
                        )
                    else:
                        raise


__all__ = [
    "AnythingLLMWeaponryClientFactory",
    "WeaponryAnythingLLMClientFactoryProtocol",
    "WeaponryAnythingLLMClients",
]
