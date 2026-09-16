"""分类节点变更 Application 结果到冻结 HTTP 200/400/500 语义的 Presenter。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging

from app.modules.reassign.domain import ReassignmentResult


logger = logging.getLogger(__name__)

_JSON_CONTENT_TYPE = "application/json"


def _legacy_json_body(value: dict[str, object]) -> bytes:
    """生成与当前 Flask ``jsonify`` 相同的稳定 JSON 字节序列。

    历史路由使用 Flask 默认 JSON Provider：键名排序、ASCII 转义、紧凑分隔符且末尾换行。
    这里显式复刻该格式，既让 Presenter 保持框架无关，也让 1E-6 可以逐字节锁住既有响应。
    """

    return (
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


@dataclass(frozen=True)
class ReassignHttpPresentation:
    """可被 Flask/FastAPI 机械转换的不可变 HTTP 展示值。"""

    status_code: int
    body: bytes
    content_type: str = _JSON_CONTENT_TYPE

    def __post_init__(self) -> None:
        if (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or not 100 <= self.status_code <= 599
        ):
            raise ValueError("status_code 必须是合法 HTTP 状态码")
        if not isinstance(self.body, bytes):
            raise TypeError("body 必须是 bytes")
        if not isinstance(self.content_type, str) or not self.content_type.strip():
            raise ValueError("content_type 必须是非空字符串")
        object.__setattr__(self, "content_type", self.content_type.strip())


class ReassignResponsePresenter:
    """只输出接口文档批准的字段、状态码和稳定文案。"""

    def present_bad_request(self, message: str) -> ReassignHttpPresentation:
        """映射 Parser 已确认的既有 400 单字段错误结构。"""

        if not isinstance(message, str) or not message:
            raise ValueError("message 必须是非空字符串")
        logger.debug("映射分类节点变更参数错误: status_code=400")
        return ReassignHttpPresentation(
            status_code=400,
            body=_legacy_json_body({"error": message}),
        )

    def present_result(
        self,
        *,
        file_name: str,
        old_architecture_id: object,
        new_architecture_id: object,
        result: ReassignmentResult,
    ) -> ReassignHttpPresentation:
        """把最小 Application 结果映射为冻结的成功或业务失败结构。

        Presenter 不接触 Repository、Knowledge Port 或内部错误详情；所有非成功结果均使用
        已确认的 500 顶层结构，因此 ``operation_id``、租约、fencing 和步骤永远无出口。
        """

        if not isinstance(file_name, str) or not file_name:
            raise ValueError("file_name 必须是非空字符串")
        if not isinstance(result, ReassignmentResult):
            raise TypeError("result 必须是 ReassignmentResult")

        success = result.success
        payload = {
            "businessType": "reassign",
            "msg": "变更成功" if success else "变更失败",
            "data": {
                "fileName": file_name,
                "oldArchitectureId": old_architecture_id,
                "newArchitectureId": new_architecture_id,
                "success": success,
                "message": result.public_message_text,
            },
        }
        status_code = 200 if success else 500
        logger.debug(
            "映射分类节点变更业务结果: status_code=%s result_category=%s",
            status_code,
            result.category.value,
        )
        return ReassignHttpPresentation(
            status_code=status_code,
            body=_legacy_json_body(payload),
        )


__all__ = ["ReassignHttpPresentation", "ReassignResponsePresenter"]
