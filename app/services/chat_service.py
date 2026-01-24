from __future__ import annotations

from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any
from app.settings import UPLOAD_DIR
import os
from typing import List, Tuple, Optional


def list_uploaded_files() -> List[Dict[str, Any]]:
    """获取uploads文件夹中的所有文件列表。
    
    返回格式：
    [
        {
            "path": "相对路径，如 'uploads/军事基地/文件.pdf'",
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
    
    # 递归扫描uploads目录
    for file_path in UPLOAD_DIR.rglob("*"):
        # 只处理文件，跳过目录
        if not file_path.is_file():
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


def setup_chat_workspace(file_paths: List[str], user_id: int = 1) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """根据选定的文件创建对话工作区。
    
    Args:
        file_paths: 文件路径列表（相对于项目根目录的路径，如 'uploads/军事基地/文件.pdf'）
        user_id: 用户ID
        
    Returns:
        (workspace_slug, thread_slug, error_message)
        成功时返回workspace_slug和thread_slug，error_message为None
        失败时返回None, None, error_message
    """
    from config import load_anythingllm_config
    from anythingllm_client import AnythingLLMClient
    from pipeline import prepare_upload_files, run_anythingllm_rag
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
        from app.settings import UPLOAD_DIR
        absolute_file_paths = []
        
        for file_path in normalized_file_paths:
            # file_path 是相对于项目根目录的路径，如 'uploads/军事基地/文件.pdf'
            # 我们需要将其转换为相对于UPLOAD_DIR的路径
            if file_path.startswith('uploads/'):
                relative_to_upload = file_path[len('uploads/'):]
                absolute_path = UPLOAD_DIR / relative_to_upload
                if absolute_path.exists():
                    absolute_file_paths.append(str(absolute_path))
                else:
                    return None, None, f"文件不存在: {file_path}"
            else:
                return None, None, f"无效的文件路径格式: {file_path}"
        
        if not absolute_file_paths:
            return None, None, "没有有效的文件路径"
        
        # 5. 对每个文件进行OCR预处理
        files_to_upload = []
        for abs_path in absolute_file_paths:
            processed_files = prepare_upload_files(abs_path)
            files_to_upload.extend(processed_files)
        
        if not files_to_upload:
            return None, None, "文件预处理失败，没有可上传的文件"
        
        # 6. 创建workspace和thread，但不发送prompt
        # 复用 run_anythingllm_rag 的逻辑，但使用一个空的prompt来避免实际发送
        dummy_prompt = ""  # 不会实际发送，因为我们只做准备工作
        
        # 调用 run_anythingllm_rag，但捕获其创建的workspace和thread信息
        # 由于我们不发送prompt，我们需要修改这个调用方式
        
        # 手动执行workspace和thread创建流程（不发送prompt）
        workspace_info = client.create_workspace(workspace_name, user_id=user_id)
        if not workspace_info:
            return None, None, "创建workspace失败"
        
        workspace_slug = workspace_info.get("slug") or str(workspace_info.get("id"))
        if not workspace_slug:
            return None, None, "获取workspace_slug失败"
        
        thread_info = client.create_thread(workspace_slug, thread_name, user_id=user_id)
        if not thread_info:
            return None, None, "创建thread失败"
        
        thread_slug = client.extract_thread_slug(thread_info) or thread_info.get("id")
        if not thread_slug:
            return None, None, "获取thread_slug失败"
        
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
            return None, None, "没有成功上传的文档"
        
        return workspace_slug, thread_slug, None
        
    except Exception as exc:
        return None, None, f"创建对话工作区失败：{exc}"