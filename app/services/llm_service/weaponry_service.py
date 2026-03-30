from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services.utils.anythingllm_client import AnythingLLMClient
from app.services.core.config import load_anythingllm_config

from app.services.core.database import DatabaseService
from app.services.utils.callback_client import post_callback_payload
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.task_service import LLMTaskService
from app.services.llm_service.translation_service import get_translation_service
from app.services.core.prompts import (
    build_input_field_prompt,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 语言检测 & 翻译
# ---------------------------------------------------------------------------



def _translate_if_needed(text: str) -> str:
    """对于所有文本，调用翻译服务翻译为中文。"""
    if not text:
        return ""
    try:
        service = get_translation_service()
        return service.translate_text_only(text, target_lang="Chinese", fast_translate=True, as_html=False)
    except Exception as e:
        logger.warning("翻译失败: %s", e)
        return ""


# ---------------------------------------------------------------------------
# source 映射
# ---------------------------------------------------------------------------

def _map_source_to_analyse_data_source(source: Dict[str, Any], text_response: str = "") -> Dict[str, Any]:
    """将 AnythingLLM 的 source 对象映射为甲方 analyseDataSource 格式。

    每条记录以检索来源片段为单位组织：
    - content: LLM 解析出的内容（text_response）
    - source: 检索片段的原文文本（不同来源片段可能不一样）
    - time: 得到解析结果的时间
    - translate: 对原文片段的翻译
    """
    chunk_text = source.get("text", "")
    return {
        "content": text_response,
        "source": chunk_text,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "translate": _translate_if_needed(chunk_text),
    }


def _build_analyse_data_sources(sources: List[Dict[str, Any]], text_response: str = "") -> List[Dict[str, Any]]:
    """将 sources 列表转换为 analyseDataSource 列表并按 score 降序排列。

    每个检索到的相关来源片段都作为一条独立记录。
    """
    scored = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        mapped = _map_source_to_analyse_data_source(src, text_response=text_response)
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
        return [_map_source_to_analyse_data_source({}, text_response=text_response)]
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
    """统计所有需要 RAG 查询的原子字段数量（INPUT 算 1，TABLE 按单元格算）。"""
    count = 0
    for field in field_list:
        if field.get("fieldType") == "TABLE":
            template_rows = field.get("tableFieldList") or []
            for row in template_rows:
                if isinstance(row, list):
                    count += len(row)
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
        logger.warning("字段 [%s] 检索无返回内容 (result is None)", field_name)
        filled["analyseData"] = ""
        filled["analyseDataSource"] = _build_analyse_data_sources([], text_response="")
        return filled

    text_response = result.get("textResponse", "")
    sources = result.get("sources", [])

    # 如果 LLM 回答"未找到"则视为空
    if "未找到" in text_response:
        logger.info("字段 [%s] LLM返回: 未找到相关信息", field_name)
        text_response = ""
        sources = []
    else:
        preview_text = text_response.replace('\n', ' ')
        if len(preview_text) > 40:
            preview_text = preview_text[:40] + "..."
        logger.info("字段 [%s] 提取成功: %s (匹配来源: %d 条)", field_name, preview_text, len(sources))

    filled["analyseData"] = text_response
    filled["analyseDataSource"] = _build_analyse_data_sources(sources, text_response=text_response)
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
    on_cell_done: Optional[Any] = None,
) -> Dict[str, Any]:
    """查询 TABLE 类型字段：当做多个普通 INPUT 字段逐个查询。

    ``on_cell_done`` 可选回调，每完成一个单元格调用一次，用于更新进度。
    """
    template_rows = field.get("tableFieldList") or []
    if not template_rows:
        filled = dict(field)
        filled["tableFieldList"] = []
        return filled

    logger.info("  -> 开始处理表格 [%s]，模板中包含 %d 行...", field.get("fieldName", "表格"), len(template_rows))
    assembled_rows: List[List[Dict[str, Any]]] = []

    for row_defs in template_rows:
        if not isinstance(row_defs, list):
            assembled_rows.append(row_defs)
            continue
            
        row: List[Dict[str, Any]] = []
        for cell_def in row_defs:
            logger.info("    -> 开始提取单元格: %s", cell_def.get("fieldName", "unknown"))
            filled_cell = _query_input_field(
                client, workspace_slug, thread_slug, cell_def, user_id=user_id,
            )
            row.append(filled_cell)
            if on_cell_done:
                on_cell_done()
        assembled_rows.append(row)

    filled = dict(field)
    filled["tableFieldList"] = assembled_rows
    return filled


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
                    on_cell_done=_update_field_progress,
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
