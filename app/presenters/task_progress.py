"""Progress 应用快照到既有 WebSocket 消息的框架无关 Presenter。"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.modules.tasks.application import CurrentProgressItem
from app.modules.tasks.domain import ProgressKey, ProgressSnapshot


logger = logging.getLogger(__name__)


class ProgressWebSocketPresenter:
    """唯一负责 Progress 公开字段、业务键类型和错误结构的组件。"""

    def present_current(self, item: CurrentProgressItem) -> dict[str, Any]:
        if not isinstance(item, CurrentProgressItem):
            raise TypeError("item 必须是 CurrentProgressItem")
        if item.snapshot is None:
            return self._build_message(
                item.key,
                progress=0.0,
                exists=False,
            )
        return self.present_snapshot(item.snapshot)

    def present_snapshot(self, snapshot: ProgressSnapshot) -> dict[str, Any]:
        if not isinstance(snapshot, ProgressSnapshot):
            raise TypeError("snapshot 必须是 ProgressSnapshot")
        return self._build_message(snapshot.key, progress=snapshot.progress)

    @staticmethod
    def present_error(message: str) -> dict[str, str]:
        if not isinstance(message, str):
            raise TypeError("message 必须是 str")
        normalized = message.strip()
        if not normalized:
            raise ValueError("message 不能为空")
        return {"type": "error", "message": normalized}

    @staticmethod
    def serialize(message: dict[str, Any]) -> str:
        """生成严格 JSON 文本，拒绝 NaN/Infinity 等非标准值。"""

        if not isinstance(message, dict):
            raise TypeError("message 必须是 dict")
        return json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _build_message(
        key: ProgressKey,
        *,
        progress: float,
        exists: bool | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"progress": progress}
        if key.business_type == "file":
            data["fileName"] = key.business_key
        elif key.business_type == "report":
            # 旧实现和接口文档均把 reportId 输出为 Long/JSON number。
            data["reportId"] = int(key.business_key)
        elif key.business_type == "weaponry":
            # 文档已冻结 weaponry 的公开身份为 JSON number；入站适配层已经把
            # "1"、"0001" 等兼容写法规范为同一无前导零业务键。
            try:
                data["architectureId"] = int(key.business_key)
            except (TypeError, ValueError) as exc:
                logger.error(
                    "武器谱 Progress 业务键无法映射为公开整数: business_key=%s",
                    key.business_key,
                )
                raise ValueError("weaponry Progress business_key 必须是整数") from exc
        else:
            raise ValueError("不支持的 Progress business_type")
        if exists is False:
            data["exists"] = False
        message = {"businessType": key.business_type, "data": data}
        logger.debug(
            "已映射 Progress WebSocket 消息: business_type=%s business_key=%s "
            "exists=%s",
            key.business_type,
            key.business_key,
            exists is not False,
        )
        return message


__all__ = ["ProgressWebSocketPresenter"]
