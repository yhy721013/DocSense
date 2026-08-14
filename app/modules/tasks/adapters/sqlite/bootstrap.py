"""阶段 2 Task Control SQLite 的失败关闭 Bootstrap。

Bootstrap 是构造 Connection Factory、UoW、Store 和后台执行器之前的唯一入口。它先在
进程级 Schema 锁内检查旧库现场，再创建或严格验证 v2 数据库；任何失败都不会自动修复、
归档或删除旧库/目标库。当前锁只解决同机进程协调，不宣称具备分布式租约或 fencing。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from app.modules.tasks.adapters.process_guard import FileProcessSingletonGuard

from .legacy_preflight import PreflightInputError, inspect_old_database
from .schema import (
    TaskControlDatabaseIdentity,
    TaskControlSchemaError,
    create_root_schema,
    install_component_schema,
    validate_task_control_schema,
)


logger = logging.getLogger(__name__)

_AUXILIARY_SUFFIXES = ("-wal", "-shm", "-journal")


class TaskControlBootstrapError(RuntimeError):
    """表示启动前门禁失败；``code`` 可供组合根做稳定分类。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class TaskControlBootstrapResult:
    """Bootstrap 成功后返回的最小、脱敏且不可变结果。"""

    database_path: Path
    identity: TaskControlDatabaseIdentity
    created: bool


def _resolve_database_path(value: str | Path, *, must_exist: bool) -> Path:
    """解析文件身份；存在分支必须是普通文件，禁止把目录当成 SQLite。"""

    try:
        path = Path(value).expanduser().resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise TaskControlBootstrapError(
            "database_path_invalid",
            f"无法解析数据库路径: error_type={type(exc).__name__}",
        ) from exc
    if must_exist and not path.is_file():
        raise TaskControlBootstrapError(
            "database_path_invalid",
            "Task Control 数据库路径不存在或不是普通文件",
        )
    return path


def _path_digest(path: Path) -> str:
    """日志只记录规范路径摘要，不泄漏部署目录或用户名。"""

    identity = os.path.normcase(os.path.realpath(path))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest().upper()[:16]


def _file_set(path: Path) -> tuple[Path, ...]:
    return (path, *(Path(f"{path}{suffix}") for suffix in _AUXILIARY_SUFFIXES))


def _existing_file_set_members(path: Path) -> tuple[Path, ...]:
    return tuple(candidate for candidate in _file_set(path) if candidate.exists())


def _connect_for_validation(path: Path, *, busy_timeout_ms: int) -> sqlite3.Connection:
    """以 mode=rw 打开既有文件，并在任何业务写入前切到 query_only。"""

    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=rw",
        uri=True,
        timeout=busy_timeout_ms / 1000,
        isolation_level=None,
    )
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA query_only = ON")
    return connection


def _strict_validate(
    path: Path,
    *,
    busy_timeout_ms: int,
    known_components: Mapping[str, Mapping[str, object]] | None = None,
    required_components: Mapping[str, int] | None = None,
) -> TaskControlDatabaseIdentity:
    connection = _connect_for_validation(path, busy_timeout_ms=busy_timeout_ms)
    try:
        return validate_task_control_schema(
            connection,
            known_components=known_components,
            required_components=required_components,
        )
    finally:
        connection.close()


def _cleanup_owned_temp_file_set(path: Path) -> None:
    """只清理本次 Bootstrap 的随机临时文件，绝不接受目标库或旧库路径。"""

    if ".bootstrap-" not in path.name:
        raise TaskControlBootstrapError(
            "bootstrap_temp_identity_lost",
            "拒绝清理无法证明归属的临时文件",
        )
    for candidate in reversed(_file_set(path)):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            logger.exception(
                "清理 Task Control Bootstrap 临时文件失败: path_sha256=%s member=%s",
                _path_digest(path),
                candidate.name.removeprefix(path.name) or "main",
            )


def _preflight_old_database(
    old_path: Path,
    new_path: Path,
    *,
    immutable_offline_snapshot: bool,
    writers_stopped_confirmed: bool,
) -> None:
    try:
        result = inspect_old_database(
            old_path,
            new_path,
            immutable_offline_snapshot=immutable_offline_snapshot,
            writers_stopped_confirmed=writers_stopped_confirmed,
        )
    except (OSError, sqlite3.Error, PreflightInputError) as exc:
        raise TaskControlBootstrapError(
            "legacy_preflight_failed",
            f"旧 Task 数据库只读预检失败: error_type={type(exc).__name__}",
        ) from exc
    status = str(result["status"])
    if status != "safe_for_empty_v2_initialization":
        raise TaskControlBootstrapError(
            "legacy_preflight_blocked",
            "旧 Task 数据库仍有阻塞事实或不兼容结构: "
            f"status={status} blocker_count={int(result['blockerCount'])} "
            f"schema_error_count={len(result['schemaErrors'])}",
        )
    logger.info(
        "旧 Task 数据库只读预检通过: old_path_sha256=%s new_path_sha256=%s",
        str(result["oldPathSha256"])[:16],
        str(result["newPathSha256"])[:16],
    )


def _create_unpublished_database(
    target_path: Path,
    *,
    busy_timeout_ms: int,
    known_components: Mapping[str, Mapping[str, object]],
    required_components: Mapping[str, int],
) -> tuple[Path, TaskControlDatabaseIdentity]:
    """在目标同目录创建并验证随机临时库，尚不触碰目标文件名。"""

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(
        f".{target_path.name}.bootstrap-{uuid4().hex}.sqlite3"
    )
    if _existing_file_set_members(temp_path):
        raise TaskControlBootstrapError(
            "bootstrap_temp_collision",
            "随机 Bootstrap 临时文件发生碰撞",
        )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            temp_path,
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        create_root_schema(
            connection,
            db_instance_uuid=str(uuid4()),
            created_at=created_at,
        )
        for component_name in sorted(required_components):
            manifest = known_components.get(component_name)
            if manifest is None:
                raise TaskControlBootstrapError(
                    "bootstrap_required_component_unknown",
                    "必需组件缺少当前发布版本 Manifest",
                )
            install_component_schema(
                connection,
                manifest,
                installed_at=datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                ),
                known_components=known_components,
            )
    except Exception:
        if connection is not None:
            connection.close()
        _cleanup_owned_temp_file_set(temp_path)
        raise
    else:
        connection.close()

    sidecars = tuple(candidate for candidate in _file_set(temp_path)[1:] if candidate.exists())
    if sidecars:
        _cleanup_owned_temp_file_set(temp_path)
        raise TaskControlBootstrapError(
            "bootstrap_temp_sidecar_remaining",
            f"临时数据库关闭后仍有侧车文件: count={len(sidecars)}",
        )
    try:
        identity = _strict_validate(
            temp_path,
            busy_timeout_ms=busy_timeout_ms,
            known_components=known_components,
            required_components=required_components,
        )
    except Exception:
        _cleanup_owned_temp_file_set(temp_path)
        raise
    return temp_path, identity


def _publish_without_overwrite(temp_path: Path, target_path: Path) -> None:
    """用同文件系统硬链接原子发布；目标已出现时内核必须拒绝覆盖。"""

    try:
        os.link(temp_path, target_path)
    except FileExistsError as exc:
        raise TaskControlBootstrapError(
            "bootstrap_publish_race",
            "发布前目标数据库已由其他参与者创建",
        ) from exc
    except OSError as exc:
        raise TaskControlBootstrapError(
            "bootstrap_atomic_publish_unsupported",
            f"当前文件系统无法执行不覆盖原子发布: error_type={type(exc).__name__}",
        ) from exc
    try:
        temp_path.unlink()
    except OSError:
        # 目标硬链接已经成功发布，不能因临时别名清理失败而删除目标；保留日志供运维处理。
        logger.exception(
            "Task Control 数据库已发布但临时硬链接清理失败: target_path_sha256=%s",
            _path_digest(target_path),
        )


def bootstrap_task_control_database(
    old_database_path: str | Path,
    new_database_path: str | Path,
    *,
    busy_timeout_ms: int = 5_000,
    immutable_offline_snapshot: bool = False,
    writers_stopped_confirmed: bool = False,
    known_components: Mapping[str, Mapping[str, object]] | None = None,
    required_components: Mapping[str, int] | None = None,
) -> TaskControlBootstrapResult:
    """预检旧库后创建或严格打开 v2 Task Control SQLite。

    本函数没有 Store/线程副作用。调用方只有拿到成功结果后，才可以进入阶段 2-2 第 2 步的
    Connection Factory 与 UoW 构造。
    """

    if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
        raise TypeError("busy_timeout_ms 必须是整数")
    if busy_timeout_ms < 1 or busy_timeout_ms > 60_000:
        raise ValueError("busy_timeout_ms 必须位于 1..60000")

    known = dict(known_components or {})
    required = dict(required_components or {})
    for component_name, version in required.items():
        manifest = known.get(component_name)
        if manifest is None or manifest.get("componentVersion") != version:
            raise TaskControlBootstrapError(
                "bootstrap_required_component_unknown",
                "必需组件版本必须与当前发布版本 Manifest 完全一致",
            )

    old_path = _resolve_database_path(old_database_path, must_exist=True)
    new_path = _resolve_database_path(new_database_path, must_exist=False)
    if os.path.normcase(os.path.realpath(old_path)) == os.path.normcase(
        os.path.realpath(new_path)
    ):
        raise TaskControlBootstrapError(
            "database_path_conflict",
            "旧 Task 数据库与 v2 Task Control 数据库解析后路径相同",
        )
    lock = FileProcessSingletonGuard(
        new_path.with_name(f"{new_path.name}.schema.lock"),
        component_name="Task Control Schema Bootstrap",
        event_logger=logger,
        path_log_value=f"sha256:{_path_digest(new_path)}",
    )
    if not lock.acquire():
        raise TaskControlBootstrapError(
            "bootstrap_schema_lock_busy",
            "Task Control Schema Bootstrap 锁已被占用",
        )
    created = False
    try:
        members = _existing_file_set_members(new_path)
        if new_path.exists():
            # 旧库预检只属于“首次发布空 v2 控制面”的切换门禁。v2 已经发布后，尚未
            # 迁移的业务会继续在旧库产生合法活动事实；若每次重启仍要求旧库全空，
            # Report 试点会被 Weaponry/Analysis 的正常任务永久阻断。重开路径只严格
            # 校验 v2 自身身份与组件，不从旧库迁移、补权或修复任何事实。
            logger.info(
                "Task Control 已存在，跳过仅首次发布使用的旧库空现场预检: "
                "new_path_sha256=%s",
                _path_digest(new_path),
            )
            if not new_path.is_file():
                raise TaskControlBootstrapError(
                    "target_file_set_conflict",
                    "目标数据库主路径不是普通文件",
                )
            journal_path = Path(f"{new_path}-journal")
            if journal_path.exists():
                raise TaskControlBootstrapError(
                    "target_hot_journal_present",
                    "目标数据库存在 journal，拒绝在启动路径中自动恢复或删除",
                )
            # 已存在数据库只允许在应用后台线程尚未启动、仍持有 Schema 启动锁时安装缺失组件。
            connection = sqlite3.connect(
                f"{new_path.as_uri()}?mode=rw",
                uri=True,
                timeout=busy_timeout_ms / 1000,
                isolation_level=None,
            )
            try:
                connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
                connection.execute("PRAGMA foreign_keys = ON")
                current = validate_task_control_schema(
                    connection,
                    known_components=known,
                )
                for component_name in sorted(set(required) - set(current.registered_components)):
                    install_component_schema(
                        connection,
                        known[component_name],
                        installed_at=datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%S.%fZ"
                        ),
                        known_components=known,
                    )
                identity = validate_task_control_schema(
                    connection,
                    known_components=known,
                    required_components=required,
                )
            finally:
                connection.close()
        else:
            _preflight_old_database(
                old_path,
                new_path,
                immutable_offline_snapshot=immutable_offline_snapshot,
                writers_stopped_confirmed=writers_stopped_confirmed,
            )
            if members:
                raise TaskControlBootstrapError(
                    "target_file_set_conflict",
                    f"目标主文件不存在但发现残留侧车: count={len(members)}",
                )
            temp_path, _ = _create_unpublished_database(
                new_path,
                busy_timeout_ms=busy_timeout_ms,
                known_components=known,
                required_components=required,
            )
            try:
                if _existing_file_set_members(new_path):
                    raise TaskControlBootstrapError(
                        "bootstrap_publish_race",
                        "临时库完成后目标文件集已发生变化",
                    )
                _publish_without_overwrite(temp_path, new_path)
            finally:
                _cleanup_owned_temp_file_set(temp_path)
            created = True
            identity = _strict_validate(
                new_path,
                busy_timeout_ms=busy_timeout_ms,
                known_components=known,
                required_components=required,
            )
        logger.info(
            "Task Control 数据库 Bootstrap 通过: path_sha256=%s created=%s "
            "db_instance_uuid_prefix=%s root_fingerprint_prefix=%s components=%d",
            _path_digest(new_path),
            created,
            identity.db_instance_uuid[:8],
            identity.root_fingerprint[:12],
            len(identity.registered_components),
        )
        return TaskControlBootstrapResult(
            database_path=new_path,
            identity=identity,
            created=created,
        )
    except TaskControlBootstrapError:
        raise
    except TaskControlSchemaError as exc:
        raise TaskControlBootstrapError(exc.code, str(exc)) from exc
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        code = (
            "bootstrap_sqlite_busy"
            if "busy" in message or "locked" in message
            else "bootstrap_sqlite_operational_error"
        )
        raise TaskControlBootstrapError(code, f"SQLite Bootstrap 失败: error_type={type(exc).__name__}") from exc
    except sqlite3.Error as exc:
        raise TaskControlBootstrapError(
            "bootstrap_sqlite_error",
            f"SQLite Bootstrap 失败: error_type={type(exc).__name__}",
        ) from exc
    finally:
        lock.release()


__all__ = [
    "TaskControlBootstrapError",
    "TaskControlBootstrapResult",
    "bootstrap_task_control_database",
]
