from __future__ import annotations

import html
import time
from pathlib import Path

from anythingllm_client import AnythingLLMClient
from config import load_anythingllm_config
from pipeline import prepare_upload_files, run_anythingllm_rag

from app.services.llm_callback_service import post_callback_payload
from app.services.llm_download_service import download_to_temp_file
from app.services.llm_progress_hub import LLMProgressHub
from app.services.llm_prompts import build_report_prompt
from app.services.llm_task_service import LLMTaskService


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

    try:
        task_service.update_task_progress("report", str(report_id), progress=0.10, message="正在下载报告文件", status="0")
        _publish_progress(progress_hub, report_id, 0.10)

        files_to_upload = []
        file_path_list = params.get("filePathList", [])
        for index, file_url in enumerate(file_path_list, start=1):
            downloaded_path = download_to_temp_file(file_url, f"report-{report_id}-{index}{Path(file_url).suffix}", download_root, timeout=60)
            files_to_upload.extend(prepare_upload_files(downloaded_path))
            download_progress = 0.10 + 0.20 * (index / max(len(file_path_list), 1))
            task_service.update_task_progress("report", str(report_id), progress=download_progress, message=f"已下载 {index}/{len(file_path_list)} 个文件")
            _publish_progress(progress_hub, report_id, download_progress)

        task_service.update_task_progress("report", str(report_id), progress=0.40, message="文件下载完成，正在初始化报告生成")
        _publish_progress(progress_hub, report_id, 0.40)

        client = AnythingLLMClient(load_anythingllm_config())

        task_service.update_task_progress("report", str(report_id), progress=0.55, message="正在执行AI报告生成")
        _publish_progress(progress_hub, report_id, 0.55)

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

        task_service.update_task_progress("report", str(report_id), progress=0.80, message="AI生成完成，正在处理报告内容")
        _publish_progress(progress_hub, report_id, 0.80)

        html_details = ensure_report_html(details or "")

        task_service.update_task_progress("report", str(report_id), progress=0.90, message="正在准备回调数据")
        _publish_progress(progress_hub, report_id, 0.90)

        callback_payload = build_report_callback_payload(report_id, html_details, status="1")

        task_service.mark_business_result("report", str(report_id), callback_payload, status="1", message="报告生成完成")
        _publish_progress(progress_hub, report_id, 1.0)

        if callback_url:
            if post_callback_payload(callback_url, callback_payload, timeout=callback_timeout):
                task_service.mark_callback_success("report", str(report_id))
            else:
                task_service.mark_callback_failed("report", str(report_id), "callback failed")
    except Exception:
        callback_payload = build_report_callback_payload(report_id, "", status="2")
        task_service.mark_business_result("report", str(report_id), callback_payload, status="2", message="报告生成失败")
        _publish_progress(progress_hub, report_id, 1.0)
        if callback_url:
            if post_callback_payload(callback_url, callback_payload, timeout=callback_timeout):
                task_service.mark_callback_success("report", str(report_id))
            else:
                task_service.mark_callback_failed("report", str(report_id), "callback failed")
