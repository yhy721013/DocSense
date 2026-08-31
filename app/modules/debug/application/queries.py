"""Debug 只读查询用例及稳定错误收敛。"""

from __future__ import annotations

import json
import logging

from app.modules.debug.application.models import (
    CallbackPreviewResult,
    ChatBootstrapResult,
)
from app.modules.debug.ports.callback_history import CallbackHistoryReadPort
from app.modules.debug.ports.chat_snapshot import ChatDebugSnapshotReadPort


logger = logging.getLogger(__name__)
CALLBACK_RECORD_LIMIT = 50


class LoadCallbackPreview:
    """列举 Callback 历史并读取一个 JSON 对象，保持既有 Debug 文案。"""

    def __init__(self, history: CallbackHistoryReadPort) -> None:
        self._history = history

    def execute(self, *, record: str | None = None) -> CallbackPreviewResult:
        logger.info("开始读取 Callback 调试历史: has_requested_record=%s", bool(record))
        try:
            records = self._history.list_records(limit=CALLBACK_RECORD_LIMIT)
        except Exception as exc:
            logger.error(
                "Callback 调试历史列表读取失败: stage=list error_type=%s",
                type(exc).__name__,
            )
            return CallbackPreviewResult(
                ok=False,
                message="回调文件读取失败",
                payload=None,
                records=(),
                selected_record=None,
            )

        selected_record = None
        if record:
            selected_record = self._history.find_record(record)
        elif records:
            selected_record = records[0]

        if selected_record is None:
            message = (
                "当前还没有新版回调历史文件"
                if not records and not record
                else "指定的回调历史记录不存在"
            )
            logger.info(
                "Callback 调试历史读取完成: status=not_found record_count=%d",
                len(records),
            )
            return CallbackPreviewResult(
                ok=False,
                message=message,
                payload=None,
                records=records,
                selected_record=None,
            )

        read_result = self._history.read_record(selected_record.record_id)
        if read_result.text is None:
            logger.warning(
                "Callback 调试记录不可读: status=read_failed error_kind=%s",
                read_result.error_kind or "unknown",
            )
            return CallbackPreviewResult(
                ok=False,
                message="回调文件读取失败",
                payload=None,
                records=records,
                selected_record=selected_record,
            )

        try:
            payload = json.loads(read_result.text)
        except json.JSONDecodeError:
            logger.warning("Callback 调试记录不是合法 JSON: status=invalid_json")
            return CallbackPreviewResult(
                ok=False,
                message="回调文件不是合法 JSON",
                payload=None,
                records=records,
                selected_record=selected_record,
            )

        if not isinstance(payload, dict):
            logger.warning("Callback 调试记录根节点不是对象: status=non_object")
            return CallbackPreviewResult(
                ok=False,
                message="回调文件根节点必须为对象",
                payload=None,
                records=records,
                selected_record=selected_record,
            )

        logger.info(
            "Callback 调试历史读取完成: status=success record_count=%d",
            len(records),
        )
        return CallbackPreviewResult(
            ok=True,
            message="读取成功",
            payload=payload,
            records=records,
            selected_record=selected_record,
        )


class LoadChatDebugBootstrap:
    """加载 Chat Debug 初始化快照并稳定收敛存储查询失败。"""

    def __init__(self, snapshots: ChatDebugSnapshotReadPort) -> None:
        self._snapshots = snapshots

    def execute(self) -> ChatBootstrapResult:
        logger.info("开始读取文件对话调试初始化数据")
        try:
            snapshot = self._snapshots.read_snapshot()
        except Exception as exc:
            # 既有 Debug JSON 会携带底层异常文本，本波次为逐字段等价而保留；日志只记录
            # 类型和阶段，不记录 SQL、文件名、消息正文或外部引用。
            logger.error(
                "读取文件对话调试初始化数据失败: stage=read_snapshot error_type=%s",
                type(exc).__name__,
            )
            return ChatBootstrapResult(
                ok=False,
                message=f"读取失败: {exc}",
                sessions=(),
                available_files=(),
            )

        logger.info(
            "文件对话调试初始化数据读取完成: "
            "session_count=%d active_scope_member_count=%d "
            "workspace_binding_count=%d available_file_count=%d",
            len(snapshot.sessions),
            snapshot.active_scope_member_count,
            snapshot.workspace_binding_count,
            len(snapshot.available_files),
        )
        return ChatBootstrapResult(
            ok=True,
            message="读取成功",
            sessions=snapshot.sessions,
            available_files=snapshot.available_files,
            active_scope_member_count=snapshot.active_scope_member_count,
            workspace_binding_count=snapshot.workspace_binding_count,
        )
