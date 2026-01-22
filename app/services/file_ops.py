from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Optional, Tuple

from app.services.category_rules import CATEGORY_FOLDERS, CATEGORY_SUBCATEGORIES
from app.settings import UPLOAD_DIR


def move_file_to_category_folder(file_path: Path, category: str) -> Tuple[bool, str]:
    """根据军事分类将文件移动到对应的文件夹。"""
    if not file_path.exists():
        return False, f"文件不存在: {file_path}"

    if category not in CATEGORY_FOLDERS:
        return False, f"未知的分类: {category}"

    target_folder = UPLOAD_DIR / CATEGORY_FOLDERS[category]
    target_folder.mkdir(parents=True, exist_ok=True)

    target_path = target_folder / file_path.name

    # 如果目标文件已存在，添加时间戳
    if target_path.exists():
        stem = file_path.stem
        suffix = file_path.suffix
        timestamp = int(time.time())
        target_path = target_folder / f"{stem}_{timestamp}{suffix}"

    try:
        shutil.move(str(file_path), str(target_path))
        return True, f"文件已移动到: {target_path.relative_to(UPLOAD_DIR.parent)}"
    except Exception as exc:  # pylint: disable=broad-except
        return False, f"移动文件失败: {exc}"


def normalize_category_path(category: Optional[str], sub_category: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """规范化分类路径：容错模型输出的多余子分类或将完整路径拆分。

    Returns:
        (full_category, error_message)
    """
    if not category or not isinstance(category, str):
        return None, "分类无效"

    category = category.strip()
    sub_category = sub_category.strip() if isinstance(sub_category, str) else ""

    # 如果模型直接返回了完整路径，优先识别
    if category in CATEGORY_FOLDERS:
        return category, None

    # 处理“类别/子类”被塞进 category 的情况
    if "/" in category and not sub_category:
        parts = [part for part in category.split("/") if part.strip()]
        if parts:
            category = parts[0].strip()
            if len(parts) > 1:
                sub_category = parts[1].strip()

    if category in CATEGORY_SUBCATEGORIES:
        if not sub_category:
            return None, "该分类需要子分类"
        if sub_category not in CATEGORY_SUBCATEGORIES[category]:
            return None, "未知的子分类"
        full_category = f"{category}/{sub_category}"
    else:
        # 其他分类忽略 sub_category
        full_category = category

    if full_category not in CATEGORY_FOLDERS:
        return None, "未知的分类"

    return full_category, None
