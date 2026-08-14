"""Weaponry 终态 Callback 快照的 SQLite 唯一物理 Writer。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3

from app.modules.tasks.adapters.sqlite.transaction import SQLiteTransactionManager
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import require_persisted_utc
from app.modules.weaponry.domain import (
    WeaponryAnalyseDataSource,
    WeaponryCallbackPayload,
    WeaponryColumnSpecification,
    WeaponryFieldResult,
    WeaponryFieldSpecification,
    WeaponryTableCellResult,
)
from app.modules.weaponry.ports import WeaponryResultSnapshot

from .task_document_snapshot_store import (
    _BorrowedSnapshotTransaction,
    _SnapshotTransaction,
)


class SQLiteWeaponryResultSnapshotStore:
    """保存完整既有 Callback JSON，并在读取时严格重建领域 DTO。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if (transaction_manager is None) == (connection is None):
            raise ValueError("transaction_manager 与 connection 必须且只能提供一个")
        if transaction_manager is not None and not isinstance(
            transaction_manager,
            SQLiteTransactionManager,
        ):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        if connection is not None and not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection 必须是 sqlite3.Connection")
        self._transactions = transaction_manager
        self._borrowed_connection = connection

    @classmethod
    def from_connection(
        cls,
        connection: sqlite3.Connection,
    ) -> "SQLiteWeaponryResultSnapshotStore":
        return cls(connection=connection)

    def _begin(self, *, read_only: bool = False) -> _SnapshotTransaction:
        if self._transactions is not None:
            return self._transactions.begin(read_only=read_only)
        assert self._borrowed_connection is not None
        return _BorrowedSnapshotTransaction(self._borrowed_connection)

    def save(
        self,
        *,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        payload: WeaponryCallbackPayload,
        created_at: str,
    ) -> WeaponryResultSnapshot:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(business_ref, TaskBusinessRef) or business_ref.business_type != "weaponry":
            raise TypeError("business_ref 必须是 Weaponry TaskBusinessRef")
        if not isinstance(payload, WeaponryCallbackPayload):
            raise TypeError("payload 必须是 WeaponryCallbackPayload")
        if str(payload.architecture_id) != business_ref.business_key:
            raise ValueError("Callback payload 与 business_ref 不一致")
        timestamp = require_persisted_utc(created_at, name="created_at")
        serialized = json.dumps(
            payload.to_public_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self._begin() as transaction:
            connection = transaction.connection
            execution = connection.execute(
                """
                SELECT business_type, business_key FROM llm_task_executions
                WHERE execution_id = ?
                """,
                (task_id.value,),
            ).fetchone()
            if (
                execution is None
                or execution["business_type"] != "weaponry"
                or execution["business_key"] != business_ref.business_key
            ):
                raise ValueError("结果快照与 Weaponry execution 身份不一致")
            existing = connection.execute(
                "SELECT callback_payload_json, result_digest, created_at "
                "FROM weaponry_result_snapshots WHERE task_id = ?",
                (task_id.value,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO weaponry_result_snapshots (
                        task_id, business_key, result_schema_version,
                        callback_payload_json, result_digest, created_at
                    ) VALUES (?, ?, 1, ?, ?, ?)
                    """,
                    (task_id.value, business_ref.business_key, serialized, digest, timestamp),
                )
            elif existing["callback_payload_json"] != serialized or existing["result_digest"] != digest:
                raise ValueError("同一 TaskId 已存在不同 Weaponry 结果快照")
            else:
                # 幂等重入必须返回数据库中已经落盘的时间，不能把本次调用时间伪装成
                # 持久事实。终态恢复可以据此稳定核对同一个结果世代。
                timestamp = require_persisted_utc(
                    str(existing["created_at"]),
                    name="created_at",
                )
            transaction.commit()
        return WeaponryResultSnapshot(task_id, business_ref, payload, digest, timestamp)

    def get(self, task_id: TaskId) -> WeaponryResultSnapshot | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        with self._begin(read_only=True) as transaction:
            row = transaction.connection.execute(
                """
                SELECT result.business_key, result.result_schema_version,
                       result.callback_payload_json, result.result_digest,
                       result.created_at, execution.business_type,
                       execution.business_key AS execution_business_key,
                       execution.result_payload
                FROM weaponry_result_snapshots AS result
                JOIN llm_task_executions AS execution
                  ON execution.execution_id = result.task_id
                WHERE result.task_id = ?
                """,
                (task_id.value,),
            ).fetchone()
            transaction.commit()
        if row is None:
            return None
        if int(row["result_schema_version"]) != 1:
            raise RuntimeError("Weaponry 结果快照 Schema 版本不受支持")
        serialized = str(row["callback_payload_json"])
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        if digest != row["result_digest"]:
            raise RuntimeError("Weaponry 结果快照摘要不一致")
        root_result_payload = row["result_payload"]
        expected_root_payload = json.dumps(
            {"result_ref": f"weaponry-result:v1:{digest}"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if (
            row["business_type"] != "weaponry"
            or row["execution_business_key"] != row["business_key"]
            or root_result_payload not in {None, expected_root_payload}
        ):
            raise RuntimeError("Weaponry 结果快照与根 execution 终态引用不一致")
        try:
            raw = json.loads(serialized)
            payload = _decode_callback_payload(raw)
        except (TypeError, ValueError, KeyError) as exc:
            raise RuntimeError("Weaponry 结果快照已损坏") from exc
        business_ref = TaskBusinessRef("weaponry", str(row["business_key"]))
        if str(payload.architecture_id) != business_ref.business_key:
            raise RuntimeError("Weaponry 结果快照业务身份不一致")
        created_at = require_persisted_utc(str(row["created_at"]), name="created_at")
        return WeaponryResultSnapshot(
            task_id,
            business_ref,
            payload,
            digest,
            created_at,
        )


def _exact_dict(value: object, keys: set[str], *, name: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{name} 字段集合无效")
    return value


def _decode_source(value: object) -> WeaponryAnalyseDataSource:
    source = _exact_dict(
        value,
        {"content", "source", "time", "fileName", "rows", "translate"},
        name="analyseDataSource",
    )
    rows = source["rows"]
    if not isinstance(rows, list) or any(not isinstance(item, str) for item in rows):
        raise ValueError("analyseDataSource.rows 无效")
    return WeaponryAnalyseDataSource(
        content=source["content"],
        source=source["source"],
        occurred_at=source["time"],
        file_name=source["fileName"],
        rows=tuple(rows),
        translation=source["translate"],
    )


def _decode_sources(value: object) -> tuple[WeaponryAnalyseDataSource, ...]:
    if not isinstance(value, list):
        raise ValueError("analyseDataSource 必须是数组")
    return tuple(_decode_source(item) for item in value)


def _clear_analysis(value: dict) -> dict:
    template = deepcopy(value)
    if "analyseData" in template:
        template["analyseData"] = ""
    if "analyseDataSource" in template:
        template["analyseDataSource"] = []
    return template


def _decode_field(value: object) -> WeaponryFieldResult:
    if not isinstance(value, dict):
        raise ValueError("weaponryTemplateFieldList 项必须是对象")
    field_type = value.get("fieldType")
    if field_type == "INPUT":
        specification = WeaponryFieldSpecification.from_mapping(_clear_analysis(value))
        analyse_data = value.get("analyseData")
        if not isinstance(analyse_data, str):
            raise ValueError("INPUT analyseData 无效")
        return WeaponryFieldResult(
            specification=specification,
            analyse_data=analyse_data,
            sources=_decode_sources(value.get("analyseDataSource")),
        )
    if field_type != "TABLE":
        raise ValueError("fieldType 无效")
    rows = value.get("tableFieldList")
    if not isinstance(rows, list) or not rows:
        raise ValueError("TABLE tableFieldList 无效")
    template = deepcopy(value)
    template_rows: list[list[dict]] = []
    decoded_rows: list[tuple[WeaponryTableCellResult, ...]] = []
    for row in rows:
        if not isinstance(row, list) or not row:
            raise ValueError("TABLE 行无效")
        template_row: list[dict] = []
        decoded_row: list[WeaponryTableCellResult] = []
        for cell in row:
            if not isinstance(cell, dict):
                raise ValueError("TABLE 单元格无效")
            cell_template = _clear_analysis(cell)
            template_row.append(cell_template)
            analyse_data = cell.get("analyseData")
            if not isinstance(analyse_data, str):
                raise ValueError("TABLE 单元格 analyseData 无效")
            decoded_row.append(
                WeaponryTableCellResult(
                    specification=WeaponryColumnSpecification.from_mapping(cell_template),
                    analyse_data=analyse_data,
                    sources=_decode_sources(cell.get("analyseDataSource")),
                )
            )
        template_rows.append(template_row)
        decoded_rows.append(tuple(decoded_row))
    template["tableFieldList"] = template_rows
    return WeaponryFieldResult(
        specification=WeaponryFieldSpecification.from_mapping(template),
        table_rows=tuple(decoded_rows),
    )


def _decode_callback_payload(value: object) -> WeaponryCallbackPayload:
    root = _exact_dict(value, {"businessType", "data", "msg"}, name="callback")
    if root["businessType"] != "weaponry" or not isinstance(root["msg"], str):
        raise ValueError("Callback 根身份无效")
    data = root["data"]
    if not isinstance(data, dict):
        raise ValueError("Callback data 必须是对象")
    status = data.get("status")
    architecture_id = data.get("architectureId")
    if status == "3":
        _exact_dict(data, {"status", "architectureId"}, name="callback.data")
        fields = ()
    elif status == "2":
        _exact_dict(
            data,
            {"status", "architectureId", "weaponryTemplateFieldList"},
            name="callback.data",
        )
        raw_fields = data["weaponryTemplateFieldList"]
        if not isinstance(raw_fields, list):
            raise ValueError("weaponryTemplateFieldList 必须是数组")
        fields = tuple(_decode_field(item) for item in raw_fields)
    else:
        raise ValueError("Callback status 无效")
    return WeaponryCallbackPayload(
        architecture_id=architecture_id,
        status=status,
        message=root["msg"],
        fields=fields,
    )


__all__ = ["SQLiteWeaponryResultSnapshotStore"]
