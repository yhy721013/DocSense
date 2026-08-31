"""Callback 历史目录的只读 Adapter。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.modules.debug.ports.callback_history import CallbackRecord, CallbackRecordText
from app.services.core.settings import RUNTIME_DIR


CALLBACK_HISTORY_DIR = RUNTIME_DIR / "callback"


class FileCallbackHistoryReadAdapter:
    """安全读取 JSON 历史文件，不写目录、不解析业务 payload。"""

    def __init__(self, history_dir: Path | None = None) -> None:
        # None 表示每次调用时读取模块默认值，便于离线测试安全替换目录；生产默认值仍固定
        # 在项目 runtime/callback 下，且不会读取 .env 或访问网络。
        self._configured_history_dir = history_dir

    @property
    def _history_dir(self) -> Path:
        return self._configured_history_dir or CALLBACK_HISTORY_DIR

    def _safe_record_path(self, record_id: str) -> Path | None:
        """把外部记录名收敛为历史目录内的普通 JSON 文件路径。

        仅检查 ``Path.name`` 不能阻止 ``name.json`` 本身是符号链接。这里同时执行
        单文件名白名单、后缀检查、符号链接拒绝和 resolve 后父目录校验；读取前会
        再调用本方法，避免 ``find_record`` 的旧结果成为后续读取授权。
        """

        if not isinstance(record_id, str) or not record_id:
            return None
        record_path = Path(record_id)
        if record_path.name != record_id or record_path.suffix.lower() != ".json":
            return None

        selected_path = self._history_dir / record_id
        try:
            if selected_path.is_symlink():
                return None
            history_root = self._history_dir.resolve(strict=False)
            resolved_path = selected_path.resolve(strict=False)
        except OSError:
            return None
        if resolved_path.parent != history_root:
            return None
        return resolved_path

    @staticmethod
    def _metadata(path: Path, stat_result: Any | None = None) -> CallbackRecord:
        current_stat = stat_result or path.stat()
        return CallbackRecord(
            record_id=path.name,
            file_name=path.name,
            modified_at=datetime.fromtimestamp(current_stat.st_mtime).isoformat(
                timespec="seconds"
            ),
            size_bytes=current_stat.st_size,
        )

    def list_records(self, *, limit: int) -> tuple[CallbackRecord, ...]:
        target_dir = self._history_dir
        if not target_dir.exists() or not target_dir.is_dir():
            return ()

        records: list[tuple[int, str, Path, Any]] = []
        for path in target_dir.glob("*.json"):
            try:
                # Debug 历史是只读展示面；符号链接即使指向目录内文件也一律拒绝，
                # 避免配置错误或本地低权限进程借此读取非历史文件。
                if path.is_symlink() or not path.is_file():
                    continue
                stat_result = path.stat()
            except OSError:
                # 单个文件与清理任务竞态时跳过该项，不能中断整个只读列表。
                continue
            records.append((stat_result.st_mtime_ns, path.name, path, stat_result))

        records.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return tuple(
            self._metadata(path, stat_result)
            for _, _, path, stat_result in records[:limit]
        )

    def find_record(self, record_id: str) -> CallbackRecord | None:
        selected_path = self._safe_record_path(record_id)
        if selected_path is None:
            return None
        try:
            # 再次检查符号链接，缩短校验和 stat 之间的竞态窗口。
            if selected_path.is_symlink() or not selected_path.is_file():
                return None
            return self._metadata(selected_path)
        except OSError:
            return None

    def read_record(self, record_id: str) -> CallbackRecordText:
        selected_path = self._safe_record_path(record_id)
        if selected_path is None:
            return CallbackRecordText(text=None, error_kind="io")
        try:
            # 读取时重新完成完整路径授权，不能信任更早一次 find_record 的结果。
            if selected_path.is_symlink() or not selected_path.is_file():
                return CallbackRecordText(text=None, error_kind="io")
            text = selected_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return CallbackRecordText(text=None, error_kind="encoding")
        except OSError:
            return CallbackRecordText(text=None, error_kind="io")
        return CallbackRecordText(text=text)
