from __future__ import annotations

import threading
import time
import json
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from app.services.classify_worker import process_folder_task, process_single_file_task
from app.services.file_ops import normalize_category_path, move_file_to_category_folder
from app.services.task_store import InMemoryTaskStore
from app.settings import UPLOAD_DIR

from database_service import document_db
from rag_with_ocr import process_file_with_rag

classify_bp = Blueprint("classify", __name__)

# 单进程内共享的任务状态存储（与旧版 processing_status 等价）
task_store = InMemoryTaskStore()


@classify_bp.get("/classify")
def classify_page():
    return render_template("classify.html")


@classify_bp.post("/api/classify/upload")
# 在 upload_file 函数中替换原来的 worker 定义和线程启动
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "没有上传文件"}), 400

    upload = request.files["file"]
    if upload.filename == "":
        return jsonify({"error": "文件名为空"}), 400

    # 每次上传都创建新的工作区，使用时间戳和文件名生成唯一名称
    timestamp = int(time.time() * 1000)
    file_stem = Path(upload.filename).stem[:20]  # 文件名前20个字符，避免过长
    workspace_name = f"workspace_{timestamp}_{file_stem}"
    thread_name = request.form.get("thread", "文档分析")
    user_id = int(request.form.get("user_id", 1))  # 从表单获取用户ID，默认为1

    file_path = UPLOAD_DIR / upload.filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    upload.save(file_path)

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

    # 调用新的处理函数
    from app.services.classify_worker import process_single_upload_task

    threading.Thread(
        target=process_single_upload_task,
        args=(task_store, task_id, str(file_path), workspace_name, thread_name, user_id,
              upload.filename, timestamp),
        daemon=True
    ).start()

    return jsonify({"task_id": task_id, "message": "文件上传成功"})


@classify_bp.get("/api/classify/status/<task_id>")
def get_status(task_id: str):
    status = task_store.get(task_id)
    if not status:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(status)


@classify_bp.post("/api/classify/upload_folder")
# 在 upload_folder 函数中替换原来的 folder_worker 定义和线程启动
def upload_folder():
    if "files" not in request.files:
        return jsonify({"error": "没有上传文件"}), 400

    uploaded_files = request.files.getlist("files")
    if not uploaded_files:
        return jsonify({"error": "没有上传文件"}), 400

    # 创建临时目录存放文件
    temp_dir = UPLOAD_DIR / f"temp_{int(time.time())}"
    temp_dir.mkdir(exist_ok=True)

    saved_files = []
    for upload in uploaded_files:
        if upload.filename != "":
            file_path = temp_dir / upload.filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            upload.save(file_path)
            saved_files.append(file_path)

    # 只创建一个工作区，而不是为每个文件创建单独的工作区
    timestamp = int(time.time() * 1000)
    workspace_prefix = request.form.get("workspace_prefix", "folder_workspace")
    workspace_name = f"{workspace_prefix}_{timestamp}"
    thread_name = request.form.get("thread_name", "批量处理线程")
    user_id = int(request.form.get("user_id", 1))  # 从表单获取用户ID，默认为1

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

    # 调用新的处理函数
    from app.services.classify_worker import process_folder_upload_task

    threading.Thread(
        target=process_folder_upload_task,
        args=(task_store, task_id, saved_files, workspace_name, thread_name, user_id),
        daemon=True
    ).start()

    return jsonify({"task_id": task_id, "message": f"开始处理文件夹，共 {len(saved_files)} 个文件"})


@classify_bp.post("/api/classify/select_category")
def select_category():
    payload = request.get_json(silent=True) or {}
    task_id = payload.get("task_id")
    category = payload.get("category")
    sub_category = payload.get("sub_category") or ""
    parsed_result = payload.get("result")  # 从请求体中获取 parsed_result

    if not task_id:
        return jsonify({"error": "缺少任务ID"}), 400

    status = task_store.get(task_id)
    if not status:
        return jsonify({"error": "任务不存在"}), 404

    # 校验 parsed_result 是否有效
    if not parsed_result or not isinstance(parsed_result, dict):
        print("⚠️ parsed_result 无效 (in classify->select_category)")
        return jsonify({"error": "无效的 parsed_result 参数"}), 400

    file_path = status.get("file_path")
    if not file_path:
        return jsonify({"error": "无法定位文件路径"}), 400

    # 优先从任务状态中获取数据库文档ID
    document_id = status.get("db_result_id")

    if not document_id:
        # 如果任务状态中没有ID，尝试从数据库中查找
        try:
            recent_doc = document_db.collection.find_one(
                {"original_file_path": file_path},
                sort=[("created_at", -1)]
            )
            if recent_doc:
                document_id = str(recent_doc.get("_id"))
        except Exception as e:
            print(f"查找文档ID时出错: {e}")

    if document_id:
        from app.services.classify_worker import handle_manual_category_selection
        import threading

        thread = threading.Thread(
            target=handle_manual_category_selection,
            args=(task_store, task_id, str(document_id), category, sub_category, file_path, parsed_result)
        )
        thread.daemon = True
        thread.start()

        return jsonify({"message": f"人工分类更新任务已启动", "task_id": task_id})
    else:
        return jsonify({"error": "无法找到对应的数据库文档"}), 500


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

    # 移动文件到指定分类目录
    success, move_message = move_file_to_category_folder(file_path_obj, full_category)
    if not success:
        return jsonify({"error": move_message}), 500

    # ✅ 关键：查找并更新数据库中的文档记录
    try:
        # 从数据库中查找对应文件的记录
        doc_record = document_db.collection.find_one(
            {"original_file_path": file_path},
            sort=[("created_at", -1)]
        )

        if doc_record:
            # 更新数据库中的分类信息
            update_success = document_db.update_document_category(
                str(doc_record["_id"]),
                category,
                sub_category,
                new_file_path=move_message.split(": ")[1] if move_message.startswith("📁") else file_path
            )

            if not update_success:
                print(f"⚠️ 更新数据库分类信息失败: {file_path}")
        else:
            print(f"⚠️ 未找到数据库中的对应记录: {file_path}")
    except Exception as e:
        print(f"⚠️ 更新数据库时发生错误: {str(e)}")

    # 更新批量处理结果
    entry.update({
        "manual_selection_required": False,
        "manual_selected": True,
        "selected_category": full_category,
        "move_message": move_message,
        "category_candidates": [],
        "category_error": "",
    })
    result["files"][idx] = entry
    status["result"] = result
    status["message"] = "批量任务分类已更新"
    task_store.set(task_id, status)

    return jsonify({"message": move_message, "category": full_category})


"""处理人工分类选择并更新数据库"""
@classify_bp.post("/api/classify/update_document_category")
def update_document_category():
    """处理人工分类选择并更新数据库"""
    payload = request.get_json(silent=True) or {}
    task_id = payload.get("task_id")
    document_id = payload.get("document_id")
    category = payload.get("category")
    sub_category = payload.get("sub_category", "")
    file_path = payload.get("file_path")
    parsed_result = payload.get("result")

    if not all([task_id, document_id, category, file_path]):
        return jsonify({"error": "缺少必要参数"}), 400

    # 创建异步任务来处理分类更新
    from app.services.classify_worker import handle_manual_category_selection
    import threading

    thread = threading.Thread(
        target=handle_manual_category_selection,
        args=(task_store, task_id, document_id, category, sub_category, file_path ,parsed_result)
    )
    thread.daemon = True
    thread.start()

    return jsonify({
        "message": "分类更新任务已启动",
        "task_id": task_id
    })
