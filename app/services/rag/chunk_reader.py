"""
文档 chunk 读取器。

从 AnythingLLM 存储目录直接读取 workspace 下所有文档的全量文本，
并将其切分为适合 BM25 索引的 chunk 列表。

v3.0 新增：解决原 _fetch_all_chunks() 仅通过单次 vector_search 获取少量 chunks 的问题，
改为从存储目录读取文档 JSON 文件，获取准全量文本构建 BM25 索引。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# BM25 索引分块参数
DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 100


class DocumentChunkReader:
    """从 AnythingLLM 存储目录读取文档 chunks。

    优先从本地存储目录读取文档 JSON 文件（包含 pageContent 全量文本），
    若存储目录不可用（如远程部署），则降级为多次 vector_search 获取准全量 chunks。
    """

    def fetch_workspace_chunks(
        self,
        client: Any,
        workspace_slug: str,
        user_id: int = 1,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> List[Dict[str, Any]]:
        """获取 workspace 中所有文档的 chunks。

        Args:
            client: AnythingLLMClient 实例
            workspace_slug: 工作区标识
            user_id: 用户 ID
            chunk_size: 单个 chunk 最大字符数
            chunk_overlap: chunk 之间的重叠字符数

        Returns:
            chunk 列表，每个元素为 {"id": str, "text": str}
        """
        # 方案 1: 从存储目录读取文档 JSON 文件
        chunks = self._read_chunks_from_storage(client, workspace_slug, user_id, chunk_size, chunk_overlap)
        if chunks:
            logger.info(
                "从存储目录读取 workspace=%s 的文档 chunks: %d 个",
                workspace_slug,
                len(chunks),
            )
            return chunks

        # 方案 2: 降级为多次 vector_search 获取准全量 chunks
        logger.warning(
            "无法从存储目录读取 chunks（workspace=%s），降级为 vector_search 方式",
            workspace_slug,
        )
        chunks = self._fetch_chunks_via_vector_search(client, workspace_slug, user_id)
        if chunks:
            logger.info(
                "通过 vector_search 获取 workspace=%s 的 chunks: %d 个",
                workspace_slug,
                len(chunks),
            )
        return chunks

    def _read_chunks_from_storage(
        self,
        client: Any,
        workspace_slug: str,
        user_id: int,
        chunk_size: int,
        chunk_overlap: int,
    ) -> List[Dict[str, Any]]:
        """从 AnythingLLM 存储目录读取文档 JSON 文件并切分为 chunks。

        Args:
            client: AnythingLLMClient 实例
            workspace_slug: 工作区标识
            user_id: 用户 ID
            chunk_size: 单个 chunk 最大字符数
            chunk_overlap: chunk 之间的重叠字符数

        Returns:
            chunk 列表，空列表表示无法从存储目录读取
        """
        # 获取存储根路径
        resolve_storage_root = getattr(client, "_resolve_storage_root", None)
        if not callable(resolve_storage_root):
            return []

        storage_root = resolve_storage_root()
        if not storage_root:
            return []

        documents_root = os.path.normpath(os.path.join(storage_root, "documents"))
        if not os.path.isdir(documents_root):
            logger.debug("AnythingLLM documents 目录不存在: %s", documents_root)
            return []

        # 获取 workspace 中的文档列表
        docs = self._list_workspace_documents(client, workspace_slug, user_id)
        if not docs:
            logger.debug("workspace=%s 中没有文档", workspace_slug)
            return []

        chunks: List[Dict[str, Any]] = []

        for doc_info in docs:
            doc_path = str(
                doc_info.get("docpath")
                or doc_info.get("location")
                or ""
            ).replace("\\", "/").strip()
            if not doc_path:
                continue

            # 构建文档 JSON 文件的完整路径
            full_path = os.path.normpath(os.path.join(documents_root, doc_path))

            # 安全检查：确保路径在 documents_root 下
            try:
                documents_root_abs = os.path.abspath(documents_root)
                full_path_abs = os.path.abspath(full_path)
                if os.name == "nt":
                    root_drive, _ = os.path.splitdrive(documents_root_abs)
                    target_drive, _ = os.path.splitdrive(full_path_abs)
                    if root_drive.lower() != target_drive.lower():
                        continue
                if os.path.commonpath([documents_root_abs, full_path_abs]) != documents_root_abs:
                    logger.warning("跳过异常路径的文档: %s", doc_path)
                    continue
            except (ValueError, OSError):
                continue

            if not os.path.exists(full_path):
                logger.debug("文档文件不存在: %s", full_path)
                continue

            # 读取文档 JSON 文件
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    doc_data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("读取文档 %s 失败: %s", doc_path, e)
                continue

            # 提取文本内容
            page_content = doc_data.get("pageContent", "")
            if not page_content or not isinstance(page_content, str):
                # pageContent 可能是 list（多页文档）
                if isinstance(page_content, list):
                    page_content = "\n\n".join(str(p) for p in page_content if p)
                else:
                    page_content = str(page_content) if page_content else ""

            if not page_content.strip():
                continue

            # 切分为 chunks
            doc_chunks = self._split_text_into_chunks(
                page_content,
                doc_id=doc_path,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
            chunks.extend(doc_chunks)

        return chunks

    def _list_workspace_documents(
        self,
        client: Any,
        workspace_slug: str,
        user_id: int,
    ) -> List[Dict[str, Any]]:
        """通过 API 获取 workspace 中的文档列表。

        Args:
            client: AnythingLLMClient 实例
            workspace_slug: 工作区标识
            user_id: 用户 ID

        Returns:
            文档信息列表
        """
        url = f"{client.config.base_url}/workspace/{workspace_slug}"
        try:
            resp = client.session.get(
                url,
                headers=client._json_headers(user_id),
                timeout=client.config.timeout,
            )
            if not resp.ok:
                logger.error(
                    "获取工作区 %s 文档列表失败: %s %s",
                    workspace_slug,
                    resp.status_code,
                    resp.text,
                )
                return []
            workspace = resp.json().get("workspace")
            if isinstance(workspace, list):
                workspace = workspace[0] if workspace else None
            if not isinstance(workspace, dict):
                return []
            docs = workspace.get("documents", [])
            return docs if isinstance(docs, list) else []
        except Exception as e:
            logger.error("获取工作区 %s 文档列表异常: %s", workspace_slug, e)
            return []

    @staticmethod
    def _split_text_into_chunks(
        text: str,
        doc_id: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> List[Dict[str, Any]]:
        """将文本切分为适合 BM25 索引的 chunk 列表。

        切分策略：
        1. 优先按段落（双换行）切分
        2. 超长段落按 chunk_size 切分，保留 chunk_overlap 重叠
        3. 每个 chunk 获得唯一 ID：{doc_id}#chunk-{idx}

        Args:
            text: 原始文本
            doc_id: 文档标识
            chunk_size: 单个 chunk 最大字符数
            chunk_overlap: chunk 之间的重叠字符数

        Returns:
            chunk 列表，每个元素为 {"id": str, "text": str}
        """
        if not text or not text.strip():
            return []

        # 按段落切分
        paragraphs = re.split(r'\n\s*\n', text.strip())
        paragraphs = [p.strip() for p in paragraphs if p.strip()]

        chunks: List[Dict[str, Any]] = []
        chunk_idx = 0

        for para in paragraphs:
            if len(para) <= chunk_size:
                # 段落不超过 chunk_size，直接作为一个 chunk
                chunks.append({
                    "id": f"{doc_id}#chunk-{chunk_idx}",
                    "text": para,
                })
                chunk_idx += 1
            else:
                # 段落超过 chunk_size，按滑动窗口切分
                start = 0
                while start < len(para):
                    end = start + chunk_size
                    chunk_text = para[start:end].strip()
                    if chunk_text:
                        chunks.append({
                            "id": f"{doc_id}#chunk-{chunk_idx}",
                            "text": chunk_text,
                        })
                        chunk_idx += 1
                    # 移动窗口（保留重叠）
                    start = end - chunk_overlap
                    if start >= len(para):
                        break

        return chunks

    @staticmethod
    def _fetch_chunks_via_vector_search(
        client: Any,
        workspace_slug: str,
        user_id: int,
    ) -> List[Dict[str, Any]]:
        """降级方案：通过多次 vector_search 获取准全量 chunks。

        使用多个通用查询词尝试获取尽可能多的 chunks，然后去重。

        Args:
            client: AnythingLLMClient 实例
            workspace_slug: 工作区标识
            user_id: 用户 ID

        Returns:
            chunk 列表
        """
        # 使用多个通用查询词获取更多结果
        generic_queries = [" ", "the", "的", "a", "是", "system", "data"]
        all_chunks: Dict[str, Dict[str, Any]] = {}

        for query in generic_queries:
            try:
                results = client.vector_search(workspace_slug, query, user_id=user_id)
                if not results:
                    continue
                for chunk in results:
                    if not isinstance(chunk, dict):
                        continue
                    chunk_id = chunk.get("id") or chunk.get("docId")
                    if chunk_id and str(chunk_id) not in all_chunks:
                        all_chunks[str(chunk_id)] = chunk
            except Exception as e:
                logger.warning("vector_search 获取 chunks 失败 (query='%s'): %s", query, e)

        return list(all_chunks.values())
