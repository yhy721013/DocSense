"""AnythingLLM 迁移期兼容 Facade。

新代码不得继续依赖本类，应根据职责使用 ``app.integrations.anythingllm`` 下的原子客户端
或后续业务 Port。该 Facade 暂时保留旧方法签名、字典返回值和失败默认值，将调用委托给
共享同一个任务级 Transport 的 Document、Workspace 与 Thread Client。

阶段 3 完成前，``session``、``config``、``_build_headers`` 和 ``_json_headers`` 仍保留，
仅用于兼容尚未迁移的旧业务代码；它们不是新的稳定接口。
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Iterator, Mapping, Optional, Sequence

from requests import Session

from app.integrations.anythingllm.documents import AnythingLLMDocumentClient
from app.integrations.anythingllm.models import (
    AnythingLLMAnswer,
    AnythingLLMDocument,
    AnythingLLMSource,
    AnythingLLMThread,
    AnythingLLMWorkspace,
    normalize_document_path,
)
from app.integrations.anythingllm.policies import (
    chat_workspace_settings,
    document_rag_workspace_settings,
)
from app.integrations.anythingllm.threads import AnythingLLMThreadClient
from app.integrations.anythingllm.transport import AnythingLLMTransport
from app.integrations.anythingllm.workspaces import AnythingLLMWorkspaceClient
from app.services.core.config import AnythingLLMConfig


logger = logging.getLogger(__name__)


def _rag_workspace_settings() -> dict[str, Any]:
    """兼容旧调用方，并复用新集成层的唯一文档 RAG 策略。"""
    return dict(document_rag_workspace_settings())


def _chat_workspace_settings() -> dict[str, Any]:
    """返回旧对话工作区使用的独立配置字典。"""
    return dict(chat_workspace_settings())


@dataclass
class AnythingLLMClient:
    """把旧接口委托给三个原子客户端的迁移期兼容对象。

    一个实例创建一个 ``requests.Session`` 和一个 ``AnythingLLMTransport``。三个原子
    Client 共享该 Transport，但彼此之间不持有引用，也不会相互调用。Facade 负责把 DTO
    转回旧代码期望的字典，并把新异常转换为旧接口的 ``None``、``False`` 或空列表。
    """

    config: AnythingLLMConfig

    def __post_init__(self) -> None:
        """创建任务独占会话、传输对象和三个原子客户端。"""
        self.session = Session()
        try:
            self._transport = AnythingLLMTransport(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                timeout=self.config.timeout,
                session=self.session,
            )
        except Exception:
            # Transport 构造失败时所有权尚未完成转移，Facade 必须主动关闭已创建的会话。
            self.session.close()
            raise
        self.documents = AnythingLLMDocumentClient(self._transport)
        self.workspaces = AnythingLLMWorkspaceClient(self._transport)
        self.threads = AnythingLLMThreadClient(self._transport)

    def __enter__(self) -> "AnythingLLMClient":
        """返回当前 Facade，便于新迁移代码显式限定资源生命周期。"""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出上下文时关闭唯一的任务级 Transport 和 Session。"""
        self.close()

    def close(self) -> None:
        """幂等关闭任务级传输对象；关闭后不可继续复用该 Facade。"""
        self._transport.close()

    def _build_headers(self, user_id: Optional[int] = None) -> dict[str, str]:
        """构造旧业务直接 HTTP 调用所需请求头；阶段 3 将删除该兼容入口。"""
        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }
        if user_id is not None:
            headers["X-AnythingLLM-User-Id"] = str(user_id)
        return headers

    def _json_headers(self, user_id: Optional[int] = None) -> dict[str, str]:
        """构造旧业务直接 JSON HTTP 调用所需请求头；仅供迁移期兼容。"""
        headers = self._build_headers(user_id)
        headers["Content-Type"] = "application/json"
        return headers

    def list_workspaces(self, user_id: Optional[int] = None) -> list[dict[str, Any]]:
        """兼容旧接口：返回工作区字典列表，失败时返回空列表。"""
        try:
            return [self._workspace_dict(item) for item in self.workspaces.list_workspaces(
                user_id=user_id
            )]
        except Exception as exc:
            logger.error(
                "获取 AnythingLLM 工作区列表失败: error_type=%s",
                type(exc).__name__,
            )
            return []

    def find_workspace_by_name(
        self,
        name: str,
        user_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """兼容旧接口：按名称精确查找第一个工作区。"""
        for workspace in self.list_workspaces(user_id):
            if workspace.get("name") == name:
                return workspace
        return None

    def create_rag_workspace(
        self,
        name: str,
        user_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """兼容旧接口：使用既有文档抽取配置创建工作区。"""
        return self._create_workspace(name, _rag_workspace_settings(), user_id=user_id)

    def create_chat_workspace(
        self,
        name: str,
        user_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """兼容旧接口：使用既有通用对话配置创建工作区。"""
        return self._create_workspace(name, _chat_workspace_settings(), user_id=user_id)

    def ensure_workspace(
        self,
        name: str,
        user_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """兼容旧接口：优先复用同名工作区，否则创建 RAG 工作区。"""
        existing = self.find_workspace_by_name(name, user_id=user_id)
        return existing or self.create_rag_workspace(name, user_id=user_id)

    def create_thread(
        self,
        workspace_slug: str,
        thread_name: str,
        user_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """兼容旧接口：创建线程并返回包含历史 slug 别名的字典。"""
        normalized_name = str(thread_name or "").strip() or f"thread-{int(time.time())}"
        try:
            thread = self.threads.create_thread(
                workspace_slug,
                normalized_name,
                user_id=user_id,
            )
            return self._thread_dict(thread)
        except Exception as exc:
            logger.error(
                "创建 AnythingLLM 会话失败: thread_name_chars=%d error_type=%s",
                len(normalized_name),
                type(exc).__name__,
            )
            return None

    @staticmethod
    def extract_thread_slug(info: Mapping[str, Any]) -> Optional[str]:
        """兼容原始字典和规范化字典中的线程 slug 字段。"""
        for key in ("slug", "threadSlug", "thread_slug", "id"):
            value = info.get(key)
            if value:
                return str(value)
        return None

    def send_prompt_to_thread(
        self,
        workspace_slug: str,
        thread_slug: str,
        prompt: str,
        user_id: Optional[int] = None,
        document_ids: Optional[Sequence[str]] = None,
        mode: str = "chat",
    ) -> Optional[dict[str, Any]]:
        """兼容旧同步问答接口，返回清理文本、原始文本和规范化来源字典。"""
        try:
            answer = self.threads.ask(
                workspace_slug,
                thread_slug,
                prompt,
                user_id=user_id,
                document_ids=document_ids,
                mode=mode,
            )
            return self._answer_dict(answer)
        except Exception as exc:
            logger.error(
                "向 AnythingLLM 会话发送提问失败: prompt_chars=%d error_type=%s",
                len(prompt or ""),
                type(exc).__name__,
            )
            return None

    def delete_thread(
        self,
        workspace_slug: str,
        thread_slug: str,
        user_id: Optional[int] = None,
    ) -> bool:
        """兼容旧接口：删除线程，失败时返回 ``False``。"""
        try:
            self.threads.delete_thread(workspace_slug, thread_slug, user_id=user_id)
            return True
        except Exception as exc:
            logger.error(
                "删除 AnythingLLM 会话失败: error_type=%s",
                type(exc).__name__,
            )
            return False

    def vector_search(
        self,
        workspace_slug: str,
        query: str,
        user_id: Optional[int] = None,
        *,
        top_n: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """执行向量检索并返回兼容来源字典，失败时返回空列表。

        ``top_n`` 和 ``score_threshold`` 是 Python 内部适配参数，不属于 DocSense HTTP
        接口。后者只供显式校准或未来冻结 profile 的 Adapter 使用；缺省时不会发送
        ``scoreThreshold``，因此既有业务继续沿用 workspace 配置。
        """
        try:
            keyword_arguments: dict[str, Any] = {
                "top_n": top_n,
                "user_id": user_id,
            }
            if score_threshold is not None:
                keyword_arguments["score_threshold"] = score_threshold
            sources = self.workspaces.vector_search(
                workspace_slug,
                query,
                **keyword_arguments,
            )
            return [self._source_dict(source) for source in sources]
        except Exception as exc:
            logger.error(
                "AnythingLLM 向量检索失败: query_chars=%d top_n=%s "
                "has_score_threshold=%s error_type=%s",
                len(query or ""),
                top_n,
                score_threshold is not None,
                type(exc).__name__,
            )
            return []

    def list_workspace_documents(
        self,
        workspace_slug: str,
        user_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """获取工作区文档并返回兼容字段字典，失败时返回空列表。

        该公开方法用于替代业务层直接访问 Workspace HTTP 详情接口。返回结果同时包含
        ``id/docId`` 与 ``location/docpath``，使迁移中的旧调用方无需继续解析供应商字段
        别名；新代码应优先直接使用 Workspace Client 返回的 DTO。
        """
        try:
            documents = self.workspaces.list_documents(
                workspace_slug,
                user_id=user_id,
            )
            return [self._document_dict(document) for document in documents]
        except Exception as exc:
            logger.error(
                "获取 AnythingLLM 工作区文档列表失败: error_type=%s",
                type(exc).__name__,
            )
            return []

    def upload_document(
        self,
        file_path: str,
        user_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """兼容旧接口：上传文档并同时提供新旧字段名。"""
        try:
            document = self.documents.upload_document(file_path, user_id=user_id)
            return self._document_dict(document)
        except Exception as exc:
            logger.error(
                "上传 AnythingLLM 文档失败: file_name=%s error_type=%s",
                os.path.basename(file_path),
                type(exc).__name__,
            )
            return None

    def fetch_workspace_document(
        self,
        workspace_slug: str,
        doc_path: str,
        user_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """兼容旧接口：按完整规范化位置精确查找工作区文档。"""
        try:
            document = self.workspaces.find_document(
                workspace_slug,
                doc_path,
                user_id=user_id,
            )
            return self._document_dict(document) if document is not None else None
        except Exception as exc:
            logger.error(
                "查找 AnythingLLM 工作区文档失败: has_document_path=%s error_type=%s",
                bool(doc_path),
                type(exc).__name__,
            )
            return None

    def wait_for_processing(
        self,
        doc_relative_path: str,
        retries: int = 300,
        delay: float = 2.0,
    ) -> bool:
        """兼容旧本地部署：轮询 AnythingLLM storage 中的解析结果文件。

        该方法不是 HTTP 原子客户端能力。纯方案 B 后续不再依赖本地 storage 反查，待旧
        RAG pipeline 完成迁移后删除。当前保留原安全边界与“目录不可用时跳过”等价语义。
        """
        storage_root = self._resolve_storage_root()
        if not storage_root:
            logger.warning("未配置可用的 AnythingLLM storage 根路径，跳过处理等待")
            return True

        documents_root = os.path.normpath(os.path.join(storage_root, "documents"))
        if not os.path.isdir(documents_root):
            logger.warning("AnythingLLM 文档目录不可用，跳过解析等待")
            return True

        safe_relative_path = str(doc_relative_path or "").replace("\\", "/").strip("/")
        target_path = os.path.normpath(os.path.join(documents_root, safe_relative_path))
        documents_root_abs = os.path.abspath(documents_root)
        target_abs = os.path.abspath(target_path)

        # Windows 不同盘符无法使用 commonpath 比较，先显式拒绝跨盘路径。
        if os.name == "nt":
            root_drive, _ = os.path.splitdrive(documents_root_abs)
            target_drive, _ = os.path.splitdrive(target_abs)
            if root_drive.casefold() != target_drive.casefold():
                logger.warning("文档路径跨盘符，拒绝等待解析结果")
                return False
        try:
            if os.path.commonpath([documents_root_abs, target_abs]) != documents_root_abs:
                logger.warning("文档路径不在允许目录内，拒绝等待解析结果")
                return False
        except ValueError:
            logger.warning("文档路径无法安全比较，拒绝等待解析结果")
            return False

        for _ in range(max(0, retries)):
            if os.path.exists(target_path):
                return True
            time.sleep(max(0.0, delay))
        return False

    def update_embeddings(
        self,
        doc_path: str,
        workspace_slug: str,
        user_id: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """兼容旧复合操作：加入文档后尽力执行 Pin，并忽略供应商业务元数据回写。

        新 ``WorkspaceClient.update_embeddings`` 本身不会隐式编排其他客户端；这里的后续
        Pin 仅为保持旧调用行为。加入文档失败会返回 ``False``，Pin 失败仍保持旧版
        best-effort 语义并记录警告。``metadata`` 只保留签名兼容，不再发送不存在的
        ``/document/meta`` 请求。
        """
        cleaned_path = normalize_document_path(doc_path)
        if not cleaned_path:
            return False
        try:
            self.workspaces.update_embeddings(
                workspace_slug,
                adds=[cleaned_path],
                user_id=user_id,
            )
        except Exception as exc:
            logger.error(
                "将文档加入 AnythingLLM 工作区失败: error_type=%s",
                type(exc).__name__,
            )
            return False

        try:
            self.workspaces.update_pin(
                workspace_slug,
                cleaned_path,
                user_id=user_id,
            )
        except Exception as exc:
            logger.warning(
                "更新 AnythingLLM 文档固定状态失败，不影响已加入工作区的文档: "
                "error_type=%s",
                type(exc).__name__,
            )
        if metadata:
            # 当前 AnythingLLM Developer API 不提供上传后更新文档元数据的稳定端点。旧流程
            # 继续接收该参数仅为保持调用签名兼容，但业务元数据必须由 DocSense 本地数据库
            # 持久化；这里禁止再调用已确认返回 404 的 /document/meta。
            logger.debug(
                "跳过 AnythingLLM 上传后元数据更新，本地数据库负责保存业务元数据: "
                "metadata_key_count=%d",
                len(metadata),
            )
        return True

    def update_embeddings_batch(
        self,
        workspace_slug: str,
        adds: Optional[Sequence[str]] = None,
        deletes: Optional[Sequence[str]] = None,
        user_id: Optional[int] = None,
    ) -> bool:
        """兼容旧接口：批量增删工作区文档，空变更直接成功。"""
        try:
            self.workspaces.update_embeddings(
                workspace_slug,
                adds=adds,
                deletes=deletes,
                user_id=user_id,
            )
            return True
        except Exception as exc:
            logger.error(
                "批量更新 AnythingLLM 工作区文档失败: add_count=%d delete_count=%d "
                "error_type=%s",
                len(adds or ()),
                len(deletes or ()),
                type(exc).__name__,
            )
            return False

    def stream_chat_to_thread(
        self,
        workspace_slug: str,
        thread_slug: str,
        message: str,
        user_id: Optional[int] = None,
        mode: str = "query",
        document_ids: Optional[Sequence[str]] = None,
    ) -> Iterator[str]:
        """兼容旧流式接口，将集成层异常转换为 ``RuntimeError``。"""
        try:
            yield from self.threads.stream(
                workspace_slug,
                thread_slug,
                message,
                user_id=user_id,
                mode=mode,
                document_ids=document_ids,
            )
        except RuntimeError:
            raise
        except Exception as exc:
            logger.error(
                "AnythingLLM 流式对话失败: message_chars=%d error_type=%s",
                len(message or ""),
                type(exc).__name__,
            )
            raise RuntimeError(f"流式对话异常: {exc}") from exc

    def get_thread_chats(
        self,
        workspace_slug: str,
        thread_slug: str,
        user_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """兼容旧接口：返回线程历史字典列表，失败时返回空列表。"""
        try:
            return [
                dict(item)
                for item in self.threads.history(
                    workspace_slug,
                    thread_slug,
                    user_id=user_id,
                )
            ]
        except Exception as exc:
            logger.error(
                "获取 AnythingLLM 会话历史失败: error_type=%s",
                type(exc).__name__,
            )
            return []

    def delete_workspace(
        self,
        workspace_slug: str,
        user_id: Optional[int] = None,
    ) -> bool:
        """兼容旧接口：删除工作区，失败时返回 ``False``。"""
        try:
            self.workspaces.delete_workspace(workspace_slug, user_id=user_id)
            return True
        except Exception as exc:
            logger.error(
                "删除 AnythingLLM 工作区失败: error_type=%s",
                type(exc).__name__,
            )
            return False

    def _create_workspace(
        self,
        name: str,
        settings: Mapping[str, Any],
        *,
        user_id: Optional[int],
    ) -> Optional[dict[str, Any]]:
        """执行两类旧创建方法共享的委托与错误兼容流程。"""
        try:
            workspace = self.workspaces.create_workspace(
                name,
                settings=settings,
                user_id=user_id,
            )
            return self._workspace_dict(workspace)
        except Exception as exc:
            logger.error(
                "创建 AnythingLLM 工作区失败: workspace_name_chars=%d error_type=%s",
                len(name or ""),
                type(exc).__name__,
            )
            return None

    def _resolve_storage_root(self) -> Optional[str]:
        """解析旧本地轮询使用的 storage 根目录，不检查远程部署文件系统。"""
        configured_root = str(self.config.storage_root or "").strip()
        if configured_root:
            return configured_root

        candidates: list[str] = []
        if os.name == "nt":
            appdata = os.getenv("APPDATA", "").strip()
            if appdata:
                candidates.append(os.path.join(appdata, "anythingllm-desktop", "storage"))
        elif sys.platform == "darwin":
            candidates.append(
                os.path.expanduser("~/Library/Application Support/anythingllm-desktop/storage")
            )
        else:
            xdg_config_home = os.getenv("XDG_CONFIG_HOME", "").strip()
            if xdg_config_home:
                candidates.append(
                    os.path.join(xdg_config_home, "anythingllm-desktop", "storage")
                )
            candidates.append(os.path.expanduser("~/.config/anythingllm-desktop/storage"))
        candidates.append(os.path.expanduser("~/.anythingllm/storage"))

        for candidate in candidates:
            if candidate and os.path.isdir(candidate):
                return candidate
        return next((candidate for candidate in candidates if candidate), None)

    @staticmethod
    def _clean_doc_path(doc_path: str) -> str:
        """保留旧私有方法名称，并委托给适配层统一路径规范化函数。"""
        return normalize_document_path(doc_path)

    @staticmethod
    def _workspace_dict(workspace: AnythingLLMWorkspace) -> dict[str, Any]:
        """把统一工作区 DTO 转换为旧调用方期望的字典。"""
        return {"id": workspace.id, "slug": workspace.slug, "name": workspace.name}

    @staticmethod
    def _thread_dict(thread: AnythingLLMThread) -> dict[str, Any]:
        """把统一线程 DTO 转换为同时包含历史 slug 别名的字典。"""
        return {
            "id": thread.id,
            "slug": thread.slug,
            "threadSlug": thread.slug,
            "thread_slug": thread.slug,
        }

    @staticmethod
    def _document_dict(document: AnythingLLMDocument) -> dict[str, Any]:
        """把统一文档 DTO 转换为同时包含历史字段别名的字典。"""
        return {
            "id": document.id,
            "docId": document.id,
            "location": document.location,
            "docpath": document.location,
            "title": document.title,
            "document_ref": document.document_ref,
        }

    @staticmethod
    def _source_dict(source: AnythingLLMSource) -> dict[str, Any]:
        """把统一来源 DTO 转换为可序列化的兼容字典。"""
        return {
            "document_ref": source.document_ref,
            "text": source.text,
            "id": source.id,
            "title": source.title,
            "url": source.url,
            "score": source.score,
            "distance": source.distance,
            "metadata": dict(source.metadata or {}),
        }

    @classmethod
    def _answer_dict(cls, answer: AnythingLLMAnswer) -> dict[str, Any]:
        """把统一回答 DTO 转换为旧同步问答返回结构。"""
        return {
            "textResponse": answer.text,
            "rawTextResponse": answer.raw_text,
            "sources": [cls._source_dict(source) for source in answer.sources],
        }
