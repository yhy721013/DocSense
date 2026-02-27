# 加入数据库存储，修改后的pipeline.py
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List, Optional, Dict, Any

from anythingllm_client import AnythingLLMClient
from document_utils import is_image_file, is_pdf_file, is_scanned_pdf
from ocr_utils import process_image_with_paddleocr, process_pdf_with_paddleocr
from database_service import document_db  # 导入数据库服务


def prepare_upload_files(
        file_path: str,
        ocr_dpi: int = 150,
        ocr_workers: Optional[int] = None,
        ocr_use_gpu: bool = True,
) -> List[str]:
    # 根据文件类型决定是否执行 OCR
    path = Path(file_path)
    if not path.exists():
        return []

    files_to_upload: List[str] = []
    if is_pdf_file(str(path)):
        if is_scanned_pdf(str(path)):
            ocr_output = process_pdf_with_paddleocr(
                str(path),
                lang="ch",
                dpi=ocr_dpi,
                num_workers=ocr_workers,
                use_gpu=ocr_use_gpu,
            )
            if ocr_output:
                files_to_upload.append(ocr_output)
        else:
            files_to_upload.append(str(path))
    elif is_image_file(str(path)):
        ocr_output = process_image_with_paddleocr(str(path), lang="ch", use_gpu=ocr_use_gpu)
        if ocr_output:
            files_to_upload.append(ocr_output)
    else:
        files_to_upload.append(str(path))

    return files_to_upload


# 修改 pipeline.py 中的 run_anythingllm_rag 函数
def run_anythingllm_rag(
        client: AnythingLLMClient,
        files_to_upload: List[str],
        prompt: str,
        workspace_name: str,
        thread_name: str,
        user_id: int,
        mode: str = "query",
        reuse_workspace: bool = False,
        store_in_db: bool = True,
        additional_metadata: Optional[Dict[str, Any]] = None,
        final_file_path: Optional[str] = None,  # 新增参数:最终文件路径
        store_original_file: bool = True  # 新增参数:保存原始文件
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

    result = client.send_prompt_to_thread(
        workspace_slug,
        thread_slug,
        prompt,
        user_id=user_id,
        document_ids=attached_document_ids,
        mode=mode,
    )

    # 如果需要存储到数据库，则保存结果
    if store_in_db and result and len(files_to_upload) > 0:
        # 准备元数据
        metadata = {
            "user_id": user_id,
            "workspace_name": workspace_name,
            "thread_name": thread_name,
            "original_file_path": files_to_upload[0],  # 使用第一个文件作为主要文件路径
            "uploaded_files": files_to_upload,
            "prompt_used": prompt,
            "mode": mode,
            "created_at": time.time(),
        }

        # 合并附加元数据
        if additional_metadata:
            metadata.update(additional_metadata)

        # 保存到MongoDB - 使用原始路径和最终路径
        try:
            original_path = files_to_upload[0]
            # 如果提供了最终路径，使用它；否则使用原始路径
            actual_final_path = final_file_path if final_file_path else original_path

            db_result_id = document_db.save_result(
                original_file_path=original_path,
                final_file_path=actual_final_path,
                result=result,
                metadata=metadata,
                store_original_file=store_original_file  # 传递存储原始文件的参数
            )
            if db_result_id:
                print(f"✅ 文档结果已保存到MongoDB，ID: {db_result_id}")
            else:
                print(f"⚠️  文档结果保存到MongoDB失败")
        except Exception as e:
            print(f"⚠️  保存到MongoDB时出错: {str(e)}")

    return result


# 在 pipeline.py 中修改 process_file_with_rag 函数，增加 final_file_path 参数
def process_file_with_rag(
        client: AnythingLLMClient,
        file_path: str,
        prompt: str,
        workspace_name: str,
        thread_name: str,
        user_id: int,
        ocr_dpi: int = 150,
        ocr_workers: Optional[int] = None,
        ocr_use_gpu: bool = True,
        store_in_db: bool = True,
        additional_metadata: Optional[Dict[str, Any]] = None,
        final_file_path: Optional[str] = None,  # 新增参数
        store_original_file: bool = True  # 新增参数
) -> Optional[str]:
    files_to_upload = prepare_upload_files(
        file_path=file_path,
        ocr_dpi=ocr_dpi,
        ocr_workers=ocr_workers,
        ocr_use_gpu=ocr_use_gpu,
    )
    return run_anythingllm_rag(
        client=client,
        files_to_upload=files_to_upload,
        prompt=prompt,
        workspace_name=workspace_name,
        thread_name=thread_name,
        user_id=user_id,
        mode="query",
        reuse_workspace=False,
        store_in_db=store_in_db,
        additional_metadata=additional_metadata,
        final_file_path=final_file_path,  # 传递最终文件路径
        store_original_file=store_original_file,  # 传递参数
    )
