from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.settings import UPLOAD_DIR


def _upload_root_label() -> str:
    try:
        return UPLOAD_DIR.relative_to(UPLOAD_DIR.parent).as_posix()
    except ValueError:
        return UPLOAD_DIR.name


def list_uploaded_files() -> List[Dict[str, Any]]:
    """获取文件存储目录中的所有文件列表。
    返回格式：
    [
        {
            "path": "相对路径，如 '<存储目录>/军事基地/文件.pdf'",
            "name": "文件名，如 '文件.pdf'", 
            "size": 文件大小(字节),
            "modified": "修改时间，如 '2024-01-01 12:00:00'"
        },
        ...
    ]
    Raises:
        Exception: 当文件系统操作失败时抛出异常
    """
    files = []
    
    # 递归扫描存储目录
    for file_path in UPLOAD_DIR.rglob("*"):
        # 只处理文件，跳过目录
        if not file_path.is_file():
            continue

        # 跳过隐藏目录
        relative_parts = file_path.relative_to(UPLOAD_DIR).parts
        if any(part.startswith(".") for part in relative_parts):
            continue
        
        # 过滤掉临时文件和系统文件
        if (file_path.name.startswith("temp_") or 
            file_path.name.startswith(".") or
            file_path.name.startswith("~")):
            continue
        
        # 获取相对路径（相对于项目根目录）
        relative_path = file_path.relative_to(UPLOAD_DIR.parent)
        
        # 获取文件信息
        stat = file_path.stat()
        size = stat.st_size
        modified_time = datetime.fromtimestamp(stat.st_mtime)
        
        files.append({
            "path": str(relative_path),
            "name": file_path.name,
            "size": size,
            "modified": modified_time.strftime("%Y-%m-%d %H:%M:%S")
        })
    
    # 按修改时间倒序排列（最新的在前面）
    files.sort(key=lambda x: x["modified"], reverse=True)
    
    return files


def setup_chat_workspace(file_paths: List[str], user_id: int = 1) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[List[str]]]:
    """根据选定的文件创建对话工作区。
    Args:
        file_paths: 文件路径列表（相对于项目根目录，如 '<存储目录>/军事基地/文件.pdf'）
        user_id: 用户ID
    Returns:
        (workspace_slug, thread_slug, error_message, document_ids)
        成功时返回workspace_slug、thread_slug、None、document_ids列表
        失败时返回None, None, error_message, None
    """
    from config import load_anythingllm_config
    from anythingllm_client import AnythingLLMClient
    from pipeline import prepare_upload_files
    import time
    
    try:
        # 1. 创建AnythingLLM客户端
        client = AnythingLLMClient(load_anythingllm_config())
        
        # 2. 生成唯一的workspace名称
        timestamp = int(time.time() * 1000)
        workspace_name = f"chat_workspace_{timestamp}"
        thread_name = f"对话会话_{timestamp}"
        
        # 3. 标准化文件路径（将反斜杠转换为正斜杠）
        normalized_file_paths = []
        for file_path in file_paths:
            # 标准化路径分隔符
            normalized_path = file_path.replace('\\', '/')
            normalized_file_paths.append(normalized_path)
        
        # 4. 准备文件路径（转换为绝对路径）
        absolute_file_paths = []
        upload_root = UPLOAD_DIR.resolve()
        upload_prefix = _upload_root_label().rstrip("/") + "/"
        
        for file_path in normalized_file_paths:
            # file_path 是相对于项目根目录的路径，如 '<存储目录>/军事基地/文件.pdf'
            # 我们需要将其转换为相对于UPLOAD_DIR的路径
            if file_path.startswith(upload_prefix):
                relative_to_upload = file_path[len(upload_prefix):]
                # 通过 resolve 标准化路径，并确保其仍位于 UPLOAD_DIR 下，防止路径遍历
                absolute_path = (UPLOAD_DIR / relative_to_upload).resolve()
                try:
                    absolute_path.relative_to(upload_root)
                except ValueError:
                    return None, None, f"无效的文件路径: {file_path}", None
                if absolute_path.exists():
                    absolute_file_paths.append(str(absolute_path))
                else:
                    return None, None, f"文件不存在: {file_path}", None
            else:
                return None, None, f"无效的文件路径格式: {file_path}", None
        
        if not absolute_file_paths:
            return None, None, "没有有效的文件路径", None
        
        # 5. 对每个文件进行OCR预处理
        files_to_upload = []
        for abs_path in absolute_file_paths:
            processed_files = prepare_upload_files(abs_path)
            files_to_upload.extend(processed_files)
        
        if not files_to_upload:
            return None, None, "文件预处理失败，没有可上传的文件", None
        
        # 6. 创建workspace和thread，但不发送prompt
        # 此处仅做会话环境的准备工作：创建workspace和thread，后续再发送实际对话内容
        
        # 手动执行workspace和thread创建流程（不发送prompt）
        workspace_info = client.create_workspace(workspace_name, user_id=user_id)
        if not workspace_info:
            return None, None, "创建workspace失败", None
        
        workspace_slug = workspace_info.get("slug") or str(workspace_info.get("id"))
        if not workspace_slug:
            return None, None, "获取workspace_slug失败", None
        
        thread_info = client.create_thread(workspace_slug, thread_name, user_id=user_id)
        if not thread_info:
            return None, None, "创建thread失败", None
        
        thread_slug = client.extract_thread_slug(thread_info) or thread_info.get("id")
        if not thread_slug:
            return None, None, "获取thread_slug失败", None
        
        # 7. 上传文件并处理embeddings
        attached_document_ids = []
        for upload_file in files_to_upload:
            if not os.path.exists(upload_file):
                continue
                
            doc_info = client.upload_document(upload_file, user_id=user_id)
            if not doc_info:
                continue

            doc_id = doc_info.get("id") or doc_info.get("docId")
            if not doc_id:
                continue

            filename = os.path.basename(upload_file)
            doc_relative_path = (
                doc_info.get("location")
                or doc_info.get("docpath")
                or f"custom-documents/{filename}-{doc_id}.json"
            )

            # 等待文档处理完成
            if not client.wait_for_processing(doc_relative_path):
                continue

            # 更新embeddings
            if not client.update_embeddings(doc_relative_path, workspace_slug, user_id=user_id):
                alt_path = f"custom-documents/{doc_id}.json"
                if client.update_embeddings(alt_path, workspace_slug, user_id=user_id):
                    doc_relative_path = alt_path

            time.sleep(1)

            # 获取文档UUID
            doc_entry = client.fetch_workspace_document(workspace_slug, doc_relative_path, user_id=user_id)
            if doc_entry:
                doc_uuid = doc_entry.get("docId") or doc_entry.get("id")
                if doc_uuid:
                    attached_document_ids.append(str(doc_uuid))
                    continue
            attached_document_ids.append(str(doc_id))

        if not attached_document_ids:
            return None, None, "没有成功上传的文档", None
        
        return workspace_slug, thread_slug, None, attached_document_ids
        
    except Exception as exc:
        return None, None, f"创建对话工作区失败：{exc}", None


def send_chat_message(workspace_slug: str, thread_slug: str, message: str, document_ids: List[str], user_id: int = 1) -> Optional[str]:
    """发送对话消息到AnythingLLM。
    Args:
        workspace_slug: 工作区标识
        thread_slug: 对话线程标识  
        message: 用户消息
        document_ids: 文档ID列表（每次发送都包含，确保基于这些文档回答）
        user_id: 用户ID
    Returns:
        AI回复文本，失败时返回None
    """
    from config import load_anythingllm_config
    from anythingllm_client import AnythingLLMClient
    
    try:
        # 创建AnythingLLM客户端
        client = AnythingLLMClient(load_anythingllm_config())
        
        # 发送消息，每次都包含document_ids确保基于选定文件回答
        result = client.send_prompt_to_thread(
            workspace_slug=workspace_slug,
            thread_slug=thread_slug,
            prompt=message,
            user_id=user_id,
            document_ids=document_ids,  # 每次都包含，确保基于这些文档回答
            mode="chat"
        )
        
        if result is None:
            return None
        return result.get("textResponse")
        
    except Exception as exc:
        return f"发送消息失败：{exc}"
