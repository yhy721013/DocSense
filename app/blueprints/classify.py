from __future__ import annotations

import threading
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

from flask import Blueprint, jsonify, render_template, request

from app.services.classify_worker import process_folder_task, process_single_file_task
from app.services.file_ops import normalize_category_path, move_file_to_category_folder
from app.services.task_store import InMemoryTaskStore
from app.settings import TEMP_UPLOAD_DIR


classify_bp = Blueprint("classify", __name__)

# 单进程内共享的任务状态存储（与旧版 processing_status 等价）
task_store = InMemoryTaskStore()


@classify_bp.get("/classify")
def classify_page():
    return render_template("classify.html")


@classify_bp.post("/api/classify/upload")
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "没有上传文件"}), 400

    upload = request.files["file"]
    if upload.filename == "":
        return jsonify({"error": "文件名为空"}), 400

    timestamp = int(time.time() * 1000)
    file_stem = Path(upload.filename).stem[:20]
    workspace_name = f"workspace_{timestamp}_{file_stem}"
    thread_name = request.form.get("thread", "文档分析")

    safe_name = Path(upload.filename).name
    file_path = TEMP_UPLOAD_DIR / safe_name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    upload.save(file_path)
    
    logger.info("收到单文件分类上传请求: filename=%s, workspace=%s", upload.filename, workspace_name)

    task_id = f"task_{timestamp}"
    task_store.set(
        task_id,
        {
            "status": "processing",
            "progress": 0,
            "message": f"文件上传成功，创建工作区: {workspace_name}，开始处理...",
            "file_path": str(file_path),
        },
    )

    thread = threading.Thread(
        target=process_single_file_task,
        kwargs={
            "store": task_store,
            "task_id": task_id,
            "file_path": str(file_path),
            "workspace_name": workspace_name,
            "thread_name": thread_name,
            "user_id": 1,
        },
        daemon=True,
    )
    thread.start()
    logger.info("已启动后台单文件分类线程: task_id=%s", task_id)
    return jsonify({"task_id": task_id, "message": "文件上传成功"})


@classify_bp.get("/api/classify/status/<task_id>")
def get_status(task_id: str):
    status = task_store.get(task_id)
    if not status:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(status)


@classify_bp.post("/api/classify/upload_folder")
def upload_folder():
    if "files" not in request.files:
        return jsonify({"error": "没有上传文件"}), 400

    uploaded_files = request.files.getlist("files")
    if not uploaded_files:
        return jsonify({"error": "没有上传文件"}), 400

    temp_dir = TEMP_UPLOAD_DIR / f"temp_{int(time.time())}"
    temp_dir.mkdir(exist_ok=True)

    saved_files = []
    for upload in uploaded_files:
        if upload.filename == "":
            continue
        relative_name = Path(upload.filename).as_posix()
        file_path = temp_dir / relative_name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        upload.save(file_path)
        saved_files.append({"path": file_path, "display_name": relative_name})
    
    logger.info("收到文件夹分类上传请求: file_count=%d", len(saved_files))

    if not saved_files:
        return jsonify({"error": "没有有效文件"}), 400

    timestamp = int(time.time() * 1000)
    workspace_prefix = request.form.get("workspace_prefix", "folder_workspace")
    workspace_name = f"{workspace_prefix}_{timestamp}"
    thread_name = request.form.get("thread_name", "批量处理线程")

    task_id = f"folder_task_{int(time.time())}"
    task_store.set(
        task_id,
        {
            "status": "processing",
            "progress": 0,
            "total_files": len(saved_files),
            "processed": 0,
            "message": f"开始处理文件夹，共 {len(saved_files)} 个文件...",
        },
    )

    thread = threading.Thread(
        target=process_folder_task,
        kwargs={
            "store": task_store,
            "task_id": task_id,
            "saved_files": saved_files,
            "workspace_name": workspace_name,
            "thread_name": thread_name,
            "user_id": 1,
        },
        daemon=True,
    )
    thread.start()
    logger.info("已启动后台文件夹分类线程: task_id=%s", task_id)
    return jsonify({"task_id": task_id, "message": f"开始处理文件夹，共 {len(saved_files)} 个文件"})


@classify_bp.post("/api/classify/select_category")
def select_category():
    payload = request.get_json(silent=True) or {}
    task_id = payload.get("task_id")
    category = payload.get("category")
    sub_category = payload.get("sub_category") or ""
    
    logger.info("收到分类确认请求: task_id=%s, category=%s", task_id, category)

    if not task_id:
        return jsonify({"error": "缺少任务ID"}), 400

    status = task_store.get(task_id)
    if not status:
        return jsonify({"error": "任务不存在"}), 404

    full_category, normalize_error = normalize_category_path(category, sub_category)
    if not full_category:
        return jsonify({"error": normalize_error or "分类无效"}), 400

    file_path = status.get("file_path")
    if not file_path:
        return jsonify({"error": "无法定位文件路径"}), 400

    file_path_obj = Path(file_path)
    if not file_path_obj.exists():
        return jsonify({"error": "文件不存在或已移动"}), 400

    success, move_message = move_file_to_category_folder(file_path_obj, full_category)
    if not success:
        return jsonify({"error": move_message}), 500

    # 写回任务状态（注意：task_store.get 返回的是拷贝，这里用 update 写回）
    task_store.update(
        task_id,
        manual_selection_required=False,
        manual_selected=True,
        selected_category=full_category,
        message="已人工选择分类 - " + move_message,
    )
    return jsonify({"message": move_message, "category": full_category})


@classify_bp.post("/api/classify/select_category_batch")
def select_category_batch():
    payload = request.get_json(silent=True) or {}
    task_id = payload.get("task_id")
    file_index = payload.get("file_index")
    category = payload.get("category")
    sub_category = payload.get("sub_category") or ""

    if task_id is None:
        return jsonify({"error": "缺少任务ID"}), 400
    if file_index is None:
        return jsonify({"error": "缺少文件索引"}), 400

    status = task_store.get(task_id)
    if not status:
        return jsonify({"error": "任务不存在"}), 404

    result = status.get("result") or {}
    files = result.get("files") if isinstance(result, dict) else None
    if not isinstance(files, list):
        return jsonify({"error": "批量结果不存在"}), 400

    try:
        idx = int(file_index)
    except (TypeError, ValueError):
        return jsonify({"error": "文件索引无效"}), 400

    if idx < 0 or idx >= len(files):
        return jsonify({"error": "文件索引超出范围"}), 400

    entry = files[idx]
    file_path = entry.get("file_path") if isinstance(entry, dict) else None
    if not file_path:
        return jsonify({"error": "无法定位文件路径"}), 400

    file_path_obj = Path(file_path)
    if not file_path_obj.exists():
        return jsonify({"error": "文件不存在或已移动"}), 400

    full_category, normalize_error = normalize_category_path(category, sub_category)
    if not full_category:
        return jsonify({"error": normalize_error or "分类无效"}), 400

    success, move_message = move_file_to_category_folder(file_path_obj, full_category)
    if not success:
        return jsonify({"error": move_message}), 500

    # 将更新写回 task_store：需要整体覆盖 result.files[idx]
    entry.update(
        {
            "manual_selection_required": False,
            "manual_selected": True,
            "selected_category": full_category,
            "move_message": move_message,
            "category_candidates": [],
            "category_error": "",
        }
    )
    result["files"][idx] = entry
    status["result"] = result
    status["message"] = "批量任务分类已更新"
    task_store.set(task_id, status)

    return jsonify({"message": move_message, "category": full_category})
