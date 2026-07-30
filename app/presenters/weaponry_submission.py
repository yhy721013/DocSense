"""武器谱受理结果到已批准 HTTP 202/400/404/409 的框架无关 Presenter。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging


logger = logging.getLogger(__name__)

_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_WEAPONRY_CONFLICT_MESSAGE = "任务正在处理中"


def _error_body(message: str) -> bytes:
    if not isinstance(message, str):
        raise TypeError("message 必须是 str")
    normalized = message.strip()
    if not normalized:
        raise ValueError("message 不能为空")
    return json.dumps(
        {"error": normalized},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class WeaponrySubmissionHttpPresentation:
    """Flask/FastAPI 都可机械转换为真实响应的不可变展示值。"""

    status_code: int
    body: bytes
    content_type: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or self.status_code < 100
            or self.status_code > 599
        ):
            raise ValueError("status_code 必须是合法 HTTP 状态码")
        if not isinstance(self.body, bytes):
            raise TypeError("body 必须是 bytes")
        if self.content_type is not None and (
            not isinstance(self.content_type, str)
            or not self.content_type.strip()
        ):
            raise ValueError("content_type 必须是非空 str 或 None")


class WeaponrySubmissionResponsePresenter:
    """只公开批准的状态、单字段错误和严格零字节成功体。"""

    def present_success(self) -> WeaponrySubmissionHttpPresentation:
        # 内部 task_id、文档键、profile 和 Dispatcher 状态均不得出现在甲方响应中。
        logger.debug("映射武器谱受理空响应: status_code=202")
        return WeaponrySubmissionHttpPresentation(status_code=202, body=b"")

    def present_conflict(self) -> WeaponrySubmissionHttpPresentation:
        logger.debug("映射武器谱活动任务或Callback Guard冲突: status_code=409")
        return self._present_error(409, _WEAPONRY_CONFLICT_MESSAGE)

    def present_bad_request(
        self,
        message: str,
    ) -> WeaponrySubmissionHttpPresentation:
        logger.debug("映射武器谱请求或文档歧义错误: status_code=400")
        return self._present_error(400, message)

    def present_not_found(
        self,
        message: str,
    ) -> WeaponrySubmissionHttpPresentation:
        logger.debug("映射武器谱未解析文档错误: status_code=404")
        return self._present_error(404, message)

    @staticmethod
    def _present_error(
        status_code: int,
        message: str,
    ) -> WeaponrySubmissionHttpPresentation:
        return WeaponrySubmissionHttpPresentation(
            status_code=status_code,
            body=_error_body(message),
            content_type=_JSON_CONTENT_TYPE,
        )


__all__ = [
    "WeaponrySubmissionHttpPresentation",
    "WeaponrySubmissionResponsePresenter",
]
