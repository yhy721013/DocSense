from __future__ import annotations

import html
import logging
import time
from pathlib import Path

from app.clients.anythingllm_client import AnythingLLMClient
from app.core.config import load_anythingllm_config
from app.pipelines.pipeline import prepare_upload_files, run_anythingllm_rag

from app.clients.callback_client import post_callback_payload
from app.utils.file_downloader import download_to_temp_file
from app.utils.mhtml_normalizer import normalize_file_for_llm
from app.core.progress_hub import LLMProgressHub
from app.core.prompts import build_report_prompt
from app.services.llm_service.task_service import LLMTaskService


logger = logging.getLogger(__name__)


def ensure_report_html(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("<") and text.endswith(">"):
        return text
    return f'<div class="report-content"><pre>{html.escape(text)}</pre></div>'


def build_report_callback_payload(report_id: int, details: str, status: str) -> dict:
    return {
        "businessType": "report",
        "data": {
            "reportId": report_id,
            "status": status,
            "details": details,
        },
        "msg": "生成成功" if status == "1" else "生成失败",
    }


def _publish_progress(progress_hub: LLMProgressHub, report_id: int, progress: float) -> None:
    progress_hub.publish(
        "report",
        str(report_id),
        {"businessType": "report", "data": {"reportId": report_id, "progress": progress}},
    )


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

        task_service.update_task_progress("report", str(report_id), progress=0.35, message="正在生成报告")
        _publish_progress(progress_hub, report_id, 0.35)

        client = AnythingLLMClient(load_anythingllm_config())
        details = run_anythingllm_rag(
            client=client,
            files_to_upload=files_to_upload,
            prompt=build_report_prompt(params),
            workspace_name=f"llm-report-{report_id}-{int(time.time() * 1000)}",
            thread_name=f"report-{report_id}",
            user_id=1,
            mode="query",
            reuse_workspace=False,
        )
        html_details = ensure_report_html(details or "")
        callback_payload = build_report_callback_payload(report_id, html_details, status="1")

        task_service.mark_business_result("report", str(report_id), callback_payload, status="1", message="报告生成完成")
        _publish_progress(progress_hub, report_id, 1.0)

        if callback_url:
            if post_callback_payload(callback_url, callback_payload, timeout=callback_timeout):
                task_service.mark_callback_success("report", str(report_id))
                logger.info("回调结果提交成功: report_id=%s", report_id)
            else:
                task_service.mark_callback_failed("report", str(report_id), "callback failed")
                logger.warning("回调结果提交失败: report_id=%s", report_id)
        
        logger.info("报告生成任务完成: report_id=%s", report_id)
    except Exception as e:
        logger.exception("报告生成任务执行异常: report_id=%s, error=%s", report_id, e)
        callback_payload = build_report_callback_payload(report_id, "", status="2")
        task_service.mark_business_result("report", str(report_id), callback_payload, status="2", message="报告生成失败")
        _publish_progress(progress_hub, report_id, 1.0)
        if callback_url:
            if post_callback_payload(callback_url, callback_payload, timeout=callback_timeout):
                task_service.mark_callback_success("report", str(report_id))
                logger.info("失败回调提交成功: report_id=%s", report_id)
            else:
                task_service.mark_callback_failed("report", str(report_id), "callback failed")
                logger.warning("失败回调提交失败: report_id=%s", report_id)
