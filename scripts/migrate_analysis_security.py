#!/usr/bin/env python3
"""把 `/llm/analysis` 存量协议键从 secrets 幂等迁移到 security。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]

MIGRATION_NAME = "analysis-security"
OLD_KEY = "secrets"
NEW_KEY = "security"


class MigrationError(RuntimeError):
    """迁移预检或执行失败。"""


class MigrationConflictError(MigrationError):
    """同一对象中的新旧键存在不同值。"""


@dataclass(frozen=True)
class SQLiteChange:
    database_path: Path
    table: str
    column: str
    rowid: int
    original_value: str
    migrated_value: str
    renamed_keys: int
    label: str


@dataclass(frozen=True)
class CallbackChange:
    path: Path
    original_sha256: str
    migrated_bytes: bytes
    renamed_keys: int
    mode: int
    atime_ns: int
    mtime_ns: int
    label: str


@dataclass(frozen=True)
class MigrationPlan:
    task_db_path: Path
    knowledge_db_path: Path
    runtime_dir: Path
    database_digests: tuple[tuple[Path, str], ...]
    sqlite_changes: tuple[SQLiteChange, ...]
    callback_changes: tuple[CallbackChange, ...]

    @property
    def changed_targets(self) -> int:
        return len(self.sqlite_changes) + len(self.callback_changes)

    @property
    def renamed_keys(self) -> int:
        return sum(item.renamed_keys for item in self.sqlite_changes) + sum(
            item.renamed_keys for item in self.callback_changes
        )

    def summary(self) -> dict[str, Any]:
        targets: dict[str, dict[str, int]] = {}
        for item in (*self.sqlite_changes, *self.callback_changes):
            current = targets.setdefault(item.label, {"changedTargets": 0, "renamedKeys": 0})
            current["changedTargets"] += 1
            current["renamedKeys"] += item.renamed_keys
        return {
            "mode": "dry-run",
            "migration": MIGRATION_NAME,
            "taskDatabase": str(self.task_db_path),
            "knowledgeDatabase": str(self.knowledge_db_path),
            "runtimeDirectory": str(self.runtime_dir),
            "changedTargets": self.changed_targets,
            "renamedKeys": self.renamed_keys,
            "targets": targets,
        }

    def database_digest(self, path: Path) -> str:
        for database_path, digest in self.database_digests:
            if database_path == path:
                return digest
        raise MigrationError(f"迁移计划缺少数据库摘要: {path}")


JsonTransformer = Callable[[dict[str, Any], str], int]


def _type_aware_equal(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _rename_mapping_key(mapping: dict[str, Any], context: str) -> int:
    if OLD_KEY not in mapping:
        return 0
    if NEW_KEY in mapping:
        if not _type_aware_equal(mapping[OLD_KEY], mapping[NEW_KEY]):
            raise MigrationConflictError(
                f"{context} 同时包含 {OLD_KEY}/{NEW_KEY} 且值不同"
            )
        del mapping[OLD_KEY]
        return 1
    mapping[NEW_KEY] = mapping.pop(OLD_KEY)
    return 1


def _transform_task_request(payload: dict[str, Any], context: str) -> int:
    params = payload.get("params")
    if isinstance(params, dict):
        return _rename_mapping_key(params, f"{context}.params")
    if not isinstance(params, list):
        return 0
    renamed = 0
    for index, item in enumerate(params):
        if isinstance(item, dict):
            renamed += _rename_mapping_key(item, f"{context}.params[{index}]")
    return renamed


def _transform_task_result(payload: dict[str, Any], context: str) -> int:
    data = payload.get("data")
    if not isinstance(data, dict):
        return 0
    return _rename_mapping_key(data, f"{context}.data")


def _transform_operation_metadata(payload: dict[str, Any], context: str) -> int:
    attributes = payload.get("attributes")
    if not isinstance(attributes, dict):
        return 0
    return _rename_mapping_key(attributes, f"{context}.attributes")


def _transform_document_metadata(payload: dict[str, Any], context: str) -> int:
    return _rename_mapping_key(payload, context)


def _transform_callback(payload: dict[str, Any], context: str) -> int:
    if payload.get("businessType") != "file":
        return 0
    return _transform_task_result(payload, context)


def _parse_json_object(raw_value: Any, context: str) -> dict[str, Any]:
    if not isinstance(raw_value, str):
        raise MigrationError(f"{context} 不是 JSON 文本")

    def reject_non_standard_constant(value: str) -> None:
        raise ValueError(f"不允许非标准 JSON 常量 {value}")

    try:
        parsed = json.loads(raw_value, parse_constant=reject_non_standard_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise MigrationError(f"{context} 不是合法 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MigrationError(f"{context} 的 JSON 根节点必须是对象")
    return parsed


def _serialize_json(payload: dict[str, Any], *, sort_keys: bool) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=sort_keys,
        separators=(",", ":"),
        allow_nan=False,
    )


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _require_database(path: Path) -> None:
    if not path.exists():
        raise MigrationError(f"SQLite 文件不存在: {path}")
    if not path.is_file():
        raise MigrationError(f"SQLite 路径不是文件: {path}")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _require_column(connection: sqlite3.Connection, table: str, column: str) -> None:
    columns = {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({_quote_identifier(table)})"
        ).fetchall()
    }
    if column not in columns:
        raise MigrationError(f"{table}.{column} 不存在")


def _check_connection_integrity(connection: sqlite3.Connection, label: Path) -> None:
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]
    if integrity != ["ok"]:
        raise MigrationError(f"SQLite integrity_check 失败: {label}: {integrity}")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise MigrationError(f"SQLite foreign_key_check 失败: {label}: {foreign_keys}")


def _check_database_integrity(path: Path) -> None:
    with _readonly_connection(path) as connection:
        _check_connection_integrity(connection, path)


def _database_logical_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for statement in connection.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    for pragma_name in ("application_id", "user_version"):
        value = connection.execute(f"PRAGMA {pragma_name}").fetchone()[0]
        digest.update(f"{pragma_name}={value}\n".encode("ascii"))
    return digest.hexdigest()


def _database_file_logical_digest(path: Path) -> str:
    with _readonly_connection(path) as connection:
        return _database_logical_digest(connection)


def _scan_sqlite_target(
    *,
    database_path: Path,
    table: str,
    column: str,
    label: str,
    transformer: JsonTransformer,
    sort_keys: bool,
    where_sql: str = "",
    connection: sqlite3.Connection | None = None,
    allow_null: bool = False,
) -> list[SQLiteChange]:
    changes: list[SQLiteChange] = []
    owns_connection = connection is None
    active_connection = connection or _readonly_connection(database_path)
    try:
        if not _table_exists(active_connection, table):
            raise MigrationError(f"{database_path} 缺少必需表 {table}")
        _require_column(active_connection, table, column)
        query = (
            f"SELECT rowid AS migration_rowid, {_quote_identifier(column)} AS json_value "
            f"FROM {_quote_identifier(table)}"
        )
        if where_sql:
            query += f" WHERE {where_sql}"
        for row in active_connection.execute(query).fetchall():
            raw_value = row["json_value"]
            if raw_value is None:
                if allow_null:
                    continue
                raise MigrationError(
                    f"{database_path}:{table}.rowid={row['migration_rowid']}.{column} "
                    "不能为 NULL"
                )
            context = f"{database_path}:{table}.rowid={row['migration_rowid']}.{column}"
            parsed = _parse_json_object(raw_value, context)
            migrated = copy.deepcopy(parsed)
            renamed = transformer(migrated, context)
            if not renamed:
                continue
            changes.append(
                SQLiteChange(
                    database_path=database_path,
                    table=table,
                    column=column,
                    rowid=int(row["migration_rowid"]),
                    original_value=raw_value,
                    migrated_value=_serialize_json(migrated, sort_keys=sort_keys),
                    renamed_keys=renamed,
                    label=label,
                )
            )
    finally:
        if owns_connection:
            active_connection.close()
    return changes


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_callback_file(path: Path, label: str) -> CallbackChange | None:
    try:
        original_bytes = path.read_bytes()
        raw_text = original_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MigrationError(f"回调文件读取失败: {path}: {exc}") from exc
    parsed = _parse_json_object(raw_text, str(path))
    migrated = copy.deepcopy(parsed)
    renamed = _transform_callback(migrated, str(path))
    if not renamed:
        return None
    current_stat = path.stat()
    migrated_bytes = (
        json.dumps(migrated, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    return CallbackChange(
        path=path,
        original_sha256=_sha256_bytes(original_bytes),
        migrated_bytes=migrated_bytes,
        renamed_keys=renamed,
        mode=stat.S_IMODE(current_stat.st_mode),
        atime_ns=current_stat.st_atime_ns,
        mtime_ns=current_stat.st_mtime_ns,
        label=label,
    )


def build_migration_plan(
    *,
    task_db_path: Path,
    knowledge_db_path: Path,
    runtime_dir: Path,
) -> MigrationPlan:
    task_db_path = task_db_path.expanduser().resolve()
    knowledge_db_path = knowledge_db_path.expanduser().resolve()
    runtime_dir = runtime_dir.expanduser().resolve()
    _require_database(task_db_path)
    _require_database(knowledge_db_path)
    if task_db_path == knowledge_db_path:
        raise MigrationError("任务库与知识库不能指向同一 SQLite 文件")

    sqlite_changes: list[SQLiteChange] = []
    database_digests: list[tuple[Path, str]] = []
    with (
        _readonly_connection(task_db_path) as task_connection,
        _readonly_connection(knowledge_db_path) as knowledge_connection,
    ):
        task_connection.execute("BEGIN")
        knowledge_connection.execute("BEGIN")
        _check_connection_integrity(task_connection, task_db_path)
        _check_connection_integrity(knowledge_connection, knowledge_db_path)
        sqlite_changes.extend(
            _scan_sqlite_target(
                database_path=task_db_path,
                table="llm_tasks",
                column="request_payload",
                label="llm_tasks.request_payload",
                transformer=_transform_task_request,
                sort_keys=False,
                where_sql="business_type = 'file'",
                connection=task_connection,
            )
        )
        sqlite_changes.extend(
            _scan_sqlite_target(
                database_path=task_db_path,
                table="llm_tasks",
                column="result_payload",
                label="llm_tasks.result_payload",
                transformer=_transform_task_result,
                sort_keys=False,
                where_sql="business_type = 'file'",
                connection=task_connection,
                allow_null=True,
            )
        )
        sqlite_changes.extend(
            _scan_sqlite_target(
                database_path=task_db_path,
                table="knowledge_index_operations",
                column="metadata_json",
                label="knowledge_index_operations.metadata_json",
                transformer=_transform_operation_metadata,
                sort_keys=True,
                where_sql="business_type = 'file'",
                connection=task_connection,
            )
        )
        sqlite_changes.extend(
            _scan_sqlite_target(
                database_path=knowledge_db_path,
                table="documents",
                column="metadata_json",
                label="documents.metadata_json",
                transformer=_transform_document_metadata,
                sort_keys=True,
                connection=knowledge_connection,
            )
        )
        database_digests.extend(
            (
                (task_db_path, _database_logical_digest(task_connection)),
                (knowledge_db_path, _database_logical_digest(knowledge_connection)),
            )
        )

    callback_changes: list[CallbackChange] = []
    callback_dir = runtime_dir / "callback"
    if callback_dir.exists():
        if not callback_dir.is_dir():
            raise MigrationError(f"回调历史路径不是目录: {callback_dir}")
        for callback_path in sorted(callback_dir.glob("*.json")):
            if callback_path.is_file():
                change = _scan_callback_file(callback_path, "callback_history")
                if change is not None:
                    callback_changes.append(change)
    legacy_callback = runtime_dir / "call_back.json"
    if legacy_callback.exists():
        if not legacy_callback.is_file():
            raise MigrationError(f"旧版回调预览不是文件: {legacy_callback}")
        change = _scan_callback_file(legacy_callback, "legacy_callback_preview")
        if change is not None:
            callback_changes.append(change)

    return MigrationPlan(
        task_db_path=task_db_path,
        knowledge_db_path=knowledge_db_path,
        runtime_dir=runtime_dir,
        database_digests=tuple(database_digests),
        sqlite_changes=tuple(sqlite_changes),
        callback_changes=tuple(callback_changes),
    )


def _atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    mode: int,
    atime_ns: int,
    mtime_ns: int,
) -> None:
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(path, temporary_path)
        with temporary_path.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        os.utime(path, ns=(atime_ns, mtime_ns))
    finally:
        temporary_path.unlink(missing_ok=True)


def _backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection: sqlite3.Connection | None = None
    backup_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(str(source))
        backup_connection = sqlite3.connect(str(destination))
        source_connection.backup(backup_connection)
        backup_connection.close()
        backup_connection = None
        source_connection.close()
        source_connection = None
        _check_database_integrity(destination)
    except Exception:
        if backup_connection is not None:
            backup_connection.close()
        if source_connection is not None:
            source_connection.close()
        destination.unlink(missing_ok=True)
        raise


def _restore_database(backup_path: Path, destination: Path) -> None:
    backup_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        backup_connection = sqlite3.connect(str(backup_path))
        destination_connection = sqlite3.connect(str(destination))
        backup_connection.backup(destination_connection)
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if backup_connection is not None:
            backup_connection.close()
    _check_database_integrity(destination)


def _restore_callback_file(backup_path: Path, destination: Path) -> None:
    temporary_path = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.restore"
    )
    try:
        shutil.copy2(backup_path, temporary_path)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _build_manifest(
    plan: MigrationPlan,
    *,
    backup_dir: Path,
    database_backups: dict[Path, Path],
    callback_backups: dict[Path, Path],
    status: str,
) -> dict[str, Any]:
    summary = plan.summary()
    summary["mode"] = "apply"
    return {
        "migration": MIGRATION_NAME,
        "status": status,
        "createdAt": datetime.now().astimezone().isoformat(),
        "backupDirectory": str(backup_dir),
        "summary": summary,
        "databases": [
            {
                "source": str(source),
                "backup": str(backup),
                "preSha256": _sha256_file(source),
                "backupSha256": _sha256_file(backup),
                "preLogicalSha256": plan.database_digest(source),
                "backupLogicalSha256": _database_file_logical_digest(backup),
            }
            for source, backup in database_backups.items()
        ],
        "callbackFiles": [
            {
                "source": str(source),
                "backup": str(backup),
                "preSha256": next(
                    item.original_sha256
                    for item in plan.callback_changes
                    if item.path == source
                ),
                "backupSha256": _sha256_file(backup),
            }
            for source, backup in callback_backups.items()
        ],
    }


def _open_exclusive_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=1.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN EXCLUSIVE")
    except Exception:
        connection.close()
        raise
    return connection


def _apply_sqlite_changes(
    changes: Iterable[SQLiteChange],
    database_path: Path,
    connection: sqlite3.Connection,
) -> None:
    selected = [item for item in changes if item.database_path == database_path]
    if not selected:
        return
    for item in selected:
        cursor = connection.execute(
            f"UPDATE {_quote_identifier(item.table)} "
            f"SET {_quote_identifier(item.column)} = ? "
            f"WHERE rowid = ? AND {_quote_identifier(item.column)} = ?",
            (item.migrated_value, item.rowid, item.original_value),
        )
        if cursor.rowcount != 1:
            raise MigrationError(
                f"预检后数据发生变化，拒绝覆盖: {database_path}:{item.table}.rowid={item.rowid}"
            )


def _apply_callback_changes(
    changes: Iterable[CallbackChange],
    *,
    applied_paths: list[Path] | None = None,
) -> None:
    for item in changes:
        if _sha256_file(item.path) != item.original_sha256:
            raise MigrationError(f"预检后回调文件发生变化，拒绝覆盖: {item.path}")
        if applied_paths is not None:
            applied_paths.append(item.path)
        _atomic_write_bytes(
            item.path,
            item.migrated_bytes,
            mode=item.mode,
            atime_ns=item.atime_ns,
            mtime_ns=item.mtime_ns,
        )


def apply_migration(
    plan: MigrationPlan,
    *,
    timestamp: str | None = None,
) -> Path | None:
    if plan.changed_targets == 0:
        return None

    suffix = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = plan.runtime_dir / "migration_backups" / f"{MIGRATION_NAME}-{suffix}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = backup_dir / "manifest.json"
    database_backups = {
        plan.task_db_path: backup_dir / "task-db.sqlite3",
        plan.knowledge_db_path: backup_dir / "knowledge-db.sqlite3",
    }
    completed_database_backups: dict[Path, Path] = {}
    callback_backups: dict[Path, Path] = {}
    exclusive_connections: dict[Path, sqlite3.Connection] = {}
    modified_database_paths = {
        item.database_path for item in plan.sqlite_changes
    }
    commit_attempted_paths: set[Path] = set()
    applied_callback_paths: list[Path] = []
    manifest: dict[str, Any] | None = None

    try:
        for source, destination in database_backups.items():
            _backup_database(source, destination)
            completed_database_backups[source] = destination
        for item in plan.callback_changes:
            relative = (
                Path("callback") / item.path.name
                if item.path.parent == plan.runtime_dir / "callback"
                else Path(item.path.name)
            )
            destination = backup_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.path, destination)
            callback_backups[item.path] = destination
            if _sha256_file(destination) != item.original_sha256:
                raise MigrationError(
                    f"预检后回调文件发生变化，"
                    f"请确认 DocSense 已停服后重试: {item.path}"
                )

        manifest = _build_manifest(
            plan,
            backup_dir=backup_dir,
            database_backups=completed_database_backups,
            callback_backups=callback_backups,
            status="prepared",
        )
        _write_manifest(manifest_path, manifest)

        for database_path in (plan.task_db_path, plan.knowledge_db_path):
            exclusive_connections[database_path] = _open_exclusive_connection(
                database_path
            )
        for source, backup in completed_database_backups.items():
            source_digest = _database_logical_digest(exclusive_connections[source])
            backup_digest = _database_file_logical_digest(backup)
            expected_digest = plan.database_digest(source)
            if source_digest != expected_digest or backup_digest != expected_digest:
                raise MigrationError(
                    f"预检或备份后数据库发生变化，"
                    f"请确认 DocSense 已停服后重试: {source}"
                )

        _apply_sqlite_changes(
            plan.sqlite_changes,
            plan.task_db_path,
            exclusive_connections[plan.task_db_path],
        )
        _apply_sqlite_changes(
            plan.sqlite_changes,
            plan.knowledge_db_path,
            exclusive_connections[plan.knowledge_db_path],
        )
        _check_connection_integrity(
            exclusive_connections[plan.task_db_path],
            plan.task_db_path,
        )
        _check_connection_integrity(
            exclusive_connections[plan.knowledge_db_path],
            plan.knowledge_db_path,
        )
        _apply_callback_changes(
            plan.callback_changes,
            applied_paths=applied_callback_paths,
        )

        for database_path, connection in exclusive_connections.items():
            if database_path in modified_database_paths:
                commit_attempted_paths.add(database_path)
            connection.commit()
        for connection in exclusive_connections.values():
            connection.close()
        exclusive_connections.clear()
        _check_database_integrity(plan.task_db_path)
        _check_database_integrity(plan.knowledge_db_path)

        manifest["status"] = "applied"
        manifest["completedAt"] = datetime.now().astimezone().isoformat()
        for database in manifest["databases"]:
            database["postSha256"] = _sha256_file(Path(database["source"]))
        for callback_file in manifest["callbackFiles"]:
            callback_file["postSha256"] = _sha256_file(Path(callback_file["source"]))
        _write_manifest(manifest_path, manifest)
        return backup_dir
    except (Exception, KeyboardInterrupt) as exc:
        restore_errors: list[str] = []
        for database_path, connection in exclusive_connections.items():
            try:
                if connection.in_transaction:
                    connection.rollback()
            except Exception as rollback_exc:  # pragma: no cover - 极端 SQLite 故障
                restore_errors.append(f"回滚数据库事务失败 {database_path}: {rollback_exc}")
            try:
                connection.close()
            except Exception as close_exc:  # pragma: no cover - 极端 SQLite 故障
                restore_errors.append(f"关闭数据库连接失败 {database_path}: {close_exc}")
        exclusive_connections.clear()

        for source in commit_attempted_paths:
            backup = completed_database_backups.get(source)
            if backup is None:
                restore_errors.append(f"缺少数据库备份，无法恢复 {source}")
                continue
            try:
                _restore_database(backup, source)
            except Exception as restore_exc:  # pragma: no cover - 极端磁盘/锁故障
                restore_errors.append(f"恢复数据库失败 {source}: {restore_exc}")
        for source in applied_callback_paths:
            backup = callback_backups.get(source)
            if backup is None:
                restore_errors.append(f"缺少回调备份，无法恢复 {source}")
                continue
            try:
                _restore_callback_file(backup, source)
            except Exception as restore_exc:  # pragma: no cover - 极端磁盘故障
                restore_errors.append(f"恢复回调文件失败 {source}: {restore_exc}")

        try:
            failure_manifest = manifest or _build_manifest(
                plan,
                backup_dir=backup_dir,
                database_backups=completed_database_backups,
                callback_backups=callback_backups,
                status="prepared",
            )
        except Exception as manifest_exc:  # pragma: no cover - 极端磁盘故障
            failure_manifest = {
                "migration": MIGRATION_NAME,
                "backupDirectory": str(backup_dir),
                "summary": plan.summary(),
            }
            restore_errors.append(f"构建失败 manifest 失败: {manifest_exc}")
        failure_manifest["status"] = "rollback_failed" if restore_errors else "rolled_back"
        failure_manifest["failedAt"] = datetime.now().astimezone().isoformat()
        failure_manifest["error"] = str(exc)
        failure_manifest["restoreErrors"] = restore_errors
        failure_manifest["summary"]["mode"] = "apply"
        failure_manifest.pop("completedAt", None)
        for target_group in ("databases", "callbackFiles"):
            for target in failure_manifest.get(target_group, []):
                attempted_sha = target.pop("postSha256", None)
                if attempted_sha:
                    target["attemptedPostSha256"] = attempted_sha
                source_path = Path(target["source"])
                if source_path.is_file():
                    target["restoredSha256"] = _sha256_file(source_path)
        try:
            _write_manifest(manifest_path, failure_manifest)
        except OSError:
            pass
        detail = f"迁移失败，已回滚已修改目标: {exc}"
        if restore_errors:
            detail += "; " + "; ".join(restore_errors)
        raise MigrationError(detail) from exc


def _resolve_runtime_dir(
    raw_value: str | None,
    configuration: Mapping[str, str],
) -> Path:
    value = (
        raw_value
        or configuration.get("DOCSENSE_RUNTIME_DIR", "")
        or str(ROOT / ".runtime")
    )
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise MigrationError("DOCSENSE_RUNTIME_DIR/--runtime-dir 必须是绝对路径")
    return path.resolve()


def _resolve_component_path(
    raw_value: str | None,
    env_names: tuple[str, ...],
    fallback: Path,
    configuration: Mapping[str, str],
) -> Path:
    value = raw_value
    if not value:
        for env_name in env_names:
            configured_value = configuration.get(env_name, "").strip()
            if configured_value:
                value = configured_value
                break
    path = Path(value).expanduser() if value else fallback
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "将 /llm/analysis 存量 secrets 键迁移为 security。默认只预检；"
            "执行 --apply 前必须停止 DocSense。"
        )
    )
    parser.add_argument("--apply", action="store_true", help="备份后实际执行迁移；默认仅 dry-run。")
    parser.add_argument("--runtime-dir", help="运行时根目录，必须为绝对路径。")
    parser.add_argument("--task-db", help="任务 SQLite 路径。")
    parser.add_argument("--knowledge-db", help="知识库 SQLite 路径。")
    return parser.parse_args(argv)


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    configuration = {
        str(key): str(value)
        for key, value in dotenv_values(ROOT / ".env").items()
        if value is not None
    }
    configuration.update(os.environ)
    runtime_dir = _resolve_runtime_dir(args.runtime_dir, configuration)
    explicit_runtime = bool(args.runtime_dir)
    task_db = _resolve_component_path(
        args.task_db,
        () if explicit_runtime else ("DOCSENSE_LLM_TASK_DB",),
        runtime_dir / "llm_tasks.sqlite3",
        configuration,
    )
    knowledge_db = _resolve_component_path(
        args.knowledge_db,
        () if explicit_runtime else ("DOCSENSE_KNOWLEDGE_BASE_DB", "KNOWLEDGE_BASE_DB_PATH"),
        runtime_dir / "knowledge_base.sqlite3",
        configuration,
    )
    return task_db, knowledge_db, runtime_dir


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        task_db, knowledge_db, runtime_dir = resolve_paths(args)
        plan = build_migration_plan(
            task_db_path=task_db,
            knowledge_db_path=knowledge_db,
            runtime_dir=runtime_dir,
        )
        output = plan.summary()
        if args.apply:
            backup_dir = apply_migration(plan)
            output["mode"] = "apply"
            output["backupDirectory"] = str(backup_dir) if backup_dir else None
        sys.stdout.write(
            json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        return 0
    except (MigrationError, OSError, sqlite3.Error) as exc:
        sys.stderr.write(f"迁移失败: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
