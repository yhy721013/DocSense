from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request


chat_bp = Blueprint("chat", __name__)


@chat_bp.get("/chat")
def chat_page():
    """对话功能页面（占位）。"""
    return render_template("chat.html")


@chat_bp.get("/api/chat/files")
def get_chat_files():
    """获取uploads文件夹中的所有文件列表，用于对话文件选择。
    返回格式：{
        "files": [
            {
                "path": "相对路径，如 'uploads/军事基地/文件.pdf'",
                "name": "文件名，如 '文件.pdf'", 
                "size": 文件大小(字节),
                "modified": "修改时间，如 '2024-01-01 12:00:00'"
            },
            ...
        ]
    }
    """
    try:
        # 调用服务层获取文件列表
        from app.services.chat_service import list_uploaded_files
        
        files = list_uploaded_files()
        return jsonify({"files": files})
        
    except Exception as exc:
        return jsonify({"error": f"获取文件列表失败：{exc}"}), 500


@chat_bp.post("/api/chat/upload")
def chat_upload():
    """上传文件/文件夹以建立对话上下文。

    说明：
    - 这里预留接口与返回格式，便于后续对接 AnythingLLM。
    - 当前版本返回明确的占位错误，避免前端静默失败。
    """

    return jsonify(
        {
            "error": "对话功能尚未实现：请在 app/blueprints/chat.py 与相关 service 中补全上传与建 Workspace/Thread 逻辑。"
        }
    ), 501


@chat_bp.post("/api/chat/message")
def chat_message():
    """发送对话消息。
    
    请求格式：{
        "workspace_slug": "chat_workspace_1234567890",
        "thread_slug": "thread_1234567890",
        "message": "用户的问题",
        "document_ids": ["doc_id_1", "doc_id_2", ...]
    }
    
    返回格式：{
        "response": "AI的回复内容"
    }
    或错误：{
        "error": "错误信息"
    }
    """
    try:
        # 获取请求数据
        payload = request.get_json(silent=True) or {}
        workspace_slug = payload.get("workspace_slug")
        thread_slug = payload.get("thread_slug")
        message = payload.get("message")
        document_ids = payload.get("document_ids", [])
        
        # 参数验证
        if not workspace_slug:
            return jsonify({"error": "缺少workspace_slug"}), 400
        if not thread_slug:
            return jsonify({"error": "缺少thread_slug"}), 400
        if not message or not isinstance(message, str):
            return jsonify({"error": "消息内容无效"}), 400
        if not isinstance(document_ids, list):
            return jsonify({"error": "document_ids必须是数组"}), 400
        
        # 调用服务层发送消息
        from app.services.chat_service import send_chat_message
        
        response = send_chat_message(workspace_slug, thread_slug, message, document_ids)
        
        if response is None:
            return jsonify({"error": "发送消息失败，无响应"}), 500
            
        # 检查是否是错误消息（以"发送消息失败"开头）
        if isinstance(response, str) and response.startswith("发送消息失败"):
            return jsonify({"error": response}), 500
        
        return jsonify({"response": response})
        
    except Exception as exc:
        return jsonify({"error": f"发送消息失败：{exc}"}), 500


@chat_bp.post("/api/chat/setup")
def chat_setup():
    """根据选定的文件创建对话工作区。
    请求格式：{
        "file_paths": ["uploads/军事基地/文件.pdf", "uploads/装备型号/文档.docx", ...]
    }
    
    返回格式：{
        "workspace_slug": "chat_workspace_1234567890",
        "thread_slug": "thread_1234567890", 
        "document_ids": ["document_1234567890", "document_9876543210", ...],
        "message": "对话工作区创建成功"
    }
    或错误：{
        "error": "错误信息"
    }
    """
    try:
        # 获取请求数据
        payload = request.get_json(silent=True) or {}
        file_paths = payload.get("file_paths", [])
        
        if not file_paths:
            return jsonify({"error": "请提供文件路径列表"}), 400
        
        if not isinstance(file_paths, list):
            return jsonify({"error": "file_paths必须是数组"}), 400
        
        # 调用服务层创建工作区
        from app.services.chat_service import setup_chat_workspace
        
        workspace_slug, thread_slug, error, document_ids = setup_chat_workspace(file_paths)
        
        if error:
            return jsonify({"error": error}), 500

        return jsonify({
            "workspace_slug": workspace_slug,
            "thread_slug": thread_slug,
            "document_ids": document_ids,
            "message": "对话工作区创建成功"
        })
        
    except Exception as exc:
        return jsonify({"error": f"设置对话工作区失败：{exc}"}), 500