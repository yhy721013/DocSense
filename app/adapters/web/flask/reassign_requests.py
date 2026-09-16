"""``POST /llm/reassign`` 的框架无关入站解析规则。

该解析器只复现已经冻结的公开请求校验顺序和历史兼容点：原始 ID 比较、
``int(oldArchitectureId)`` 的转换时点，以及 ``newArchitectureId`` 不额外收紧类型。
它不访问数据库、AnythingLLM、线程或 Flask 全局对象；蓝图只负责把 Flask 已解析的
JSON 值交给本模块。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.reassign.domain import ReassignDocumentCommand


class ReassignRequestValidationError(ValueError):
    """分类节点变更请求违反已确认公开契约时使用的稳定参数错误。"""


@dataclass(frozen=True)
class ParsedReassignRequest:
    """已完成公开校验、可交给 Application 的不可变请求快照。

    原始新旧分类值独立保存，仅供 Presenter 原样复现既有响应字段。业务服务只接收
    ``command``，不会知道 HTTP 字段名，也不会把内部 Operation 信息返回给调用方。
    """

    command: ReassignDocumentCommand
    file_name: str
    old_architecture_id: object
    new_architecture_id: object

    def __post_init__(self) -> None:
        if not isinstance(self.command, ReassignDocumentCommand):
            raise TypeError("command 必须是 ReassignDocumentCommand")
        if not isinstance(self.file_name, str) or not self.file_name:
            raise ValueError("file_name 必须是非空字符串")
        if self.command.file_name != self.file_name:
            raise ValueError("command.file_name 必须与 file_name 保持一致")


def parse_reassign_request(payload: object) -> ParsedReassignRequest:
    """按遗留路由的顺序解析分类节点变更请求。

    ``payload or {}`` 和随后的 ``.get`` 故意保留旧实现的非对象兼容语义：空值会按空
    对象进入 ``businessType`` 校验，非空 JSON 数组等非法根值仍会在 ``.get`` 处触发原始
    ``AttributeError``，由 Flask 维持既有 500 边界。这里不能擅自把它改成新的 400。
    """

    # 不使用 ``Mapping`` 类型宽松校验，避免改变当前 ``dict`` 专用 params 规则和非对象
    # 根 JSON 的历史失败时点。
    raw_payload = payload or {}
    if raw_payload.get("businessType") != "reassign":
        raise ReassignRequestValidationError("businessType必须为reassign")

    params = raw_payload.get("params")
    if not isinstance(params, dict):
        raise ReassignRequestValidationError("params不能为空")

    file_name = params.get("fileName")
    if not isinstance(file_name, str) or not file_name.strip():
        raise ReassignRequestValidationError("fileName不能为空")
    normalized_file_name = file_name.strip()

    old_architecture_id = params.get("oldArchitectureId")
    if old_architecture_id is None:
        raise ReassignRequestValidationError("oldArchitectureId不能为空")

    new_architecture_id = params.get("newArchitectureId")
    if new_architecture_id is None:
        raise ReassignRequestValidationError("newArchitectureId不能为空")

    # 必须使用 Python 原始值比较，不能把 1、"1" 或 false 在 HTTP 层提前归一化。
    if old_architecture_id == new_architecture_id:
        raise ReassignRequestValidationError(
            "oldArchitectureId与newArchitectureId不能相同"
        )

    # 这是冻结的兼容边界：转换异常不包装成新的 ValidationError，保持原路由的 Flask 500
    # 时点和默认错误体。领域命令只做二次一致性断言，不拥有该公开转换语义。
    old_architecture_id_query_value = int(old_architecture_id)
    command = ReassignDocumentCommand(
        file_name=normalized_file_name,
        old_architecture_id_raw=old_architecture_id,
        new_architecture_id_raw=new_architecture_id,
        old_architecture_id_query_value=old_architecture_id_query_value,
    )
    return ParsedReassignRequest(
        command=command,
        file_name=normalized_file_name,
        old_architecture_id=old_architecture_id,
        new_architecture_id=new_architecture_id,
    )


__all__ = [
    "ParsedReassignRequest",
    "ReassignRequestValidationError",
    "parse_reassign_request",
]
