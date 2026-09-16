"""报告生成遗留兼容实现。

当前公开 ``/llm/generate-report`` 路由已经由报告模块的
``SubmitReportTask -> LocalReportTaskDispatcher -> RunReportTask`` 链路接管。本文件只为
迁移期黄金样例、旧导入路径和安全回滚观察保留；生产组合根、Flask 路由及后续业务代码
均不得重新依赖 ``run_report_task``。待运行路径、测试与配置三类引用全部清零后，再在阶段
1G 按证据删除，而不是在阶段 1C-7 冒险移除兼容基线。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import urlparse

from app.services.utils.anythingllm_client import AnythingLLMClient
from app.services.core.config import load_anythingllm_config
from app.services.utils.rag_pipeline import prepare_upload_files, run_anythingllm_rag

from app.services.utils.callback_client import post_callback_payload
from app.services.utils.file_downloader import download_to_temp_file
from app.services.utils.mhtml_normalizer import normalize_file_for_llm
from app.services.utils.word_extractor import extract_text_from_word
from app.services.core.progress_hub import LLMProgressHub
from app.services.core.prompts import build_report_prompt
from app.services.llm_service.task_service import LLMTaskService
from app.modules.report.domain import (
    ReportId,
    build_report_callback,
    build_report_context_name,
    build_report_conversation_name,
    build_report_result,
    ensure_report_html as _ensure_report_html,
)


logger = logging.getLogger(__name__)


def ensure_report_html(content: str | None) -> str:
    """兼容旧导入路径，并把唯一 HTML 规则转发到报告领域层。"""

    return _ensure_report_html(content)


def build_report_callback_payload(report_id: int, details: str, status: str) -> dict:
    """兼容旧调用方，并由强类型领域规则构造公开回调字典。"""

    return build_report_callback(
        ReportId.from_public_value(report_id),
        details,
        status=status,
    ).to_public_dict()


def _publish_progress(progress_hub: LLMProgressHub, report_id: int, progress: float) -> None:
    progress_hub.publish(
        "report",
        str(report_id),
        {"businessType": "report", "data": {"reportId": report_id, "progress": progress}},
    )


def _suffix_from_url(url: str, fallback: str = "") -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix
    return suffix or fallback


def _extract_template_outline_text(params: dict, report_id: int, download_root: str) -> str:
    template_url = str(params.get("templateOutline") or "").strip()
    if not template_url:
        raise ValueError("templateOutline不能为空")

    suffix = _suffix_from_url(template_url, fallback=".docx")
    template_path = download_to_temp_file(
        template_url,
        f"report-{report_id}-template{suffix}",
        download_root,
        timeout=60,
    )
    outline_text = extract_text_from_word(template_path).strip()
    if not outline_text:
        raise ValueError("Word模板未提取到有效文字内容")
    return outline_text


def run_report_task(
    *,
    task_service: LLMTaskService,
    progress_hub: LLMProgressHub,
    request_payload: dict,
    download_root: str,
    callback_url: str,
    callback_timeout: float,
) -> None:
    params = request_payload["params"][0]
    report_id = int(params["reportId"])
    report_identity = ReportId.from_public_value(report_id)

    logger.info("开始执行报告生成任务: report_id=%s", report_id)
    try:
        task_service.update_task_progress("report", str(report_id), progress=0.15, message="正在下载报告文件", status="0")
        _publish_progress(progress_hub, report_id, 0.15)

        files_to_upload = []
        for index, file_url in enumerate(params.get("filePathList", []), start=1):
            downloaded_path = download_to_temp_file(file_url, f"report-{report_id}-{index}{Path(file_url).suffix}", download_root, timeout=60)
            prepared_source = downloaded_path
            try:
                prepared_source = normalize_file_for_llm(downloaded_path)
            except Exception:
                prepared_source = downloaded_path
            files_to_upload.extend(prepare_upload_files(prepared_source))

        task_service.update_task_progress("report", str(report_id), progress=0.25, message="正在解析报告模板")
        _publish_progress(progress_hub, report_id, 0.25)
        template_outline_text = _extract_template_outline_text(params, report_id, download_root)
        prompt_params = dict(params)
        prompt_params["templateOutline"] = template_outline_text

        task_service.update_task_progress("report", str(report_id), progress=0.35, message="正在生成报告")
        _publish_progress(progress_hub, report_id, 0.35)

        client = AnythingLLMClient(load_anythingllm_config())
        details = run_anythingllm_rag(
            client=client,
            files_to_upload=files_to_upload,
            prompt=build_report_prompt(prompt_params),
            workspace_name=build_report_context_name(
                report_identity,
                str(int(time.time() * 1000)),
            ),
            thread_name=build_report_conversation_name(report_identity),
            user_id=1,
            mode="query",
            reuse_workspace=False,
        )
        report_result = build_report_result(report_identity, details)
        if report_result.empty_rag_result:
            # 空结果按已确认接口契约仍属于成功。这里显式记录质量信号，避免运维只能从
            # 空 HTML 反推上游模型行为；该内部标记不得进入回调或 Progress 载荷。
            logger.warning(
                "报告RAG返回空内容，按兼容契约继续成功: "
                "report_id=%s empty_rag_result=true",
                report_id,
            )
        html_details = report_result.html_details
        callback_payload = build_report_callback_payload(report_id, html_details, status="1")

        task_service.mark_business_result("report", str(report_id), callback_payload, status="1", message="报告生成完成")
        _publish_progress(progress_hub, report_id, 1.0)

        if callback_url:
            if post_callback_payload(
                callback_url,
                callback_payload,
                timeout=callback_timeout,
                callback_context={"businessType": "report", "reportId": report_id},
            ):
                task_service.mark_callback_success("report", str(report_id))
                logger.info("报告任务外部回调提交成功: report_id=%s", report_id)
            else:
                task_service.mark_callback_failed("report", str(report_id), "callback failed")
                logger.warning("报告任务外部回调提交失败: report_id=%s", report_id)
        else:
            # 没有回调地址时显式关闭状态机，避免完成任务被错误展示为等待回调。
            task_service.mark_callback_skipped("report", str(report_id))
        
        logger.info("报告生成任务执行完成: report_id=%s", report_id)
    except Exception as e:
        logger.exception(
            "报告生成任务执行异常: report_id=%s error_type=%s",
            report_id,
            type(e).__name__,
        )
        callback_payload = build_report_callback_payload(report_id, "", status="2")
        task_service.mark_business_result("report", str(report_id), callback_payload, status="2", message="报告生成失败")
        _publish_progress(progress_hub, report_id, 1.0)
        if callback_url:
            if post_callback_payload(
                callback_url,
                callback_payload,
                timeout=callback_timeout,
                callback_context={"businessType": "report", "reportId": report_id},
            ):
                task_service.mark_callback_success("report", str(report_id))
                logger.info("报告任务失败结果的外部回调提交成功: report_id=%s", report_id)
            else:
                task_service.mark_callback_failed("report", str(report_id), "callback failed")
                logger.warning("报告任务失败结果的外部回调提交失败: report_id=%s", report_id)
        else:
            # 业务失败与回调失败是两个独立维度；此处只表示未配置外部回调。
            task_service.mark_callback_skipped("report", str(report_id))
