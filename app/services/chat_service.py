from __future__ import annotations

from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any
from app.settings import UPLOAD_DIR


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