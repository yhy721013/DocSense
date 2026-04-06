from __future__ import annotations

import json
import logging
from typing import Any, Dict, Generator, List

from app.services.utils.anythingllm_client import AnythingLLMClient
from app.services.core.database import ChatDatabaseService, DatabaseService

logger = logging.getLogger(__name__)


# ── SSE 格式化 ──────────────────────────────────────────────

def _format_sse_event(event: str, data: dict) -> str:
    """将事件名和数据格式化为 SSE 文本行。"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _build_doc_path(anything_doc_id: str) -> str:
    """根据 anything_doc_id 拼接 AnythingLLM 文档路径。"""
    return f"custom-documents/{anything_doc_id}.json"


# ── 对话主流程 ──────────────────────────────────────────────

def handle_chat_stream(
    *,
    chat_db: ChatDatabaseService,
    kb_service: DatabaseService,
    client: AnythingLLMClient,
    chat_id: str,
    file_names: List[str],
    message: str,
) -> Generator[str, None, None]:
    """SSE 流式对话生成器。

    流程：
    1. 判断新/旧对话
    2. 新对话 → 创建 Workspace + 引用文档 + 创建 Thread
    3. 旧对话 → 增量 update-embeddings（若 fileNames 变更）
    4. yield chatInfo → textChunk × N → done
    """
    try:
        existing_chat = chat_db.get_chat(chat_id)

        if existing_chat is None:
            # ── 新对话 ──
            workspace_info = client.create_chat_workspace(f"chat-{chat_id}")
            if not workspace_info:
                yield _format_sse_event("error", {"error": "创建对话工作区失败"})
                return

            workspace_slug = workspace_info.get("slug") or str(workspace_info.get("id"))

            # 跨 Workspace 引用文档
            doc_paths = _resolve_doc_paths(kb_service, file_names)
            if doc_paths:
                success = client.update_embeddings_batch(workspace_slug, adds=doc_paths)
                if not success:
                    yield _format_sse_event("error", {"error": "在工作区中引用文件失败"})
                    return

            thread_info = client.create_thread(workspace_slug, f"thread-{chat_id}")
            if not thread_info:
                yield _format_sse_event("error", {"error": "创建对话线程失败"})
                return
            thread_slug = client.extract_thread_slug(thread_info) or str(thread_info.get("id"))

            chat_db.create_chat(chat_id, file_names, workspace_slug, thread_slug)
            is_new_chat = True
        else:
            # ── 继续对话 ──
            workspace_slug = existing_chat["workspace_slug"]
            thread_slug = existing_chat["thread_slug"]

            # 对比 fileNames，增量更新嵌入
            old_set = set(existing_chat["file_names"])
            new_set = set(file_names)
            to_add = new_set - old_set
            to_remove = old_set - new_set

            if to_add or to_remove:
                add_paths = _resolve_doc_paths(kb_service, list(to_add)) if to_add else []
                remove_paths = _resolve_doc_paths(kb_service, list(to_remove)) if to_remove else []
                success = client.update_embeddings_batch(workspace_slug, adds=add_paths or None, deletes=remove_paths or None)
                if not success:
                    yield _format_sse_event("error", {"error": "更新工作区文件引用失败"})
                    return
                chat_db.update_file_names(chat_id, file_names)

            is_new_chat = False

        # ── 推送 chatInfo ──
        yield _format_sse_event("chatInfo", {"chatId": chat_id, "isNewChat": is_new_chat})

        # ── 流式对话 ──
        for chunk in client.stream_chat_to_thread(workspace_slug, thread_slug, message):
            yield _format_sse_event("textChunk", {"content": chunk})

        # ── 完成 ──
        yield _format_sse_event("done", {"chatId": chat_id})

    except Exception as e:
        logger.exception("对话流处理异常: chat_id=%s, error=%s", chat_id, e)
        yield _format_sse_event("error", {"error": f"大模型服务响应异常: {e}"})


# ── 获取对话历史 ──────────────────────────────────────────

def get_chat_history(
    *,
    chat_db: ChatDatabaseService,
    client: AnythingLLMClient,
    chat_id: str,
) -> Dict[str, Any]:
    """从 AnythingLLM Thread 获取完整对话历史。"""
    chat_record = chat_db.get_chat(chat_id)
    if chat_record is None:
        raise ChatNotFoundError(chat_id)

    raw_history = client.get_thread_chats(
        chat_record["workspace_slug"],
        chat_record["thread_slug"],
    )

    messages = []
    for item in raw_history:
        role = item.get("role")
        content = item.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    return {
        "chatId": chat_id,
        "fileNames": chat_record["file_names"],
        "messages": messages,
    }


# ── 删除对话 ──────────────────────────────────────────────

def delete_chat(
    *,
    chat_db: ChatDatabaseService,
    client: AnythingLLMClient,
    chat_id: str,
) -> None:
    """删除对话：清理 AnythingLLM 资源 + 删除数据库记录。"""
    chat_record = chat_db.get_chat(chat_id)
    if chat_record is None:
        raise ChatNotFoundError(chat_id)

    workspace_slug = chat_record["workspace_slug"]
    thread_slug = chat_record["thread_slug"]

    # 尽力删除 AnythingLLM 资源，失败不阻塞
    try:
        client.delete_thread(workspace_slug, thread_slug)
    except Exception as e:
        logger.warning("删除 Thread 失败（继续）: %s", e)

    try:
        client.delete_workspace(workspace_slug)
    except Exception as e:
        logger.warning("删除 Workspace 失败（继续）: %s", e)

    chat_db.delete_chat(chat_id)


# ── 辅助函数 ──────────────────────────────────────────────

def _resolve_doc_paths(kb_service: DatabaseService, file_names: List[str]) -> List[str]:
    """根据 fileName 列表查询 documents 表，返回 doc_path 列表。

    优先使用文件解析时保存的完整 doc_path，缺失时回退到 anything_doc_id 拼接。
    """
    paths = []
    for file_name in file_names:
        record = kb_service.get_document_record(file_name)
        if not record:
            continue
        # 优先使用已保存的完整路径
        doc_path = record.get("doc_path")
        if doc_path:
            paths.append(doc_path)
        elif record.get("anything_doc_id"):
            paths.append(_build_doc_path(record["anything_doc_id"]))
    return paths


# ── 异常类 ──────────────────────────────────────────────

class ChatNotFoundError(Exception):
    """对话不存在时抛出。"""
    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        super().__init__(f"对话不存在: {chat_id}")
