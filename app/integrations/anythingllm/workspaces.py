"""AnythingLLM 工作区接口的原子客户端。

该客户端封装工作区 CRUD、文档列表、嵌入变更、Pin 和向量检索。每个方法只执行一次
上游原子操作；例如加入文档不会隐式 Pin 或更新元数据，跨接口编排由 Gateway 或迁移期
Facade 明确完成。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import quote

from app.integrations.anythingllm.errors import AnythingLLMProtocolError
from app.integrations.anythingllm.models import (
    AnythingLLMDocument,
    AnythingLLMSource,
    AnythingLLMWorkspace,
    normalize_document_path,
    normalize_document_ref,
    require_mapping,
    require_sequence,
)
from app.integrations.anythingllm.transport import AnythingLLMTransport


logger = logging.getLogger(__name__)


class AnythingLLMWorkspaceClient:
    """提供不互相编排的 AnythingLLM 工作区原子操作。"""

    def __init__(self, transport: AnythingLLMTransport) -> None:
        """绑定任务级传输对象，但不接管其关闭职责。"""
        self._transport = transport

    def list_workspaces(
        self,
        *,
        user_id: int | None = None,
    ) -> list[AnythingLLMWorkspace]:
        """获取全部工作区并统一 ``slug``/``id`` 字段别名。"""
        body = self._transport.get_json("workspaces", user_id=user_id)
        payload = require_mapping(body, context="工作区列表响应")
        items = require_sequence(payload.get("workspaces"), context="workspaces 字段")
        workspaces = [AnythingLLMWorkspace.from_payload(item) for item in items]
        logger.debug(
            "获取 AnythingLLM 工作区列表完成: workspace_count=%d has_user_context=%s",
            len(workspaces),
            user_id is not None,
        )
        return workspaces

    def get_workspace(
        self,
        workspace_slug: str,
        *,
        user_id: int | None = None,
    ) -> AnythingLLMWorkspace:
        """获取指定工作区的统一 DTO，不暴露其中的原始文档结构。"""
        payload = self._get_workspace_payload(workspace_slug, user_id=user_id)
        workspace = AnythingLLMWorkspace.from_payload(payload)
        logger.debug(
            "获取 AnythingLLM 工作区完成: workspace_slug=%s workspace_id=%s",
            workspace.slug,
            workspace.id,
        )
        return workspace

    def create_workspace(
        self,
        name: str,
        *,
        settings: Optional[Mapping[str, Any]] = None,
        user_id: int | None = None,
    ) -> AnythingLLMWorkspace:
        """创建工作区，并把可选供应商配置作为显式参数发送。

        ``settings`` 不能覆盖 ``name``，确保方法实参始终是工作区名称的唯一来源。该方法
        不判断同名工作区是否存在，查找或复用属于更外层编排职责。
        """
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("工作区 name 不能为空")
        request_payload = dict(settings or {})
        request_payload["name"] = normalized_name
        body = self._transport.post_json(
            "workspace/new",
            request_payload,
            user_id=user_id,
        )
        payload = require_mapping(body, context="创建工作区响应")
        if payload.get("error"):
            raise AnythingLLMProtocolError("AnythingLLM 明确拒绝创建工作区")
        workspace = AnythingLLMWorkspace.from_payload(payload.get("workspace") or payload)
        logger.info(
            "AnythingLLM 工作区创建完成: workspace_name=%s workspace_slug=%s "
            "workspace_id=%s has_user_context=%s",
            workspace.name,
            workspace.slug,
            workspace.id,
            user_id is not None,
        )
        return workspace

    def update_workspace(
        self,
        workspace_slug: str,
        settings: Mapping[str, Any],
        *,
        user_id: int | None = None,
    ) -> AnythingLLMWorkspace:
        """更新一个工作区的供应商配置并返回最新统一 DTO。"""
        path = f"workspace/{self._path_segment(workspace_slug)}/update"
        body = self._transport.post_json(path, dict(settings), user_id=user_id)
        payload = require_mapping(body, context="更新工作区响应")
        if payload.get("error"):
            raise AnythingLLMProtocolError("AnythingLLM 明确拒绝更新工作区")
        workspace = AnythingLLMWorkspace.from_payload(payload.get("workspace") or payload)
        logger.info(
            "AnythingLLM 工作区配置更新完成: workspace_slug=%s setting_keys=%s",
            workspace.slug,
            sorted(str(key) for key in settings.keys()),
        )
        return workspace

    def delete_workspace(
        self,
        workspace_slug: str,
        *,
        user_id: int | None = None,
    ) -> None:
        """删除指定工作区，仅以 HTTP 状态码判断操作是否成功。

        AnythingLLM 的该端点在不同部署中可能返回空正文、JSON 或纯文本 ``OK``，正文
        不构成稳定业务契约。因此这里使用状态码型 DELETE，并仍由传输层统一处理超时、
        连接异常和非 2xx 状态。
        """
        path = f"workspace/{self._path_segment(workspace_slug)}"
        self._transport.delete_status(path, user_id=user_id)
        logger.info(
            "AnythingLLM 工作区删除完成: workspace_slug=%s has_user_context=%s",
            workspace_slug,
            user_id is not None,
        )

    def list_documents(
        self,
        workspace_slug: str,
        *,
        user_id: int | None = None,
    ) -> list[AnythingLLMDocument]:
        """获取工作区文档，并在适配层统一 ID 与位置字段别名。"""
        payload = self._get_workspace_payload(workspace_slug, user_id=user_id)
        documents = require_sequence(
            payload.get("documents", []),
            context="工作区 documents 字段",
        )
        normalized_documents = [AnythingLLMDocument.from_payload(item) for item in documents]
        logger.debug(
            "获取 AnythingLLM 工作区文档完成: workspace_slug=%s document_count=%d",
            workspace_slug,
            len(normalized_documents),
        )
        return normalized_documents

    def find_document(
        self,
        workspace_slug: str,
        location: str,
        *,
        user_id: int | None = None,
    ) -> AnythingLLMDocument | None:
        """按规范化后的完整 ``document_ref`` 精确查找工作区文档。

        优先比较调用方位置与工作区记录位置的精确规范化结果，以兼容上传后带文档 ID
        后缀的内部路径；随后才比较逻辑文件身份。本方法不使用文件名子串或模糊包含
        关系，避免相似文档名称产生错误命中。
        """
        target_ref = normalize_document_ref(location)
        if not target_ref:
            return None
        for document in self.list_documents(workspace_slug, user_id=user_id):
            stored_location_ref = normalize_document_ref(document.location)
            if stored_location_ref == target_ref or document.document_ref == target_ref:
                logger.debug(
                    "AnythingLLM 工作区文档精确匹配: workspace_slug=%s "
                    "document_id=%s document_ref=%s",
                    workspace_slug,
                    document.id,
                    document.document_ref,
                )
                return document
        logger.debug(
            "AnythingLLM 工作区文档未匹配: workspace_slug=%s target_ref=%s",
            workspace_slug,
            target_ref,
        )
        return None

    def update_embeddings(
        self,
        workspace_slug: str,
        *,
        adds: Optional[Sequence[str]] = None,
        deletes: Optional[Sequence[str]] = None,
        user_id: int | None = None,
    ) -> AnythingLLMWorkspace | None:
        """原子更新工作区文档集合，并严格验证响应工作区身份。

        返回 ``None`` 表示规范化后没有任何有效变更，因此没有发送 HTTP 请求。真正发送
        请求时，成功响应必须包含非空 ``workspace`` 对象，且其规范化 slug 必须与目标
        slug 一致；缺失或冲突均视为协议失败。
        """
        request_payload: dict[str, list[str]] = {}
        normalized_adds = self._document_paths(adds)
        normalized_deletes = self._document_paths(deletes)
        if normalized_adds:
            request_payload["adds"] = normalized_adds
        if normalized_deletes:
            request_payload["deletes"] = normalized_deletes
        if not request_payload:
            logger.debug(
                "跳过空的 AnythingLLM 嵌入变更: workspace_slug=%s",
                workspace_slug,
            )
            return None

        target_slug = self._required_slug(workspace_slug)
        path = f"workspace/{quote(target_slug, safe='')}/update-embeddings"
        body = self._transport.post_json(path, request_payload, user_id=user_id)
        payload = require_mapping(body, context="更新嵌入响应")
        if payload.get("error"):
            raise AnythingLLMProtocolError("AnythingLLM 明确拒绝更新工作区文档")
        workspace_value = payload.get("workspace")
        if not isinstance(workspace_value, Mapping) or not workspace_value:
            raise AnythingLLMProtocolError(
                "AnythingLLM 更新嵌入响应缺少非空 workspace 对象"
            )
        workspace = AnythingLLMWorkspace.from_payload(workspace_value)
        if workspace.slug.casefold() != target_slug.casefold():
            raise AnythingLLMProtocolError(
                "AnythingLLM 更新嵌入响应的 workspace slug 与目标不一致"
            )
        logger.info(
            "AnythingLLM 工作区文档变更已接受: workspace_slug=%s add_count=%d "
            "delete_count=%d has_user_context=%s",
            workspace.slug,
            len(normalized_adds),
            len(normalized_deletes),
            user_id is not None,
        )
        return workspace

    def update_pin(
        self,
        workspace_slug: str,
        location: str,
        *,
        pinned: bool = True,
        user_id: int | None = None,
    ) -> None:
        """更新文档 Pin 状态，并严格校验 2xx JSON 中的成功消息。

        Pin 只证明工作区文档记录的固定状态已更新，不证明模型回答实际使用了该文档。
        Gateway 后续仍必须通过 ``sources`` 的 ``document_ref`` 验证来源身份。
        """
        normalized_location = normalize_document_path(location)
        if not normalized_location:
            raise ValueError("Pin 使用的文档 location 不能为空")
        path = f"workspace/{self._path_segment(workspace_slug)}/update-pin"
        body = self._transport.post_json(
            path,
            {"docPath": normalized_location, "pinStatus": bool(pinned)},
            user_id=user_id,
        )
        payload = require_mapping(body, context="更新 Pin 响应")
        if payload.get("error") or payload.get("success") is False:
            raise AnythingLLMProtocolError("AnythingLLM 明确拒绝更新文档 Pin 状态")
        message = str(payload.get("message") or "").strip().casefold()
        success_markers = ("success", "updated", "pin status", "pinned", "unpinned")
        if not message or not any(marker in message for marker in success_markers):
            raise AnythingLLMProtocolError(
                "AnythingLLM 更新 Pin 响应缺少可识别的成功消息"
            )
        logger.info(
            "AnythingLLM 文档 Pin 状态更新完成: workspace_slug=%s location=%s "
            "pinned=%s has_user_context=%s",
            workspace_slug,
            normalized_location,
            pinned,
            user_id is not None,
        )

    def vector_search(
        self,
        workspace_slug: str,
        query: str,
        *,
        top_n: int | None = None,
        user_id: int | None = None,
    ) -> list[AnythingLLMSource]:
        """执行向量检索并把不同版本的结果字段统一为来源 DTO。"""
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise ValueError("向量检索 query 不能为空")
        request_payload: dict[str, Any] = {"query": normalized_query}
        if top_n is not None:
            if top_n <= 0:
                raise ValueError("top_n 必须大于 0")
            request_payload["topN"] = top_n
        path = f"workspace/{self._path_segment(workspace_slug)}/vector-search"
        body = self._transport.post_json(path, request_payload, user_id=user_id)
        payload = require_mapping(body, context="向量检索响应")
        results = require_sequence(payload.get("results", []), context="results 字段")
        sources = [AnythingLLMSource.from_payload(item) for item in results]
        logger.debug(
            "AnythingLLM 向量检索完成: workspace_slug=%s query_chars=%d "
            "top_n=%s result_count=%d",
            workspace_slug,
            len(normalized_query),
            top_n,
            len(sources),
        )
        return sources

    def _get_workspace_payload(
        self,
        workspace_slug: str,
        *,
        user_id: int | None,
    ) -> Mapping[str, Any]:
        """读取工作区并兼容上游偶尔返回单元素数组的结构。"""
        path = f"workspace/{self._path_segment(workspace_slug)}"
        body = self._transport.get_json(path, user_id=user_id)
        payload = require_mapping(body, context="工作区详情响应")
        workspace_value = payload.get("workspace")
        if isinstance(workspace_value, list):
            workspace_value = workspace_value[0] if workspace_value else None
        return require_mapping(workspace_value, context="workspace 字段")

    @staticmethod
    def _document_paths(values: Optional[Sequence[str]]) -> list[str]:
        """规范化文档位置、移除空值并保持调用方提供的顺序。"""
        normalized: list[str] = []
        for value in values or ():
            path = normalize_document_path(value)
            if path:
                normalized.append(path)
        return normalized

    @classmethod
    def _path_segment(cls, value: str) -> str:
        """校验并编码一个不可为空的 URL 路径段。"""
        return quote(cls._required_slug(value), safe="")

    @staticmethod
    def _required_slug(value: str) -> str:
        """规范化工作区 slug，拒绝空标识。"""
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("workspace_slug 不能为空")
        return normalized
