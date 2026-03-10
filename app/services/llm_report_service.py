from __future__ import annotations

import html


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
