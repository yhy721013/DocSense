# app/services/classify_worker.py
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from rag_with_ocr import process_file_with_rag

from app.services.file_ops import move_file_to_category_folder, normalize_category_path
from app.services.task_store import InMemoryTaskStore


# “100% 确信”阈值：用于兼容模型把 100/100%/1/1.0 等不同格式写入 category_confidence 的情况
AUTO_CLASSIFY_THRESHOLD = 0.999
REFUSAL_MARKERS = (
    "很抱歉，我无法从提供的文档中找到相关信息来回答该问题",
    "无法从提供的文档中找到相关信息",
)


def _build_refusal_fallback(raw_text: str) -> Dict[str, Any]:
    return {
        "outline": [],
        "security_level": "公开",
        "category_confidence": 0.1,
        "category": None,
        "sub_category": None,
        "category_candidates": [],
        "extract": {},
        "summary": raw_text.strip() or "未能从文档中检索到足够信息",
    }


def _parse_result(result: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if result is None:
        return None, "未收到 AnythingLLM 的响应"
    if isinstance(result, dict):
        return result, None
    if isinstance(result, str):
        text = result.lstrip("\ufeff").strip()
        if not text:
            return None, "结果为空，未返回可解析内容"

        if any(marker in text for marker in REFUSAL_MARKERS):
            return _build_refusal_fallback(text), None

        candidates: List[str] = [text]

        # 兼容模型输出 ```json ... ``` 围栏
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
        if fence_match:
            fenced = fence_match.group(1).strip()
            if fenced:
                candidates.append(fenced)

        # 兼容前后带说明文字，只截取 JSON 主体
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if 0 <= first_brace < last_brace:
            body = text[first_brace:last_brace + 1].strip()
            if body:
                candidates.append(body)

        seen: set[str] = set()
        for candidate in candidates:
            candidate = candidate.lstrip("\ufeff").strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)

            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed, None
            except json.JSONDecodeError:
                # 容错：去掉尾随逗号再尝试一次
                normalized = re.sub(r",\s*([}\]])", r"\1", candidate)
                try:
                    parsed = json.loads(normalized)
                    if isinstance(parsed, dict):
                        return parsed, None
                except json.JSONDecodeError:
                    continue

        preview = text[:180].replace("\n", " ")
        return None, f"结果不是合法 JSON（响应片段：{preview}）"
    return None, "未知的结果类型"


def _parse_confidence(value: Any) -> Optional[float]:
    """把多种置信度表达统一为 [0,1] 浮点数。"""
    if value is None:
        return None

    try:
        if isinstance(value, (int, float)):
            v = float(value)
        elif isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            if s.endswith("%"):
                s = s[:-1].strip()
                v = float(s) / 100.0
            else:
                v = float(s)
        else:
            return None
    except (TypeError, ValueError):
        return None

    # 兼容 100/99.5 这类“百分数但没写 %”的情况
    if v > 1.0:
        v = v / 100.0 if v <= 100.0 else 1.0
    if v < 0.0:
        v = 0.0
    if v > 1.0:
        v = 1.0
    return v


def _handle_category_move(
    file_path: Path,
    parsed_result: Dict[str, Any],
) -> Tuple[bool, str, bool, List[Dict[str, Any]]]:
    """解析分类并（如确定）移动文件。

    业务规则：
    1) 当模型“确信”（category_confidence≈1.0）时：直接采用 category/sub_category 并移动文件；
       即便模型同时给出了 category_candidates，也应忽略候选并不触发人工确认。
    2) 当模型不确信时：不移动文件，返回候选列表供人工确认。

    Returns:
        (success, move_message, manual_selection_required, category_candidates)
    """
    raw_candidates = parsed_result.get("category_candidates")
    category_candidates: List[Dict[str, Any]] = raw_candidates if isinstance(raw_candidates, list) else []

    category = parsed_result.get("category")
    sub_category = parsed_result.get("sub_category")
    confidence = _parse_confidence(parsed_result.get("category_confidence"))

    has_category = isinstance(category, str) and category.strip() != ""
    confident = has_category and (confidence is None or confidence >= AUTO_CLASSIFY_THRESHOLD)

    # Case A: 确信分类 -> 忽略候选，避免前端误判为“需要人工确认”
    if confident:
        parsed_result["category"] = category.strip()  # type: ignore[union-attr]
        if isinstance(sub_category, str):
            parsed_result["sub_category"] = sub_category.strip()
        # 将置信度规范化为 1.0（前端会显示 100%）
        parsed_result["category_confidence"] = 1.0
        parsed_result.pop("category_candidates", None)

    # Case B: 不确信且有候选 -> 进入人工确认模式（并避免同时输出 category/sub_category）
    elif category_candidates:
        parsed_result.pop("category", None)
        parsed_result.pop("sub_category", None)
        return True, "等待人工选择分类", True, category_candidates

    # Case C: 没有候选（或候选为空）-> 退化为直接采用 category（保持旧逻辑，不阻塞流程）
    category_value = parsed_result.get("category")
    sub_category_value = parsed_result.get("sub_category")
    full_category, normalize_error = normalize_category_path(category_value, sub_category_value)
    if not full_category:
        return True, normalize_error or "未获取到有效的分类信息，文件保留在原位置", False, []

    moved, move_message = move_file_to_category_folder(file_path, full_category)
    if not moved:
        return True, move_message, False, []
    return True, move_message, False, []


def process_single_file_task(
    store: InMemoryTaskStore,
    task_id: str,
    file_path: str,
    workspace_name: str,
    thread_name: str,
    user_id: int = 1,
) -> None:
    """后台线程：处理单文件分类/抽取任务。"""
    path = Path(file_path)
    logger.info("开始执行单文件分类任务: task_id=%s, file=%s", task_id, path.name)
    try:
        result = process_file_with_rag(
            file_path=str(path),
            workspace_name=workspace_name,
            thread_name=thread_name,
            user_id=user_id,
        )

        parsed, parse_error = _parse_result(result)
        if parse_error:
            store.update(
                task_id,
                status="error",
                message=f"处理失败：{parse_error}",
                error=f"处理失败：{parse_error}",
                raw_result=result,
            )
            return

        assert parsed is not None
        _, move_message, manual_required, candidates = _handle_category_move(path, parsed)

        store.update(
            task_id,
            status="completed",
            progress=100,
            message="处理完成" + (f" - {move_message}" if move_message else ""),
            manual_selection_required=manual_required,
            category_candidates=candidates if manual_required else [],
            # 注意：存“规范化后的 parsed”，避免前端仅凭 category_candidates 字段误判需要人工确认
            result=parsed,
            raw_result=result,
        )
        logger.info("单文件分类任务完成: task_id=%s", task_id)

    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("单文件分类任务异常: task_id=%s, error=%s", task_id, exc)
        store.update(
            task_id,
            status="error",
            message=f"处理失败：{exc}",
            error=f"处理失败：{exc}",
        )


def process_folder_task(
    store: InMemoryTaskStore,
    task_id: str,
    saved_files: List[Dict[str, Any]],
    workspace_name: str,
    thread_name: str,
    user_id: int = 1,
) -> None:
    """后台线程：批量处理文件夹。"""
    logger.info("开始执行文件夹分类任务: task_id=%s, file_count=%d", task_id, len(saved_files))
    results: List[Dict[str, Any]] = []
    processed = 0
    total_files = len(saved_files)

    for idx, item in enumerate(saved_files):
        file_path_obj = Path(item["path"])
        display_name = item["display_name"]
        specific_thread_name = f"{thread_name}-{idx + 1}-{file_path_obj.stem}"

        try:
            raw_result = process_file_with_rag(
                file_path=str(file_path_obj),
                workspace_name=workspace_name,
                thread_name=specific_thread_name,
                user_id=user_id,
            )

            parsed, parse_error = _parse_result(raw_result)
            category_candidates: List[Dict[str, Any]] = []

            manual_selection_required = False
            move_message = ""
            category_error = ""
            error_message = ""

            result_for_ui: Any = raw_result
            if parse_error:
                error_message = parse_error
                category_error = parse_error
            else:
                assert parsed is not None
                result_for_ui = parsed
                _, move_message, manual_selection_required, category_candidates = _handle_category_move(
                    file_path_obj,
                    parsed,
                )
                # _handle_category_move 在“未取得有效分类/移动失败”时也会返回说明
                if move_message and ("失败" in move_message or "无效" in move_message):
                    category_error = move_message

            results.append(
                {
                    "file": display_name,
                    "file_path": str(file_path_obj),
                    "result": result_for_ui,  # 统一给前端“规范化结果”
                    "raw_result": raw_result,  # 保留原始返回用于排障
                    "success": raw_result is not None and parse_error is None,
                    "error": error_message,
                    "thread_name": specific_thread_name,
                    "category_candidates": category_candidates if manual_selection_required else [],
                    "manual_selection_required": manual_selection_required,
                    "move_message": move_message,
                    "category_error": category_error,
                }
            )

        except Exception as exc:  # pylint: disable=broad-except
            results.append(
                {
                    "file": display_name,
                    "file_path": str(file_path_obj),
                    "result": None,
                    "raw_result": None,
                    "success": False,
                    "error": str(exc),
                    "thread_name": specific_thread_name,
                    "category_candidates": [],
                    "manual_selection_required": False,
                    "move_message": "",
                    "category_error": "处理失败",
                }
            )

        processed += 1
        store.update(
            task_id,
            progress=int(processed / total_files * 100),
            processed=processed,
            message=f"正在处理第 {processed}/{total_files} 个文件 ({display_name})...",
        )

        # 轻微节流，避免 AnythingLLM/embedding 更新峰值
        time.sleep(0.1)

    logger.info("文件夹分类任务完成: task_id=%s", task_id)
    store.update(
        task_id,
        status="completed",
        progress=100,
        message=f"文件夹处理完成，成功 {sum(1 for r in results if r['success'])}/{len(results)}",
        result={
            "batch_summary": {
                "total": len(results),
                "successful": sum(1 for r in results if r["success"]),
                "failed": sum(1 for r in results if not r["success"]),
            },
            "files": results,
        },
    )
