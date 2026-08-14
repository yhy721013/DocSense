"""阶段 2 Task Control SQLite 的根 Schema 身份、DDL 与严格核验。

本模块只负责数据库结构，不负责业务事务。根 Manifest 是唯一结构真相源：初始化根据
Manifest 生成无 ``IF NOT EXISTS`` 的 DDL，普通打开则逐项核对 PRAGMA 可观察语义。
任何未知对象或身份漂移都失败关闭，严禁在启动路径中自动补表、补列或改索引。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import UUID


logger = logging.getLogger(__name__)

APPLICATION_ID = 1_146_307_378
USER_VERSION = 2
SCHEMA_NAME = "docsense.task-control"
SCHEMA_GENERATION = 2
ROOT_MANIFEST_VERSION = 1
MANIFEST_PROFILE = "canonical_json_v1"
METADATA_TABLE = "task_control_schema_metadata"
COMPONENT_REGISTRY_TABLE = "task_control_schema_components"

_MANIFEST_PATH = Path(__file__).with_name("root_schema_manifest.json")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class TaskControlSchemaError(RuntimeError):
    """表示数据库身份或实际结构不满足当前发布版本的严格契约。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class TaskControlDatabaseIdentity:
    """严格验证成功后可安全向组合根传递的数据库身份摘要。"""

    db_instance_uuid: str
    root_fingerprint: str
    schema_version: int
    metadata_created_at: str
    component_registry_digest: str
    registered_components: tuple[str, ...]


def _reject_float(_value: str) -> Any:
    """canonical_json_v1 禁止 float，读取 Manifest 时立即拒绝。"""

    raise TaskControlSchemaError("manifest_float_forbidden", "Manifest 禁止浮点数")


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝 JSON 对象重复键，避免解析器静默采用最后一个值掩盖契约漂移。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TaskControlSchemaError(
                "manifest_duplicate_key",
                f"Manifest JSON 对象包含重复键: {key}",
            )
        result[key] = value
    return result


def _load_manifest(path: Path = _MANIFEST_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_float=_reject_float,
            parse_constant=_reject_float,
            object_pairs_hook=_reject_duplicate_object_keys,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskControlSchemaError(
            "manifest_load_failed",
            f"无法加载 Task Control Schema Manifest: {type(exc).__name__}",
        ) from exc
    if not isinstance(payload, dict):
        raise TaskControlSchemaError("manifest_invalid", "根 Manifest 必须是 JSON 对象")
    _validate_manifest_definition(payload, expected_component="core")
    return payload


def _validate_canonical_value(value: Any, *, location: str = "$") -> None:
    """递归验证 canonical_json_v1 值域，避免 bool 被误当成整数。"""

    if value is None or isinstance(value, str) or type(value) is bool:
        return
    if type(value) is int:
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_canonical_value(item, location=f"{location}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TaskControlSchemaError(
                    "manifest_invalid_key",
                    f"Manifest 对象键必须是字符串: location={location}",
                )
            _validate_canonical_value(item, location=f"{location}.{key}")
        return
    raise TaskControlSchemaError(
        "manifest_value_forbidden",
        f"Manifest 含 canonical_json_v1 禁止的值: location={location}",
    )


def canonical_manifest_json(manifest: Mapping[str, Any]) -> str:
    """按冻结 profile 生成稳定 JSON；结果可直接参与 SHA-256。"""

    material = dict(manifest)
    _validate_canonical_value(material)
    return json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def root_manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    """计算根 Schema fingerprint，不读取 sqlite_schema.sql。"""

    canonical = canonical_manifest_json(manifest)
    material = f"{SCHEMA_NAME}\n{SCHEMA_GENERATION}\n{canonical}".encode("utf-8")
    return hashlib.sha256(material).hexdigest().upper()


def component_manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    """计算业务组件 fingerprint；组件身份与根 fingerprint 相互独立。"""

    component_name = str(manifest.get("componentName", ""))
    component_version = manifest.get("componentVersion")
    if type(component_version) is not int:
        raise TaskControlSchemaError("manifest_invalid", "组件版本必须是整数")
    canonical = canonical_manifest_json(manifest)
    material = f"{component_name}\n{component_version}\n{canonical}".encode("utf-8")
    return hashlib.sha256(material).hexdigest().upper()


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        raise TaskControlSchemaError(
            "manifest_invalid_identifier",
            f"Manifest 含非法 SQLite 标识符: {value!r}",
        )
    return f'"{value}"'


def _validate_manifest_definition(
    manifest: Mapping[str, Any],
    *,
    expected_component: str | None = None,
) -> None:
    """验证生成 DDL 前所需的 Manifest 自身约束。"""

    _validate_canonical_value(dict(manifest))
    if manifest.get("manifestProfile") != MANIFEST_PROFILE:
        raise TaskControlSchemaError("manifest_profile_mismatch", "未知 Manifest profile")
    component_name = manifest.get("componentName")
    if expected_component is not None and component_name != expected_component:
        raise TaskControlSchemaError("manifest_component_mismatch", "Manifest 组件身份不匹配")
    if not isinstance(component_name, str):
        raise TaskControlSchemaError("manifest_invalid", "Manifest 缺少组件名称")
    _quote_identifier(component_name)
    if manifest.get("rootSchemaGeneration") != SCHEMA_GENERATION:
        raise TaskControlSchemaError("manifest_generation_mismatch", "Manifest 根世代不匹配")
    defaults = manifest.get("indexTermDefaults")
    if defaults != {"collation": "BINARY", "order": "ASC"}:
        raise TaskControlSchemaError(
            "manifest_index_defaults_mismatch",
            "当前版本只接受冻结的 BINARY/ASC 索引项默认值",
        )

    table_names: set[str] = set()
    index_names: set[str] = set()
    for table in manifest.get("tables", []):
        name = str(table.get("name", ""))
        _quote_identifier(name)
        if name in table_names:
            raise TaskControlSchemaError("manifest_duplicate_object", f"重复表: {name}")
        table_names.add(name)
        column_names = [str(column.get("name", "")) for column in table.get("columns", [])]
        if not column_names or len(column_names) != len(set(column_names)):
            raise TaskControlSchemaError("manifest_invalid_columns", f"表列定义无效: {name}")
        for column in column_names:
            _quote_identifier(column)
        for check in table.get("checkConstraints", []):
            if ("expression" in check) == ("semanticExpression" in check):
                raise TaskControlSchemaError(
                    "manifest_invalid_check",
                    f"CHECK 必须且只能声明一种表达式: table={name}",
                )
        for foreign_key in table.get("foreignKeys", []):
            if bool(foreign_key.get("deferrable")):
                raise TaskControlSchemaError(
                    "manifest_unsupported_deferrable_fk",
                    "阶段 2 根/组件 Manifest 暂不允许可延迟外键",
                )

    for index in manifest.get("indexes", []):
        name = str(index.get("name", ""))
        _quote_identifier(name)
        if name in index_names or name in table_names:
            raise TaskControlSchemaError("manifest_duplicate_object", f"重复对象: {name}")
        index_names.add(name)
        if index.get("table") not in table_names:
            raise TaskControlSchemaError("manifest_invalid_index", f"索引目标表不存在: {name}")


def _utc_check(column: str, *, optional: bool) -> str:
    """把持久化 UTC 语义展开成稳定 SQLite CHECK 表达式。"""

    quoted = _quote_identifier(column)
    digit = "[0-9]"
    glob_pattern = (
        digit * 4
        + "-"
        + digit * 2
        + "-"
        + digit * 2
        + "T"
        + digit * 2
        + ":"
        + digit * 2
        + ":"
        + digit * 2
        + "."
        + digit * 6
        + "Z"
    )
    required = f"length({quoted}) = 27 AND {quoted} GLOB '{glob_pattern}'"
    if optional:
        return f"{quoted} IS NULL OR ({required})"
    return required


def _check_expression(check: Mapping[str, Any]) -> str:
    expression = check.get("expression")
    if isinstance(expression, str):
        return expression
    semantic = str(check.get("semanticExpression", ""))
    match = re.fullmatch(r"(optional_)?persisted_utc\(([A-Za-z_][A-Za-z0-9_]*)\)", semantic)
    if match is None:
        raise TaskControlSchemaError(
            "manifest_unknown_semantic_check",
            f"未知语义 CHECK: {semantic!r}",
        )
    return _utc_check(match.group(2), optional=bool(match.group(1)))


def _create_table_sql(table: Mapping[str, Any]) -> str:
    table_name = str(table["name"])
    clauses: list[str] = []
    primary_key_columns: list[tuple[int, str]] = []
    for column in table["columns"]:
        name = str(column["name"])
        parts = [_quote_identifier(name), str(column["type"])]
        if bool(column["notNull"]):
            parts.append("NOT NULL")
        if column.get("default") is not None:
            parts.extend(("DEFAULT", str(column["default"])))
        clauses.append(" ".join(parts))
        position = int(column.get("primaryKeyPosition", 0))
        if position:
            primary_key_columns.append((position, name))

    if primary_key_columns:
        ordered = [name for _, name in sorted(primary_key_columns)]
        columns_sql = ", ".join(_quote_identifier(name) for name in ordered)
        clauses.append(
            f'CONSTRAINT {_quote_identifier(f"pk_{table_name}")} PRIMARY KEY ({columns_sql})'
        )
    for unique in table.get("uniqueConstraints", []):
        columns_sql = ", ".join(_quote_identifier(str(name)) for name in unique["columns"])
        clauses.append(
            f'CONSTRAINT {_quote_identifier(str(unique["id"]))} UNIQUE ({columns_sql})'
        )
    for check in table.get("checkConstraints", []):
        clauses.append(
            f'CONSTRAINT {_quote_identifier(str(check["id"]))} '
            f'CHECK ({_check_expression(check)})'
        )
    for position, foreign_key in enumerate(table.get("foreignKeys", []), start=1):
        source = ", ".join(_quote_identifier(str(name)) for name in foreign_key["columns"])
        target = ", ".join(
            _quote_identifier(str(name)) for name in foreign_key["referencedColumns"]
        )
        clauses.append(
            f'CONSTRAINT {_quote_identifier(f"fk_{table_name}_{position}")} '
            f"FOREIGN KEY ({source}) REFERENCES "
            f'{_quote_identifier(str(foreign_key["referencedTable"]))} ({target}) '
            f'ON UPDATE {foreign_key["onUpdate"]} ON DELETE {foreign_key["onDelete"]} '
            "NOT DEFERRABLE"
        )
    body = ",\n    ".join(clauses)
    return f"CREATE TABLE {_quote_identifier(table_name)} (\n    {body}\n)"


def _create_index_sql(index: Mapping[str, Any]) -> str:
    unique = "UNIQUE " if bool(index["unique"]) else ""
    columns = ", ".join(_quote_identifier(str(name)) for name in index["columns"])
    sql = (
        f"CREATE {unique}INDEX {_quote_identifier(str(index['name']))} "
        f"ON {_quote_identifier(str(index['table']))} ({columns})"
    )
    where = index.get("where")
    if where is not None:
        sql += f" WHERE {where}"
    return sql


def root_schema_ddl() -> tuple[str, ...]:
    """返回确定顺序的根 DDL，调用方必须放入显式 Schema 事务。"""

    manifest = _load_manifest()
    tables = tuple(_create_table_sql(table) for table in manifest["tables"])
    indexes = tuple(_create_index_sql(index) for index in manifest["indexes"])
    return tables + indexes


def component_schema_ddl(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """根据冻结组件 Manifest 生成确定顺序的 DDL。

    本函数只生成 SQL，不打开连接、不执行 DDL。组件安装必须由
    :func:`install_component_schema` 在显式独占事务内完成，普通数据库打开严禁调用。
    """

    _validate_manifest_definition(manifest)
    if manifest.get("componentName") == "core":
        raise TaskControlSchemaError(
            "component_manifest_invalid",
            "业务组件 Manifest 不得声明为 core",
        )
    tables = tuple(_create_table_sql(table) for table in manifest.get("tables", []))
    indexes = tuple(_create_index_sql(index) for index in manifest.get("indexes", []))
    return tables + indexes


def install_component_schema(
    connection: sqlite3.Connection,
    manifest: Mapping[str, Any],
    *,
    installed_at: str,
    known_components: Mapping[str, Mapping[str, Any]],
) -> TaskControlDatabaseIdentity:
    """在独占事务中安装一个冻结业务组件，并在提交后执行完整结构复核。

    安装顺序固定为：验证现有根/组件并集、创建全部新对象、验证对象并集、写组件注册行、
    提交、再次完整验证。任何一步失败都会回滚，禁止 ``IF NOT EXISTS`` 或增量补列掩盖漂移。
    已安装的同版本组件按严格幂等验证返回；跳版本、降级及未知组件一律失败关闭。
    """

    if connection.in_transaction:
        raise TaskControlSchemaError(
            "component_install_nested_transaction",
            "禁止在已有事务内安装组件",
        )
    _validate_utc(installed_at)
    component_name = str(manifest.get("componentName", ""))
    _validate_manifest_definition(manifest, expected_component=component_name)
    component_version = manifest.get("componentVersion")
    if type(component_version) is not int or component_version < 1:
        raise TaskControlSchemaError(
            "component_version_invalid",
            "组件版本必须是正整数",
        )
    known = dict(known_components)
    if component_name not in known or dict(known[component_name]) != dict(manifest):
        raise TaskControlSchemaError(
            "component_not_declared",
            "待安装组件必须与当前发布版本的已知 Manifest 完全一致",
        )

    # 先在无写事务状态下完整验证当前结构。若目标组件已经安装，只有完整身份一致才按幂等成功。
    before = validate_task_control_schema(connection, known_components=known)
    if component_name in before.registered_components:
        return validate_task_control_schema(
            connection,
            known_components=known,
            required_components={component_name: component_version},
        )

    try:
        connection.execute("BEGIN EXCLUSIVE")
        # 独占锁取得后再次核对此前看到的数据库身份，阻止检查与安装之间的结构竞态。
        validate_task_control_connection_identity(connection, before)
        for statement in component_schema_ddl(manifest):
            connection.execute(statement)

        root_manifest = _load_manifest()
        installed_manifests = [root_manifest]
        for name in before.registered_components:
            installed_manifests.append(known[name])
        installed_manifests.append(manifest)
        # 契约要求先证明对象集合精确，再发布组件注册事实。
        _validate_manifest_objects(connection, installed_manifests)
        connection.execute(
            f"""
            INSERT INTO {_quote_identifier(COMPONENT_REGISTRY_TABLE)} (
                component_name, component_version, root_schema_generation,
                schema_fingerprint, manifest_profile, installed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                component_name,
                component_version,
                SCHEMA_GENERATION,
                component_manifest_fingerprint(manifest),
                MANIFEST_PROFILE,
                installed_at,
            ),
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise

    required = {
        name: int(known[name]["componentVersion"])
        for name in (*before.registered_components, component_name)
    }
    identity = validate_task_control_schema(
        connection,
        known_components=known,
        required_components=required,
    )
    logger.info(
        "Task Control 组件安装通过: component=%s version=%d fingerprint_prefix=%s",
        component_name,
        component_version,
        component_manifest_fingerprint(manifest)[:12],
    )
    return identity


def create_root_schema(
    connection: sqlite3.Connection,
    *,
    db_instance_uuid: str,
    created_at: str,
) -> None:
    """在全新空连接中原子创建根 Schema 和唯一 metadata 行。

    该函数不负责创建/发布文件，也不接受已经开启的事务，避免调用方误以为部分 DDL 可以
    自动修复旧数据库。失败时显式回滚，保留原始异常供 Bootstrap 分类。
    """

    if connection.in_transaction:
        raise TaskControlSchemaError("schema_transaction_nested", "禁止嵌套 Schema 事务")
    _validate_uuid(db_instance_uuid)
    _validate_utc(created_at)
    manifest = _load_manifest()
    fingerprint = root_manifest_fingerprint(manifest)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {USER_VERSION}")
        for statement in root_schema_ddl():
            connection.execute(statement)
        connection.execute(
            f"""
            INSERT INTO {_quote_identifier(METADATA_TABLE)} (
                metadata_id, schema_name, schema_generation,
                compatible_generation_min, compatible_generation_max,
                root_manifest_version, schema_fingerprint,
                db_instance_uuid, created_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                SCHEMA_NAME,
                SCHEMA_GENERATION,
                SCHEMA_GENERATION,
                SCHEMA_GENERATION,
                ROOT_MANIFEST_VERSION,
                fingerprint,
                db_instance_uuid,
                created_at,
            ),
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _normalize_sql_fragment(value: str) -> str:
    """仅折叠引号外空白；不做可能掩盖语义差异的 SQL 重写。"""

    result: list[str] = []
    pending_space = False
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote is not None:
            result.append(char)
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    result.append(value[index + 1])
                    index += 1
                else:
                    quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            if pending_space and result and result[-1] not in {"(", " "}:
                result.append(" ")
            pending_space = False
            quote = char
            result.append(char)
        elif char.isspace():
            pending_space = True
        else:
            if pending_space and result and result[-1] not in {"(", " "} and char != ")":
                result.append(" ")
            pending_space = False
            result.append(char)
        index += 1
    return "".join(result).strip()


def _extract_check_expressions(create_sql: str) -> tuple[str, ...]:
    """从受控建表文本提取 CHECK 内容，正确处理括号和 SQL 引号。"""

    expressions: list[str] = []
    upper = create_sql.upper()
    cursor = 0
    while cursor < len(create_sql):
        match = re.search(r"\bCHECK\s*\(", upper[cursor:])
        if match is None:
            break
        opening = cursor + match.end() - 1
        depth = 1
        quote: str | None = None
        position = opening + 1
        while position < len(create_sql) and depth:
            char = create_sql[position]
            if quote is not None:
                if char == quote:
                    if position + 1 < len(create_sql) and create_sql[position + 1] == quote:
                        position += 1
                    else:
                        quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            position += 1
        if depth:
            raise TaskControlSchemaError("schema_sql_invalid", "建表 SQL 的 CHECK 括号不完整")
        expressions.append(_normalize_sql_fragment(create_sql[opening + 1 : position - 1]))
        cursor = position
    return tuple(expressions)


def _extract_index_where(create_sql: str) -> str | None:
    """提取 CREATE INDEX 顶层 WHERE，索引列括号内的文本不会被误识别。"""

    quote: str | None = None
    depth = 0
    position = 0
    while position < len(create_sql):
        char = create_sql[position]
        if quote is not None:
            if char == quote:
                if position + 1 < len(create_sql) and create_sql[position + 1] == quote:
                    position += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and create_sql[position : position + 5].upper() == "WHERE":
            before = create_sql[position - 1] if position else " "
            after = create_sql[position + 5] if position + 5 < len(create_sql) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return _normalize_sql_fragment(create_sql[position + 5 :])
        position += 1
    return None


def _pragma_rows(connection: sqlite3.Connection, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in connection.execute(sql).fetchall()]


def _validate_table(connection: sqlite3.Connection, table: Mapping[str, Any], sql: str) -> None:
    table_name = str(table["name"])
    quoted_table = _quote_identifier(table_name)
    actual_columns = _pragma_rows(connection, f"PRAGMA table_xinfo({quoted_table})")
    expected_columns = [
        (
            position,
            str(column["name"]),
            str(column["type"]),
            int(bool(column["notNull"])),
            column.get("default"),
            int(column.get("primaryKeyPosition", 0)),
            int(column.get("hidden", 0)),
        )
        for position, column in enumerate(table["columns"])
    ]
    if actual_columns != expected_columns:
        raise TaskControlSchemaError("schema_column_drift", f"表列结构漂移: table={table_name}")

    actual_checks = _extract_check_expressions(sql)
    expected_checks = tuple(
        _normalize_sql_fragment(_check_expression(check))
        for check in table.get("checkConstraints", [])
    )
    if actual_checks != expected_checks:
        raise TaskControlSchemaError("schema_check_drift", f"CHECK 约束漂移: table={table_name}")

    expected_fks = {
        (
            tuple(str(value) for value in foreign_key["columns"]),
            str(foreign_key["referencedTable"]),
            tuple(str(value) for value in foreign_key["referencedColumns"]),
            str(foreign_key["onUpdate"]),
            str(foreign_key["onDelete"]),
        )
        for foreign_key in table.get("foreignKeys", [])
    }
    grouped: dict[int, list[tuple[Any, ...]]] = {}
    for row in _pragma_rows(connection, f"PRAGMA foreign_key_list({quoted_table})"):
        grouped.setdefault(int(row[0]), []).append(row)
    actual_fks = {
        (
            tuple(str(row[3]) for row in sorted(rows, key=lambda item: int(item[1]))),
            str(rows[0][2]),
            tuple(str(row[4]) for row in sorted(rows, key=lambda item: int(item[1]))),
            str(rows[0][5]),
            str(rows[0][6]),
        )
        for rows in grouped.values()
    }
    if actual_fks != expected_fks:
        raise TaskControlSchemaError("schema_foreign_key_drift", f"外键漂移: table={table_name}")
    normalized_table_sql = _normalize_sql_fragment(sql).upper()
    expected_not_deferrable = len(table.get("foreignKeys", []))
    if normalized_table_sql.count("NOT DEFERRABLE") != expected_not_deferrable:
        raise TaskControlSchemaError(
            "schema_foreign_key_deferrability_drift",
            f"外键延迟策略漂移: table={table_name}",
        )

    expected_unique = {
        tuple(str(value) for value in unique["columns"])
        for unique in table.get("uniqueConstraints", [])
    }
    actual_unique: set[tuple[str, ...]] = set()
    for row in _pragma_rows(connection, f"PRAGMA index_list({quoted_table})"):
        index_name = str(row[1])
        origin = str(row[3])
        if origin != "u":
            continue
        quoted_index = _quote_identifier(index_name)
        terms = [
            str(term[2])
            for term in _pragma_rows(connection, f"PRAGMA index_xinfo({quoted_index})")
            if int(term[5]) == 1
        ]
        actual_unique.add(tuple(terms))
    if actual_unique != expected_unique:
        raise TaskControlSchemaError("schema_unique_drift", f"UNIQUE 约束漂移: table={table_name}")


def _validate_index(
    connection: sqlite3.Connection,
    index: Mapping[str, Any],
    sql: str,
) -> None:
    table_name = str(index["table"])
    index_name = str(index["name"])
    listed = {
        str(row[1]): row
        for row in _pragma_rows(connection, f"PRAGMA index_list({_quote_identifier(table_name)})")
    }
    row = listed.get(index_name)
    if row is None or str(row[3]) != "c":
        raise TaskControlSchemaError("schema_index_missing", f"显式索引缺失: index={index_name}")
    if bool(row[2]) != bool(index["unique"]):
        raise TaskControlSchemaError("schema_index_unique_drift", f"索引唯一性漂移: index={index_name}")
    expected_partial = index.get("where") is not None
    if bool(row[4]) != expected_partial:
        raise TaskControlSchemaError("schema_index_partial_drift", f"索引 partial 标记漂移: index={index_name}")

    terms = [
        term
        for term in _pragma_rows(connection, f"PRAGMA index_xinfo({_quote_identifier(index_name)})")
        if int(term[5]) == 1
    ]
    actual_terms = tuple((str(term[2]), str(term[4]), int(term[3])) for term in terms)
    expected_terms = tuple((str(name), "BINARY", 0) for name in index["columns"])
    if actual_terms != expected_terms:
        raise TaskControlSchemaError("schema_index_term_drift", f"索引项漂移: index={index_name}")

    actual_where = _extract_index_where(sql)
    expected_where = index.get("where")
    if actual_where != (
        _normalize_sql_fragment(str(expected_where)) if expected_where is not None else None
    ):
        raise TaskControlSchemaError("schema_index_predicate_drift", f"索引谓词漂移: index={index_name}")


def _validate_manifest_objects(
    connection: sqlite3.Connection,
    manifests: Sequence[Mapping[str, Any]],
) -> None:
    expected_tables = {
        str(table["name"]): table
        for manifest in manifests
        for table in manifest["tables"]
    }
    expected_indexes = {
        str(index["name"]): index
        for manifest in manifests
        for index in manifest["indexes"]
    }
    if set(expected_tables) & set(expected_indexes):
        raise TaskControlSchemaError("manifest_object_overlap", "Manifest 对象所有权重叠")

    table_list_rows = _pragma_rows(connection, "PRAGMA table_list")
    actual_table_list = {
        str(row[1]): (str(row[2]), int(row[3]), int(row[4]), int(row[5]))
        for row in table_list_rows
        if str(row[0]) == "main" and not str(row[1]).startswith("sqlite_")
    }
    expected_table_list = {
        name: ("table", len(table["columns"]), 0, 0)
        for name, table in expected_tables.items()
    }
    if actual_table_list != expected_table_list:
        raise TaskControlSchemaError(
            "schema_table_list_drift",
            "PRAGMA table_list 与 Manifest 不一致",
        )

    rows = _pragma_rows(
        connection,
        "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY name",
    )
    actual_explicit: dict[str, tuple[str, str, str]] = {}
    forbidden_internal: list[str] = []
    for object_type, name_raw, table_raw, sql_raw in rows:
        name = str(name_raw)
        if name.startswith("sqlite_autoindex_") and sql_raw is None:
            continue
        if name.startswith("sqlite_"):
            forbidden_internal.append(name)
            continue
        if sql_raw is None:
            raise TaskControlSchemaError("schema_object_without_sql", f"对象缺少 SQL: object={name}")
        actual_explicit[name] = (str(object_type), str(table_raw), str(sql_raw))
    if forbidden_internal:
        raise TaskControlSchemaError(
            "schema_forbidden_internal_object",
            f"存在未登记 SQLite 内部对象: count={len(forbidden_internal)}",
        )

    expected_names = set(expected_tables) | set(expected_indexes)
    if set(actual_explicit) != expected_names:
        missing = len(expected_names - set(actual_explicit))
        unexpected = len(set(actual_explicit) - expected_names)
        raise TaskControlSchemaError(
            "schema_object_union_drift",
            f"实际对象集合与 Manifest 并集不符: missing={missing} unexpected={unexpected}",
        )
    for name, table in expected_tables.items():
        object_type, table_name, create_sql = actual_explicit[name]
        if object_type != "table" or table_name != name:
            raise TaskControlSchemaError("schema_object_type_drift", f"表对象类型漂移: object={name}")
        _validate_table(connection, table, create_sql)
    for name, index in expected_indexes.items():
        object_type, table_name, create_sql = actual_explicit[name]
        if object_type != "index" or table_name != index["table"]:
            raise TaskControlSchemaError("schema_object_type_drift", f"索引对象类型漂移: object={name}")
        _validate_index(connection, index, create_sql)


def _validate_uuid(value: str) -> None:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise TaskControlSchemaError("database_uuid_invalid", "数据库实例 UUID 无效") from exc
    if str(parsed) != value:
        raise TaskControlSchemaError("database_uuid_invalid", "数据库实例 UUID 必须是规范小写文本")


def _validate_utc(value: str) -> None:
    try:
        parsed = datetime.strptime(value, _UTC_FORMAT)
    except (TypeError, ValueError) as exc:
        raise TaskControlSchemaError("database_time_invalid", "数据库 UTC 时间格式无效") from exc
    if parsed.strftime(_UTC_FORMAT) != value:
        raise TaskControlSchemaError("database_time_invalid", "数据库 UTC 时间必须保留六位微秒")


def _component_registry_digest(rows: Sequence[Sequence[Any]]) -> str:
    """固定全部组件注册事实，供后续短连接做轻量身份复核。"""

    canonical_rows = [[value for value in row] for row in rows]
    material = json.dumps(
        canonical_rows,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest().upper()


def validate_task_control_connection_identity(
    connection: sqlite3.Connection,
    expected: TaskControlDatabaseIdentity,
) -> None:
    """复核短连接仍指向 Bootstrap 验证过的同一 Schema 身份。

    完整对象/完整性核验只在 Bootstrap 执行；每个业务短连接核对不可变根 metadata、组件注册
    摘要及 SQLite ``schema_version``。这样既能拒绝运行期间 DDL 漂移，又避免在每个高频 UoW
    内重复执行全库 ``integrity_check``。本门禁不是对恶意数据库管理员的安全边界。
    """

    if not isinstance(expected, TaskControlDatabaseIdentity):
        raise TypeError("expected 必须是 TaskControlDatabaseIdentity")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise TaskControlSchemaError("foreign_keys_disabled", "SQLite foreign_keys 必须启用")
    if int(connection.execute("PRAGMA application_id").fetchone()[0]) != APPLICATION_ID:
        raise TaskControlSchemaError("database_application_id_mismatch", "数据库 application_id 不匹配")
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != USER_VERSION:
        raise TaskControlSchemaError("database_user_version_mismatch", "数据库 user_version 不匹配")
    if int(connection.execute("PRAGMA schema_version").fetchone()[0]) != expected.schema_version:
        raise TaskControlSchemaError("database_schema_version_drift", "数据库 schema_version 已漂移")
    try:
        metadata_rows = connection.execute(
            f"SELECT metadata_id, schema_name, schema_generation, "
            f"compatible_generation_min, compatible_generation_max, "
            f"root_manifest_version, schema_fingerprint, db_instance_uuid, created_at "
            f"FROM {_quote_identifier(METADATA_TABLE)} ORDER BY metadata_id"
        ).fetchall()
        registry_rows = connection.execute(
            f"SELECT component_name, component_version, root_schema_generation, "
            f"schema_fingerprint, manifest_profile, installed_at "
            f"FROM {_quote_identifier(COMPONENT_REGISTRY_TABLE)} ORDER BY component_name"
        ).fetchall()
    except sqlite3.Error as exc:
        raise TaskControlSchemaError(
            "database_identity_unreadable",
            "短连接无法读取数据库身份",
        ) from exc
    if len(metadata_rows) != 1:
        raise TaskControlSchemaError("database_metadata_cardinality", "metadata 必须恰有一行")
    metadata = tuple(metadata_rows[0])
    expected_metadata = (
        1,
        SCHEMA_NAME,
        SCHEMA_GENERATION,
        SCHEMA_GENERATION,
        SCHEMA_GENERATION,
        ROOT_MANIFEST_VERSION,
        expected.root_fingerprint,
        expected.db_instance_uuid,
        expected.metadata_created_at,
    )
    if metadata != expected_metadata:
        raise TaskControlSchemaError(
            "database_connection_identity_drift",
            "短连接观察到的根 metadata 已漂移",
        )
    registry_names = tuple(str(row[0]) for row in registry_rows)
    if registry_names != expected.registered_components:
        raise TaskControlSchemaError(
            "database_component_registry_drift",
            "短连接观察到的组件集合已漂移",
        )
    if _component_registry_digest(registry_rows) != expected.component_registry_digest:
        raise TaskControlSchemaError(
            "database_component_registry_drift",
            "短连接观察到的组件注册身份已漂移",
        )


def validate_task_control_schema(
    connection: sqlite3.Connection,
    *,
    known_components: Mapping[str, Mapping[str, Any]] | None = None,
    required_components: Mapping[str, int] | None = None,
) -> TaskControlDatabaseIdentity:
    """只读严格验证数据库根身份、组件注册和实际对象并集。

    调用方必须先启用 ``foreign_keys``；本函数不执行 DDL/DML，也不会修复损坏。阶段 2-2
    默认不认识且不要求任何业务组件，因此注册表非空会使旧二进制失败关闭。
    """

    known = dict(known_components or {})
    required = dict(required_components or {})
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise TaskControlSchemaError("foreign_keys_disabled", "SQLite foreign_keys 必须启用")
    if int(connection.execute("PRAGMA application_id").fetchone()[0]) != APPLICATION_ID:
        raise TaskControlSchemaError("database_application_id_mismatch", "数据库 application_id 不匹配")
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != USER_VERSION:
        raise TaskControlSchemaError("database_user_version_mismatch", "数据库 user_version 不匹配")

    root_manifest = _load_manifest()
    expected_root_fingerprint = root_manifest_fingerprint(root_manifest)
    # 先确认身份表存在，再读取组件注册。这里只做最小存在性门禁，完整列/约束检查仍由
    # Manifest 核验完成；这样损坏库会得到稳定错误分类，而不是泄漏底层 OperationalError。
    existing_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall()
    }
    missing_identity_tables = {
        METADATA_TABLE,
        COMPONENT_REGISTRY_TABLE,
    } - existing_tables
    if missing_identity_tables:
        raise TaskControlSchemaError(
            "database_identity_table_missing",
            f"数据库缺少身份表: count={len(missing_identity_tables)}",
        )
    try:
        registered_rows = connection.execute(
            f"SELECT component_name, component_version, root_schema_generation, "
            f"schema_fingerprint, manifest_profile, installed_at "
            f"FROM {_quote_identifier(COMPONENT_REGISTRY_TABLE)} ORDER BY component_name"
        ).fetchall()
    except sqlite3.Error as exc:
        raise TaskControlSchemaError(
            "database_component_registry_unreadable",
            "组件注册表结构不可读",
        ) from exc
    registered_names = {str(row[0]) for row in registered_rows}
    unknown = registered_names - set(known)
    if unknown:
        raise TaskControlSchemaError(
            "database_unknown_component",
            f"数据库含当前版本未知组件: count={len(unknown)}",
        )
    missing_required = set(required) - registered_names
    if missing_required:
        raise TaskControlSchemaError(
            "database_required_component_missing",
            f"数据库缺少必需组件: count={len(missing_required)}",
        )

    manifests: list[Mapping[str, Any]] = [root_manifest]
    for row in registered_rows:
        name = str(row[0])
        manifest = known[name]
        _validate_manifest_definition(manifest, expected_component=name)
        expected_version = manifest.get("componentVersion")
        if int(row[1]) != expected_version or required.get(name, expected_version) != expected_version:
            raise TaskControlSchemaError("database_component_version_mismatch", f"组件版本不匹配: component={name}")
        if int(row[2]) != SCHEMA_GENERATION:
            raise TaskControlSchemaError("database_component_generation_mismatch", f"组件根世代不匹配: component={name}")
        if str(row[3]) != component_manifest_fingerprint(manifest):
            raise TaskControlSchemaError("database_component_fingerprint_mismatch", f"组件 fingerprint 不匹配: component={name}")
        if str(row[4]) != MANIFEST_PROFILE:
            raise TaskControlSchemaError("database_component_profile_mismatch", f"组件 profile 不匹配: component={name}")
        _validate_utc(str(row[5]))
        manifests.append(manifest)

    _validate_manifest_objects(connection, manifests)
    try:
        metadata_rows = connection.execute(
            f"SELECT metadata_id, schema_name, schema_generation, "
            f"compatible_generation_min, compatible_generation_max, "
            f"root_manifest_version, schema_fingerprint, db_instance_uuid, created_at "
            f"FROM {_quote_identifier(METADATA_TABLE)} ORDER BY metadata_id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise TaskControlSchemaError(
            "database_metadata_unreadable",
            "数据库 metadata 结构不可读",
        ) from exc
    if len(metadata_rows) != 1:
        raise TaskControlSchemaError("database_metadata_cardinality", "metadata 必须恰有一行")
    row = metadata_rows[0]
    expected_prefix = (
        1,
        SCHEMA_NAME,
        SCHEMA_GENERATION,
        SCHEMA_GENERATION,
        SCHEMA_GENERATION,
        ROOT_MANIFEST_VERSION,
        expected_root_fingerprint,
    )
    if tuple(row[:7]) != expected_prefix:
        raise TaskControlSchemaError("database_root_identity_mismatch", "数据库根身份或 fingerprint 不匹配")
    db_instance_uuid = str(row[7])
    _validate_uuid(db_instance_uuid)
    _validate_utc(str(row[8]))

    integrity_rows = _pragma_rows(connection, "PRAGMA integrity_check")
    if integrity_rows != [("ok",)]:
        raise TaskControlSchemaError("database_integrity_failed", "SQLite integrity_check 未通过")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise TaskControlSchemaError("database_foreign_key_failed", "SQLite foreign_key_check 未通过")
    return TaskControlDatabaseIdentity(
        db_instance_uuid=db_instance_uuid,
        root_fingerprint=expected_root_fingerprint,
        schema_version=int(connection.execute("PRAGMA schema_version").fetchone()[0]),
        metadata_created_at=str(row[8]),
        component_registry_digest=_component_registry_digest(registered_rows),
        registered_components=tuple(sorted(registered_names)),
    )


ROOT_MANIFEST_FINGERPRINT = root_manifest_fingerprint(_load_manifest())


__all__ = [
    "APPLICATION_ID",
    "COMPONENT_REGISTRY_TABLE",
    "MANIFEST_PROFILE",
    "METADATA_TABLE",
    "ROOT_MANIFEST_FINGERPRINT",
    "ROOT_MANIFEST_VERSION",
    "SCHEMA_GENERATION",
    "SCHEMA_NAME",
    "TaskControlDatabaseIdentity",
    "TaskControlSchemaError",
    "USER_VERSION",
    "canonical_manifest_json",
    "component_manifest_fingerprint",
    "component_schema_ddl",
    "create_root_schema",
    "install_component_schema",
    "root_manifest_fingerprint",
    "root_schema_ddl",
    "validate_task_control_schema",
    "validate_task_control_connection_identity",
]
