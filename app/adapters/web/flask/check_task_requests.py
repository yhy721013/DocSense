"""``POST /llm/check-task`` 的严格、无副作用请求解析器。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging

from app.adapters.web import (
    ArchitectureIdValidationError,
    ReportIdValidationError,
    normalize_architecture_id,
    normalize_report_id,
)
from app.modules.tasks.application import (
    CheckTaskRequest,
    ExecuteCheckTaskCommand,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskLookupItem


logger = logging.getLogger(__name__)
_BUSINESS_TYPES = frozenset({"file", "report", "weaponry"})


class CheckTaskRequestValidationError(ValueError):
    """check-task 请求违反当前公开参数契约。"""


@dataclass(frozen=True)
class ParsedCheckTaskRequest:
    """供薄路由直接交给 Application 的已校验结果。"""

    command: ExecuteCheckTaskCommand


def parse_check_task_request(payload: object) -> ParsedCheckTaskRequest:
    """先校验整个批次，再按规范业务键去重；本函数不读取任务或发送回调。"""

    normalized_payload = payload if isinstance(payload, Mapping) else {}
    business_type = normalized_payload.get("businessType")
    if business_type not in _BUSINESS_TYPES:
        raise CheckTaskRequestValidationError("businessType无效")

    params = normalized_payload.get("params")
    if (
        not isinstance(params, list)
        or not params
        or any(not isinstance(item, Mapping) for item in params)
    ):
        raise CheckTaskRequestValidationError("params不能为空")

    unique_items: list[TaskLookupItem] = []
    first_index_by_key: dict[str, int] = {}
    duplicate_count = 0
    for index, item in enumerate(params):
        normalized_key, response_key, response_value = _parse_business_key(
            business_type,
            item,
        )
        first_index = first_index_by_key.get(normalized_key)
        if first_index is not None:
            duplicate_count += 1
            logger.info(
                "任务查询请求跳过规范化重复项: business_type=%s index=%d "
                "first_index=%d",
                business_type,
                index,
                first_index,
            )
            continue
        first_index_by_key[normalized_key] = index
        unique_items.append(
            TaskLookupItem(
                business_ref=TaskBusinessRef(business_type, normalized_key),
                response_key=response_key,
                response_value=response_value,
            )
        )

    return ParsedCheckTaskRequest(
        command=ExecuteCheckTaskCommand(
            request=CheckTaskRequest(tuple(unique_items)),
            requested_count=len(params),
            duplicate_count=duplicate_count,
        )
    )


def _parse_business_key(
    business_type: str,
    item: Mapping[object, object],
) -> tuple[str, str, str | int]:
    """按当前业务类型返回内部键、公开键名和规范公开值。"""

    if business_type == "file":
        file_name = item.get("fileName")
        if not isinstance(file_name, str) or not file_name.strip():
            raise CheckTaskRequestValidationError("fileName不能为空")
        normalized = file_name.strip()
        return normalized, "fileName", normalized

    if business_type == "weaponry":
        raw_architecture_id = item.get("architectureId")
        if raw_architecture_id is None:
            raise CheckTaskRequestValidationError("architectureId不能为空")
        try:
            architecture_id = normalize_architecture_id(raw_architecture_id)
        except ArchitectureIdValidationError as exc:
            raise CheckTaskRequestValidationError(str(exc)) from exc
        return (
            architecture_id.business_key,
            "architectureId",
            architecture_id.value,
        )

    raw_report_id = item.get("reportId")
    if raw_report_id is None:
        raise CheckTaskRequestValidationError("reportId不能为空")
    try:
        report_id = normalize_report_id(raw_report_id)
    except ReportIdValidationError as exc:
        raise CheckTaskRequestValidationError(str(exc)) from exc
    return report_id.business_key, "reportId", report_id.value


__all__ = [
    "CheckTaskRequestValidationError",
    "ParsedCheckTaskRequest",
    "parse_check_task_request",
]
