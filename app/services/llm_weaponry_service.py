from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

from app.clients.anythingllm_client import AnythingLLMClient
from app.core.config import load_anythingllm_config

from app.core.database_service import DatabaseService
from app.clients.callback_client import post_callback_payload
from app.core.llm_progress_hub import LLMProgressHub
from app.services.llm_task_service import LLMTaskService
from app.services.llm_translation_service import get_translation_service
from app.core.prompts import (
    build_input_field_prompt,
    build_table_column_prompt,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 语言检测 & 翻译
# ---------------------------------------------------------------------------

def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _translate_if_needed(text: str) -> str:
    """若文本不含中文字符，则调用翻译服务翻译为中文。"""
    if not text or _has_cjk(text):
        return ""
    try:
        service = get_translation_service()
        return service.translate_text_only(text, target_lang="Chinese")
    except Exception as e:
        logger.warning("翻译失败: %s", e)
        return ""


# ---------------------------------------------------------------------------
# source 映射
# ---------------------------------------------------------------------------

def _map_source_to_analyse_data_source(source: Dict[str, Any]) -> Dict[str, Any]:
    """将 AnythingLLM 的 source 对象映射为甲方 analyseDataSource 格式。"""
    content = source.get("text", "")
    title = source.get("title", "")
    return {
        "content": content,
        "source": title,
        "time": "",
        "translate": _translate_if_needed(content),
    }


def _build_analyse_data_sources(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将 sources 列表转换为 analyseDataSource 列表并按 score 降序排列。"""
    scored = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        mapped = _map_source_to_analyse_data_source(src)
        score = 0.0
        try:
            score = float(src.get("score", 0))
        except (TypeError, ValueError):
            pass
        scored.append((score, mapped))
    scored.sort(key=lambda x: x[0], reverse=True)
    res = [item for _, item in scored]
    if not res:
        # 甲方接口要求：无来源时返回空内容对象
        return [_map_source_to_analyse_data_source({})]
    return res


# ---------------------------------------------------------------------------
# 进度发布
# ---------------------------------------------------------------------------

def _publish_progress(
    progress_hub: LLMProgressHub,
    architecture_id: str,
    progress: float,
) -> None:
    progress_hub.publish(
        "weaponry",
        architecture_id,
        {
            "businessType": "weaponry",
            "data": {"architectureId": architecture_id, "progress": progress},
        },
    )


# ---------------------------------------------------------------------------
# 回调 payload 构建
# ---------------------------------------------------------------------------

def _build_weaponry_callback_payload(
    architecture_id: int,
    field_list: List[Dict[str, Any]],
    status: str,
    msg: str = "",
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "status": status,
        "architectureId": architecture_id,
    }
    if status == "2":
        data["weaponryTemplateFieldList"] = field_list
    return {
        "businessType": "weaponry",
        "data": data,
        "msg": msg or ("解析成功" if status == "2" else "解析失败"),
    }


# ---------------------------------------------------------------------------
# 展开字段列表，计数所有需要查询的原子字段
# ---------------------------------------------------------------------------

def _count_query_fields(field_list: List[Dict[str, Any]]) -> int:
    """统计所有需要 RAG 查询的原子字段数量（INPUT 算 1，TABLE 按列数算）。"""
    count = 0
    for field in field_list:
        if field.get("fieldType") == "TABLE":
            # TABLE 模板第一行即列定义
            template_rows = field.get("tableFieldList") or []
            if template_rows and isinstance(template_rows[0], list):
                count += len(template_rows[0])
            else:
                count += 1
        else:
            count += 1
    return count


# ---------------------------------------------------------------------------
# 单字段 RAG 查询
# ---------------------------------------------------------------------------

def _query_input_field(
    client: AnythingLLMClient,
    workspace_slug: str,
    thread_slug: str,
    field: Dict[str, Any],
    user_id: int = 1,
) -> Dict[str, Any]:
    """查询 INPUT 类型字段并返回填充后的字段对象。"""
    field_name = field.get("fieldName", "")
    field_desc = field.get("fieldDescription", "")

    prompt = build_input_field_prompt(field_name, field_desc)
    result = client.send_prompt_to_thread(
        workspace_slug,
        thread_slug,
        prompt,
        user_id=user_id,
        mode="query",
    )

    filled = dict(field)
    if result is None:
        filled["analyseData"] = ""
        filled["analyseDataSource"] = _build_analyse_data_sources([])
        return filled

    text_response = result.get("textResponse", "")
    sources = result.get("sources", [])

    # 如果 LLM 回答"未找到"则视为空
    if "未找到" in text_response:
        text_response = ""
        sources = []

    filled["analyseData"] = text_response
    filled["analyseDataSource"] = _build_analyse_data_sources(sources)
    return filled


# ---------------------------------------------------------------------------
# TABLE 类型字段处理
# ---------------------------------------------------------------------------

def _query_table_field(
    client: AnythingLLMClient,
    workspace_slug: str,
    thread_slug: str,
    field: Dict[str, Any],
    user_id: int = 1,
    on_column_done: Optional[Any] = None,
) -> Dict[str, Any]:
    """查询 TABLE 类型字段：逐列查询 + 按来源分组组装多行。

    ``on_column_done`` 可选回调，每完成一列调用一次，用于更新进度。
    """
    template_rows = field.get("tableFieldList") or []
    if not template_rows or not isinstance(template_rows[0], list):
        filled = dict(field)
        filled["tableFieldList"] = []
        return filled

    # 列定义取模板第一行
    column_defs = template_rows[0]
    table_name = field.get("fieldName", "表格")

    # 逐列查询结果，每列存储 [{value, sources}, ...]
    column_results: List[List[Dict[str, Any]]] = []

    for col_def in column_defs:
        col_name = col_def.get("fieldName", "")
        col_desc = col_def.get("fieldDescription", "")

        prompt = build_table_column_prompt(col_name, col_desc, table_context=table_name)
        result = client.send_prompt_to_thread(
            workspace_slug,
            thread_slug,
            prompt,
            user_id=user_id,
            mode="query",
        )

        if result is None or "未找到" in result.get("textResponse", ""):
            column_results.append([])
        else:
            text = result.get("textResponse", "")
            sources = result.get("sources", [])
            # 将文本按行拆分，每行视为一条记录
            values = _parse_multi_value_response(text)
            entries = []
            for val in values:
                entries.append({
                    "value": val,
                    "sources": sources,
                })
            column_results.append(entries)

        if on_column_done:
            on_column_done()

    # 按行数对齐（取最大行数），组装 tableFieldList
    max_rows = max((len(col) for col in column_results), default=0)
    assembled_rows: List[List[Dict[str, Any]]] = []

    for row_idx in range(max_rows):
        row: List[Dict[str, Any]] = []
        for col_idx, col_def in enumerate(column_defs):
            cell = dict(col_def)
            col_entries = column_results[col_idx] if col_idx < len(column_results) else []
            if row_idx < len(col_entries):
                entry = col_entries[row_idx]
                cell["analyseData"] = entry["value"]
                cell["analyseDataSource"] = _build_analyse_data_sources(entry["sources"])
            else:
                cell["analyseData"] = ""
                cell["analyseDataSource"] = _build_analyse_data_sources([])
            row.append(cell)
        assembled_rows.append(row)

    filled = dict(field)
    filled["tableFieldList"] = assembled_rows
    return filled


def _parse_multi_value_response(text: str) -> List[str]:
    """将 LLM 返回的多值文本拆分为值列表。

    支持格式：
        值1: XXX（来源：...）
        1. XXX
        - XXX
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    values = []
    for line in lines:
        # 去除序号前缀 "1." "1、" "值1:"
        cleaned = re.sub(r"^(\d+[.、:：]|值\d+[：:])\s*", "", line)
        # 去除来源标注 （来源：...）
        cleaned = re.sub(r"[（(]来源[：:].*?[)）]", "", cleaned).strip()
        if cleaned:
            values.append(cleaned)
    return values if values else [text.strip()] if text.strip() else []


# ---------------------------------------------------------------------------
# 主任务入口
# ---------------------------------------------------------------------------

def run_weaponry_task(
    *,
    task_service: LLMTaskService,
    kb_service: DatabaseService,
    progress_hub: LLMProgressHub,
    request_payload: Dict[str, Any],
    callback_url: str,
    callback_timeout: float,
) -> None:
    """后台线程入口：执行 weaponry 解析任务。"""

    params = request_payload.get("params", {})
    architecture_id = params.get("architectureId")
    architecture_id_str = str(architecture_id)
    field_list: List[Dict[str, Any]] = params.get("weaponryTemplateFieldList", [])

    try:
        # ─── 阶段 1：查找 Workspace ───
        task_service.update_task_progress(
            "weaponry", architecture_id_str,
            progress=0.05, message="正在查找知识库", status="1",
        )
        _publish_progress(progress_hub, architecture_id_str, 0.05)

        workspace_slug = kb_service.get_workspace_slug(architecture_id)
        if not workspace_slug:
            logger.warning("architectureId=%s 无对应 Workspace，标记失败", architecture_id)
            _fail_task(
                task_service, progress_hub, architecture_id, architecture_id_str,
                callback_url, callback_timeout,
                msg=f"architectureId={architecture_id} 对应的知识库不存在",
            )
            return

        # ─── 阶段 2：创建临时 Thread ───
        task_service.update_task_progress(
            "weaponry", architecture_id_str,
            progress=0.10, message="正在创建检索会话",
        )
        _publish_progress(progress_hub, architecture_id_str, 0.10)

        client = AnythingLLMClient(load_anythingllm_config())
        thread_name = f"weaponry-{architecture_id}-{int(time.time() * 1000)}"
        thread_info = client.create_thread(workspace_slug, thread_name, user_id=1)
        if not thread_info:
            _fail_task(
                task_service, progress_hub, architecture_id, architecture_id_str,
                callback_url, callback_timeout,
                msg="创建检索会话失败",
            )
            return
        thread_slug = client.extract_thread_slug(thread_info) or thread_info.get("id")
        if not thread_slug:
            _fail_task(
                task_service, progress_hub, architecture_id, architecture_id_str,
                callback_url, callback_timeout,
                msg="获取检索会话标识失败",
            )
            return

        # ─── 阶段 3：逐字段查询 ───
        total_query_fields = _count_query_fields(field_list)
        completed_fields = 0

        def _update_field_progress():
            nonlocal completed_fields
            completed_fields += 1
            # 字段查询占总进度的 0.15 ~ 0.90 区间
            progress = 0.15 + (completed_fields / max(total_query_fields, 1)) * 0.75
            task_service.update_task_progress(
                "weaponry", architecture_id_str,
                progress=progress,
                message=f"正在提取字段 ({completed_fields}/{total_query_fields})",
            )
            _publish_progress(progress_hub, architecture_id_str, progress)

        result_fields: List[Dict[str, Any]] = []
        for field in field_list:
            field_type = field.get("fieldType", "INPUT")
            field_name = field.get("fieldName", "unknown")
            logger.info("正在处理字段: %s (%s)", field_name, field_type)

            if field_type == "TABLE":
                filled = _query_table_field(
                    client, workspace_slug, thread_slug, field,
                    user_id=1,
                    on_column_done=_update_field_progress,
                )
            else:
                filled = _query_input_field(
                    client, workspace_slug, thread_slug, field, user_id=1,
                )
                _update_field_progress()

            result_fields.append(filled)

        # ─── 阶段 4：删除 Thread ───
        task_service.update_task_progress(
            "weaponry", architecture_id_str,
            progress=0.92, message="正在清理检索会话",
        )
        _publish_progress(progress_hub, architecture_id_str, 0.92)

        if not client.delete_thread(workspace_slug, thread_slug, user_id=1):
            logger.warning("删除 Thread %s 失败（不影响结果）", thread_slug)

        logger.info("武器装备提取任务完成: architectureId=%s", architecture_id)

        # ─── 阶段 5：组装回调并发送 ───
        callback_payload = _build_weaponry_callback_payload(
            architecture_id, result_fields, status="2", msg="解析成功",
        )
        task_service.mark_business_result(
            "weaponry", architecture_id_str,
            callback_payload, status="2", message="解析完成",
        )
        _publish_progress(progress_hub, architecture_id_str, 1.0)

        if callback_url:
            if post_callback_payload(callback_url, callback_payload, timeout=callback_timeout):
                task_service.mark_callback_success("weaponry", architecture_id_str)
                logger.info("回调结果提交成功: architectureId=%s", architecture_id)
            else:
                task_service.mark_callback_failed("weaponry", architecture_id_str, "callback failed")
                logger.warning("回调结果提交失败: architectureId=%s", architecture_id)

    except Exception as e:
        logger.exception("武器装备提取任务异常: architectureId=%s, error=%s", architecture_id, e)
        _fail_task(
            task_service, progress_hub, architecture_id, architecture_id_str,
            callback_url, callback_timeout,
            msg=f"解析异常: {e}",
        )


def _fail_task(
    task_service: LLMTaskService,
    progress_hub: LLMProgressHub,
    architecture_id: int,
    architecture_id_str: str,
    callback_url: str,
    callback_timeout: float,
    msg: str = "解析失败",
) -> None:
    """统一的任务失败处理。"""
    callback_payload = _build_weaponry_callback_payload(
        architecture_id, [], status="3", msg=msg,
    )
    task_service.mark_business_result(
        "weaponry", architecture_id_str,
        callback_payload, status="3", message=msg,
    )
    _publish_progress(progress_hub, architecture_id_str, 1.0)

    if callback_url:
        if post_callback_payload(callback_url, callback_payload, timeout=callback_timeout):
            task_service.mark_callback_success("weaponry", architecture_id_str)
        else:
            task_service.mark_callback_failed("weaponry", architecture_id_str, "callback failed")
