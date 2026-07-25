"""从共享 SQLite latest 投影恢复武器谱同步回调候选。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging

from app.modules.tasks.domain import TaskId
from app.modules.weaponry.domain import (
    WEAPONRY_STATUS_FAILED,
    WEAPONRY_STATUS_SUCCEEDED,
    WeaponryAnalyseDataSource,
    WeaponryCallbackPayload,
    WeaponryColumnSpecification,
    WeaponryFieldResult,
    WeaponryFieldSpecification,
    WeaponryTableCellResult,
    normalize_architecture_id_value,
)
from app.modules.weaponry.ports import (
    WeaponryCallbackRecoveryCandidate,
    WeaponryCallbackRecoverySourcePort,
)
from app.services.llm_service.task_service import LLMTaskService


logger = logging.getLogger(__name__)

_WEAPONRY_BUSINESS_TYPE = "weaponry"
_RECOVERABLE_CALLBACK_STATUSES = frozenset({"pending", "failed"})
_TERMINAL_STATUSES = frozenset({WEAPONRY_STATUS_SUCCEEDED, WEAPONRY_STATUS_FAILED})


def _mapping(value: object, *, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"武器谱公开回调载荷 {name} 必须是对象")
    return value


def _sequence(value: object, *, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise RuntimeError(f"武器谱公开回调载荷 {name} 必须是数组")
    return value


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"武器谱公开回调载荷 {name} 必须是字符串")
    return value


def _source(value: object, *, name: str) -> WeaponryAnalyseDataSource:
    item = _mapping(value, name=name)
    rows = tuple(
        _text(row, name=f"{name}.rows")
        for row in _sequence(item.get("rows"), name=f"{name}.rows")
    )
    return WeaponryAnalyseDataSource(
        content=_text(item.get("content"), name=f"{name}.content"),
        source=_text(item.get("source"), name=f"{name}.source"),
        occurred_at=_text(item.get("time"), name=f"{name}.time"),
        file_name=_text(item.get("fileName"), name=f"{name}.fileName"),
        rows=rows,
        translation=_text(item.get("translate"), name=f"{name}.translate"),
    )


def _sources(value: object, *, name: str) -> tuple[WeaponryAnalyseDataSource, ...]:
    return tuple(
        _source(item, name=f"{name}[{index}]")
        for index, item in enumerate(_sequence(value, name=name))
    )


def _field(value: object, *, index: int) -> WeaponryFieldResult:
    item = _mapping(value, name=f"weaponryTemplateFieldList[{index}]")
    specification = WeaponryFieldSpecification.from_mapping(item)
    if specification.field_type == "INPUT":
        return WeaponryFieldResult(
            specification=specification,
            analyse_data=_text(
                item.get("analyseData"),
                name=f"weaponryTemplateFieldList[{index}].analyseData",
            ),
            sources=_sources(
                item.get("analyseDataSource"),
                name=f"weaponryTemplateFieldList[{index}].analyseDataSource",
            ),
        )

    raw_rows = _sequence(
        item.get("tableFieldList"),
        name=f"weaponryTemplateFieldList[{index}].tableFieldList",
    )
    # 零结果 TABLE 会原样保留请求中的列模板；只有真正的结果单元格才必然带
    # ``analyseData``。不要把原始列模板误解成一行业务结果，否则同步恢复会篡改 payload。
    has_result_cells = any(
        isinstance(cell, Mapping) and "analyseData" in cell
        for row in raw_rows
        for cell in _sequence(row, name="tableFieldList row")
    )
    if not has_result_cells:
        return WeaponryFieldResult(specification=specification)

    table_rows: list[tuple[WeaponryTableCellResult, ...]] = []
    for row_index, raw_row in enumerate(raw_rows):
        cells: list[WeaponryTableCellResult] = []
        for cell_index, raw_cell in enumerate(
            _sequence(raw_row, name=f"tableFieldList[{row_index}]")
        ):
            cell = _mapping(
                raw_cell,
                name=f"tableFieldList[{row_index}][{cell_index}]",
            )
            cells.append(
                WeaponryTableCellResult(
                    specification=WeaponryColumnSpecification.from_mapping(cell),
                    analyse_data=_text(
                        cell.get("analyseData"),
                        name=f"tableFieldList[{row_index}][{cell_index}].analyseData",
                    ),
                    sources=_sources(
                        cell.get("analyseDataSource"),
                        name=(
                            f"tableFieldList[{row_index}][{cell_index}]"
                            ".analyseDataSource"
                        ),
                    ),
                )
            )
        table_rows.append(tuple(cells))
    return WeaponryFieldResult(
        specification=specification,
        table_rows=tuple(table_rows),
    )


def _decode_callback_payload(
    payload: Mapping[str, object],
    *,
    expected_architecture_id: int,
) -> WeaponryCallbackPayload:
    if payload.get("businessType") != _WEAPONRY_BUSINESS_TYPE:
        raise RuntimeError("武器谱公开回调载荷 businessType 无效")
    data = _mapping(payload.get("data"), name="data")
    architecture_id = normalize_architecture_id_value(data.get("architectureId"))
    if architecture_id != expected_architecture_id:
        raise RuntimeError("武器谱公开回调载荷与 latest 业务键不一致")
    status = _text(data.get("status"), name="data.status")
    message = _text(payload.get("msg"), name="msg")
    if status == WEAPONRY_STATUS_FAILED:
        fields: tuple[WeaponryFieldResult, ...] = ()
    elif status == WEAPONRY_STATUS_SUCCEEDED:
        fields = tuple(
            _field(item, index=index)
            for index, item in enumerate(
                _sequence(
                    data.get("weaponryTemplateFieldList"),
                    name="data.weaponryTemplateFieldList",
                )
            )
        )
    else:
        raise RuntimeError("武器谱公开回调载荷 status 不是终态")
    callback = WeaponryCallbackPayload(
        architecture_id=architecture_id,
        status=status,
        message=message,
        fields=fields,
    )
    if callback.to_public_dict() != dict(payload):
        # 恢复发送必须做到字节语义等价；无法无损重建时宁可停止，也不能向甲方发送被
        # 重新解释或补字段后的 payload。
        raise RuntimeError("武器谱公开回调载荷无法无损重建")
    return callback


class SQLiteWeaponryCallbackRecoverySource(WeaponryCallbackRecoverySourcePort):
    """只读取 latest 公共投影，不重新运行字段抽取或访问 AnythingLLM。"""

    def __init__(self, task_service: LLMTaskService) -> None:
        if not isinstance(task_service, LLMTaskService):
            raise TypeError("task_service 必须是 LLMTaskService")
        self._task_service = task_service

    def load_recoverable(
        self,
        architecture_id: int,
    ) -> WeaponryCallbackRecoveryCandidate | None:
        normalized_id = normalize_architecture_id_value(architecture_id)
        task = self._task_service.get_task(
            _WEAPONRY_BUSINESS_TYPE,
            str(normalized_id),
        )
        if task is None:
            return None
        if task.get("status") not in _TERMINAL_STATUSES:
            return None
        if task.get("callback_status") not in _RECOVERABLE_CALLBACK_STATUSES:
            return None
        raw_payload = task.get("result_payload")
        payload = _mapping(raw_payload, name="root")
        candidate = WeaponryCallbackRecoveryCandidate(
            task_id=TaskId(task.get("execution_id")),  # type: ignore[arg-type]
            architecture_id=normalized_id,
            payload=_decode_callback_payload(
                payload,
                expected_architecture_id=normalized_id,
            ),
        )
        logger.debug(
            "已加载武器谱同步回调恢复候选: task_id=%s architecture_id=%s "
            "callback_status=%s",
            candidate.task_id.value,
            normalized_id,
            task.get("callback_status"),
        )
        return candidate


__all__ = ["SQLiteWeaponryCallbackRecoverySource"]
