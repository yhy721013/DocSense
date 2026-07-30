"""``POST /llm/generate-report`` 的框架无关入站解析规则。

文件位于 Flask Adapter 包，是因为当前调用方仍是 Flask Blueprint；解析函数本身不读取
``flask.request``。未来 FastAPI Adapter 应复用本文件冻结的契约测试或把纯规则上提到共享
Web 边界，不能反向依赖 Flask 包。本模块只校验并复制 JSON 数据，不访问数据库、文件、
AnythingLLM、回调或后台执行器。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.adapters.web.report_ids import (
    NormalizedReportId,
    ReportIdValidationError,
    normalize_report_id,
)
from app.modules.report.domain import ReportId, ReportSubmission


class ReportRequestValidationError(ValueError):
    """报告生成 HTTP 请求违反已经确认的公开参数契约。"""


@dataclass(frozen=True)
class ParsedReportRequest:
    """完成公开校验后交给报告应用服务的解析结果。

    ``request_payload`` 保留深复制后的完整兼容快照，供遗留行为回归测试与过渡工具读取；
    当前生产路由通过 :meth:`to_submission` 只把既有第一项业务语义映射为不可变领域命令。
    解析器不会修改 Flask 提供的原始对象。
    """

    request_payload: dict[str, Any]
    params: dict[str, Any]
    report_id: NormalizedReportId

    def to_submission(self, *, trace_id: str) -> ReportSubmission:
        """把已校验 HTTP 副本转换为不可变报告应用命令。

        这里只做边界 DTO 映射，不生成 TaskId、不访问 SQLite，也不触发文件或网络 I/O。
        ``trace_id`` 由请求入口生成并随 execution 持久化，不能从调用方新增参数读取。
        """

        return ReportSubmission(
            report_id=ReportId.from_public_value(self.report_id.value),
            source_urls=tuple(self.params["filePathList"]),
            template_outline_url=self.params["templateOutline"],
            template_desc=self.params.get("templateDesc", ""),
            requirement=self.params.get("requirement", ""),
            trace_id=trace_id,
        )


def parse_report_request(payload: object) -> ParsedReportRequest:
    """完整校验报告生成请求，并返回隔离副本及领域命令映射入口。

    校验顺序属于公开契约的一部分：顶层必须是对象，随后校验 ``businessType``、完整
    ``params`` 容器、首项报告字段。``params`` 出现任意非对象元素时拒绝整次请求，不能
    过滤后继续受理；``filePathList`` 的每一项必须是非空字符串。
    """

    if not isinstance(payload, Mapping):
        raise ReportRequestValidationError("请求体必须是JSON对象")

    if payload.get("businessType") != "report":
        raise ReportRequestValidationError("businessType必须为report")

    raw_params = payload.get("params")
    if not isinstance(raw_params, list) or not raw_params:
        raise ReportRequestValidationError("params不能为空")
    if any(not isinstance(item, Mapping) for item in raw_params):
        raise ReportRequestValidationError("params元素必须是对象")

    first_params = raw_params[0]
    report_id_value = first_params.get("reportId")
    if report_id_value is None:
        raise ReportRequestValidationError("reportId不能为空")
    try:
        normalized_report_id = normalize_report_id(report_id_value)
    except ReportIdValidationError as exc:
        # 统一转换为本接口的请求异常，Blueprint 不需要了解 reportId 规范化器的实现类型。
        raise ReportRequestValidationError(str(exc)) from exc

    file_path_list = first_params.get("filePathList")
    if not isinstance(file_path_list, list) or not file_path_list:
        raise ReportRequestValidationError("filePathList不能为空")
    for index, file_path in enumerate(file_path_list, start=1):
        if not isinstance(file_path, str) or not file_path.strip():
            raise ReportRequestValidationError(
                f"filePathList中第{index}项不是有效字符串"
            )

    template_outline = first_params.get("templateOutline")
    if not isinstance(template_outline, str) or not template_outline.strip():
        raise ReportRequestValidationError("templateOutline不能为空")

    # 所有 params 元素已经证明为 Mapping。复制完整数组可以保留额外对象、字段、顺序和
    # 重复值；当前执行链仍只使用第一项，但不能在适配过程中静默丢弃调用方数据。
    copied_params = [deepcopy(dict(item)) for item in raw_params]
    copied_params[0]["reportId"] = normalized_report_id.value

    # 遗留 Prompt 通过 f-string 消费这两个可选字段，历史上数字、布尔值、对象乃至显式
    # null 都会被兼容性字符串化。领域层只接收 str，因此在 Web 边界一次性冻结等价文本；
    # 字段缺失仍使用旧默认空串。这里不裁剪内容，也不把转换后的业务文本写入日志。
    for field_name in ("templateDesc", "requirement"):
        raw_value = first_params.get(field_name, "")
        copied_params[0][field_name] = (
            raw_value if isinstance(raw_value, str) else str(raw_value)
        )
    copied_payload = deepcopy(dict(payload))
    copied_payload["params"] = copied_params

    return ParsedReportRequest(
        request_payload=copied_payload,
        params=copied_params[0],
        report_id=normalized_report_id,
    )


__all__ = [
    "ParsedReportRequest",
    "ReportRequestValidationError",
    "parse_report_request",
]
