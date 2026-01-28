from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List, Optional

from anythingllm_client import AnythingLLMClient


def prepare_upload_files(file_path: str) -> List[str]:
    """
    准备上传文件列表。
    所有类型的文件都直接上传到 AnythingLLM 进行解析存储，
    由 AnythingLLM 内置的文档处理能力完成 OCR 和文本提取。
    """
    path = Path(file_path)
    if not path.exists():
        return []

    return [str(path)]


def run_anythingllm_rag(
    client: AnythingLLMClient,
    files_to_upload: List[str],
    prompt: str,
    workspace_name: str,
    thread_name: str,
    user_id: int,
    mode: str = "query",
    reuse_workspace: bool = False,
) -> Optional[str]:
    # 负责：建 workspace/thread -> 上传 -> 绑 embedding -> 发送 prompt
    if not files_to_upload:
        return None

    if reuse_workspace:
        workspace_info = client.ensure_workspace(workspace_name, user_id=user_id)
    else:
        workspace_info = client.create_workspace(workspace_name, user_id=user_id)
    if not workspace_info:
        return None

    workspace_slug = workspace_info.get("slug") or str(workspace_info.get("id"))
    if not workspace_slug:
        return None

    thread_info = client.create_thread(workspace_slug, thread_name, user_id=user_id)
    if not thread_info:
        return None

    thread_slug = client.extract_thread_slug(thread_info) or thread_info.get("id")
    if not thread_slug:
        return None

    attached_document_ids: List[str] = []
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

        if not client.wait_for_processing(doc_relative_path):
            continue

        if not client.update_embeddings(doc_relative_path, workspace_slug, user_id=user_id):
            alt_path = f"custom-documents/{doc_id}.json"
            if client.update_embeddings(alt_path, workspace_slug, user_id=user_id):
                doc_relative_path = alt_path

        time.sleep(1)

        doc_entry = client.fetch_workspace_document(workspace_slug, doc_relative_path, user_id=user_id)
        if doc_entry:
            doc_uuid = doc_entry.get("docId") or doc_entry.get("id")
            if doc_uuid:
                attached_document_ids.append(str(doc_uuid))
                continue
        attached_document_ids.append(str(doc_id))

    if not attached_document_ids:
        return None

    return client.send_prompt_to_thread(
        workspace_slug,
        thread_slug,
        prompt,
        user_id=user_id,
        document_ids=attached_document_ids,
        mode=mode,
    )


def process_file_with_rag(
    client: AnythingLLMClient,
    file_path: str,
    prompt: str,
    workspace_name: str,
    thread_name: str,
    user_id: int,
) -> Optional[str]:
    """
    处理文件并执行 RAG 查询。
    所有文件直接上传到 AnythingLLM，由其内置能力进行解析。
    """
    files_to_upload = prepare_upload_files(file_path=file_path)
    return run_anythingllm_rag(
        client=client,
        files_to_upload=files_to_upload,
        prompt=prompt,
        workspace_name=workspace_name,
        thread_name=thread_name,
        user_id=user_id,
        mode="query",
        reuse_workspace=False,
    )
