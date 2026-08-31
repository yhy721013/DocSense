"""SQLite 单实例的 Conversation 公开身份与世代仓储。"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from app.modules.chat.adapters.sqlite.repositories import (
    _connect,
    _required_text,
    _utc_now_iso,
    ensure_chat_schema,
)
from app.modules.chat.domain.identity import (
    ConversationIdentity,
    ConversationIdentityBinding,
    FileChatIdentity,
    IDENTITY_KIND_FILE,
    IDENTITY_KIND_WEAPONRY,
    WeaponryChatIdentity,
    require_conversation_id,
)
from app.modules.chat.domain.models import (
    CLEANUP_JOB_SUCCEEDED,
    CLEANUP_REASON_DELETE_CHAT,
    LEASE_CLOSED,
    SESSION_ACTIVE,
    SESSION_DELETED,
    SESSION_DELETING,
    ChatSession,
)
from app.modules.chat.ports.identities import (
    ConversationAdmissionBusyError,
    ConversationAdmissionLease,
    ConversationAdmissionLostError,
    ConversationIdentityConflictError,
    ConversationIdentityStore,
    ConversationResolution,
    FileConversationTombstonedError,
)


logger = logging.getLogger(__name__)
DEFAULT_CONVERSATION_ADMISSION_SECONDS = 30


class SQLiteConversationIdentityRepository(ConversationIdentityStore):
    """以 `BEGIN IMMEDIATE` 保护身份创建和释放线性化点。"""

    def __init__(
        self,
        db_path: str,
        *,
        owner_instance_id: str | None = None,
        admission_seconds: int = DEFAULT_CONVERSATION_ADMISSION_SECONDS,
        initialize: bool = True,
    ) -> None:
        self.db_path = _required_text(db_path, name="db_path")
        if isinstance(admission_seconds, bool) or not isinstance(
            admission_seconds,
            int,
        ) or admission_seconds < 1:
            raise ValueError("admission_seconds must be a positive integer")
        self._owner_instance_id = _required_text(
            owner_instance_id or str(uuid.uuid4()),
            name="owner_instance_id",
        )
        self._admission_seconds = admission_seconds
        if initialize:
            ensure_chat_schema(self.db_path)

    def resolve_active(
        self,
        identity: ConversationIdentity,
    ) -> ConversationResolution | None:
        resolution = self.resolve_any(identity)
        if resolution is None:
            return None
        if not resolution.binding.active or resolution.session.status == SESSION_DELETED:
            return None
        return resolution

    def resolve_any(
        self,
        identity: ConversationIdentity,
    ) -> ConversationResolution | None:
        normalized = self._require_identity(identity)
        connection = _connect(self.db_path)
        try:
            row = self._select_identity(connection, normalized)
            return None if row is None else self._resolution_from_row(row)
        finally:
            connection.close()

    def get_by_conversation_id(
        self,
        conversation_id: str,
    ) -> ConversationResolution | None:
        """按内部 UUID 查询身份绑定，不要求该绑定仍处于活动状态。"""

        normalized_id = require_conversation_id(conversation_id)
        connection = _connect(self.db_path)
        try:
            row = connection.execute(
                """
                SELECT identity.*, session.workspace_ref, session.thread_ref,
                       session.status AS session_status,
                       session.created_at AS session_created_at,
                       session.updated_at AS session_updated_at,
                       session.metadata_json
                FROM conversation_identities AS identity
                JOIN conversations AS session
                  ON session.conversation_id = identity.conversation_id
                WHERE identity.conversation_id = ?
                """,
                (normalized_id,),
            ).fetchone()
            return None if row is None else self._resolution_from_row(row)
        finally:
            connection.close()

    def reserve_admission(
        self,
        identity: ConversationIdentity,
    ) -> ConversationAdmissionLease:
        normalized = self._require_identity(identity)
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=self._admission_seconds)).isoformat()
        lease = ConversationAdmissionLease(
            identity_key=normalized.identity_key,
            identity_kind=normalized.identity_kind,
            admission_token=str(uuid.uuid4()),
            owner_instance_id=self._owner_instance_id,
            expires_at=expires_at,
        )
        connection = _connect(self.db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM conversation_admissions WHERE expires_at <= ?",
                (now.isoformat(),),
            )
            fields = self._identity_fields(normalized)
            try:
                connection.execute(
                    """
                    INSERT INTO conversation_admissions(
                        identity_key, identity_kind, chat_id, user_id,
                        architecture_id, admission_token, owner_instance_id,
                        scope_mode, requested_scope_architecture_id,
                        expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized.identity_key,
                        normalized.identity_kind,
                        fields[0],
                        fields[1],
                        fields[2],
                        lease.admission_token,
                        lease.owner_instance_id,
                        (
                            "files"
                            if normalized.identity_kind == IDENTITY_KIND_FILE
                            else "architecture"
                        ),
                        (
                            None
                            if normalized.identity_kind == IDENTITY_KIND_FILE
                            else fields[2]
                        ),
                        lease.expires_at,
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConversationAdmissionBusyError(
                    "current conversation identity already has an admission"
                ) from exc
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        logger.info(
            "Conversation 身份准入已保留: identity_kind=%s",
            normalized.identity_kind,
        )
        return lease

    def release_admission(self, lease: ConversationAdmissionLease) -> bool:
        if not isinstance(lease, ConversationAdmissionLease):
            raise TypeError("lease must be ConversationAdmissionLease")
        connection = _connect(self.db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM conversation_admissions
                WHERE identity_key = ? AND admission_token = ?
                  AND owner_instance_id = ?
                """,
                (
                    lease.identity_key,
                    lease.admission_token,
                    lease.owner_instance_id,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_conversation(
        self,
        identity: ConversationIdentity,
        *,
        admission_lease: ConversationAdmissionLease | None = None,
    ) -> ConversationResolution:
        normalized = self._require_identity(identity)
        if admission_lease is not None:
            self._validate_lease_identity(admission_lease, normalized)
        now = _utc_now_iso()
        conversation_id = str(uuid.uuid4())
        connection = _connect(self.db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if admission_lease is not None:
                guard = connection.execute(
                    """
                    SELECT expires_at FROM conversation_admissions
                    WHERE identity_key = ? AND admission_token = ?
                      AND owner_instance_id = ?
                    """,
                    (
                        admission_lease.identity_key,
                        admission_lease.admission_token,
                        admission_lease.owner_instance_id,
                    ),
                ).fetchone()
                if guard is None or str(guard["expires_at"]) <= now:
                    raise ConversationAdmissionLostError(
                        "conversation admission is no longer valid"
                    )
            existing = self._select_identity(connection, normalized)
            if existing is not None:
                existing_resolution = self._resolution_from_row(existing)
                if (
                    isinstance(normalized, FileChatIdentity)
                    and existing_resolution.session.status == SESSION_DELETED
                ):
                    raise FileConversationTombstonedError(
                        "file chat identity has already been deleted"
                    )
                if (
                    isinstance(normalized, FileChatIdentity)
                    or existing_resolution.binding.active
                ):
                    raise ConversationIdentityConflictError(
                        "conversation identity is already occupied"
                    )
            connection.execute(
                """
                INSERT INTO conversations(
                    conversation_id, workspace_ref, thread_ref, status,
                    created_at, updated_at, metadata_json
                ) VALUES (?, '', '', ?, ?, ?, ?)
                """,
                (conversation_id, SESSION_ACTIVE, now, now, json.dumps({})),
            )
            fields = self._identity_fields(normalized)
            try:
                connection.execute(
                    """
                    INSERT INTO conversation_identities(
                        conversation_id, identity_kind, chat_id, user_id,
                        architecture_id, active, created_at, released_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, '')
                    """,
                    (
                        conversation_id,
                        normalized.identity_kind,
                        fields[0],
                        fields[1],
                        fields[2],
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConversationIdentityConflictError(
                    "conversation identity is already occupied"
                ) from exc
            if admission_lease is not None:
                consumed = connection.execute(
                    """
                    DELETE FROM conversation_admissions
                    WHERE identity_key = ? AND admission_token = ?
                      AND owner_instance_id = ?
                    """,
                    (
                        admission_lease.identity_key,
                        admission_lease.admission_token,
                        admission_lease.owner_instance_id,
                    ),
                )
                if consumed.rowcount != 1:
                    raise ConversationAdmissionLostError(
                        "conversation admission could not be consumed"
                    )
            row = self._select_identity(connection, normalized)
            if row is None:
                raise RuntimeError("created conversation cannot be reloaded")
            resolution = self._resolution_from_row(row)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        logger.info(
            "Conversation 世代已创建: identity_kind=%s",
            normalized.identity_kind,
        )
        return resolution

    def finalize_completed_delete(
        self,
        conversation_id: str,
    ) -> None:
        """以一个事务完成本地删除终态，禁止暴露可恢复正文的半完成状态。

        本方法不会只信任应用服务的调用顺序，而会在同一写事务内重新确认会话已进入
        ``deleting``、删除清理任务已经成功且不存在未关闭租约。Weaponry 会话随后物理
        删除整个在线聚合，仅保留独立的最小审计行；File 会话继续沿用既有策略，只清除
        消息正文并保留不可复用的全世代墓碑。
        """

        normalized_id = require_conversation_id(conversation_id)
        now = _utc_now_iso()
        connection = _connect(self.db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT identity.*, session.status AS session_status
                FROM conversation_identities AS identity
                JOIN conversations AS session
                  ON session.conversation_id = identity.conversation_id
                WHERE identity.conversation_id = ?
                """,
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise ValueError("conversation identity does not exist")
            if row["session_status"] != SESSION_DELETING:
                raise ValueError(
                    "conversation must be deleting before finalization"
                )
            cleanup_succeeded = connection.execute(
                """
                SELECT 1
                FROM chat_cleanup_jobs
                WHERE conversation_id = ? AND reason = ? AND status = ?
                LIMIT 1
                """,
                (
                    normalized_id,
                    CLEANUP_REASON_DELETE_CHAT,
                    CLEANUP_JOB_SUCCEEDED,
                ),
            ).fetchone()
            if cleanup_succeeded is None:
                raise ValueError(
                    "conversation delete cleanup has not succeeded"
                )
            open_lease_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM chat_resource_leases
                    WHERE conversation_id = ? AND status != ?
                    """,
                    (normalized_id, LEASE_CLOSED),
                ).fetchone()[0]
            )
            if open_lease_count:
                raise ValueError(
                    "conversation still has unresolved resource leases"
                )
            identity_kind = str(row["identity_kind"])
            connection.execute(
                """
                INSERT INTO conversation_deletion_audits(
                    conversation_id, identity_kind, deletion_status,
                    cleanup_result, deleted_at
                ) VALUES (?, ?, 'deleted', 'succeeded', ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    deletion_status = excluded.deletion_status,
                    cleanup_result = excluded.cleanup_result,
                    deleted_at = excluded.deleted_at
                """,
                (normalized_id, identity_kind, now),
            )
            if identity_kind == IDENTITY_KIND_WEAPONRY:
                # 身份表的外键未设置级联，必须先显式删除；其余运行、消息、Chunk、范围、
                # 绑定、租约和清理任务均由 conversations 的 ON DELETE CASCADE 清除。
                connection.execute(
                    "DELETE FROM conversation_identities WHERE conversation_id = ?",
                    (normalized_id,),
                )
                deleted = connection.execute(
                    "DELETE FROM conversations WHERE conversation_id = ?",
                    (normalized_id,),
                )
                if deleted.rowcount != 1:
                    raise RuntimeError("conversation aggregate could not be purged")
            else:
                connection.execute(
                    "DELETE FROM chat_messages WHERE conversation_id = ?",
                    (normalized_id,),
                )
                connection.execute(
                    """
                    UPDATE conversations
                    SET status = ?, updated_at = ?
                    WHERE conversation_id = ?
                    """,
                    (SESSION_DELETED, now, normalized_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        logger.info(
            "Conversation 本地删除终态已原子提交: identity_kind=%s",
            identity_kind,
        )

    @staticmethod
    def _require_identity(identity: ConversationIdentity) -> ConversationIdentity:
        if not isinstance(identity, ConversationIdentity):
            raise TypeError("identity must implement ConversationIdentity")
        if not isinstance(identity, (FileChatIdentity, WeaponryChatIdentity)):
            raise TypeError("identity implementation is unsupported")
        return identity

    @staticmethod
    def _identity_fields(
        identity: ConversationIdentity,
    ) -> tuple[int | None, int | None, int | None]:
        if isinstance(identity, FileChatIdentity):
            return identity.chat_id, None, None
        assert isinstance(identity, WeaponryChatIdentity)
        return None, identity.user_id, identity.architecture_id

    @staticmethod
    def _validate_lease_identity(
        lease: ConversationAdmissionLease,
        identity: ConversationIdentity,
    ) -> None:
        if not isinstance(lease, ConversationAdmissionLease):
            raise TypeError("admission_lease must be ConversationAdmissionLease")
        if (
            lease.identity_key != identity.identity_key
            or lease.identity_kind != identity.identity_kind
        ):
            raise ConversationAdmissionLostError(
                "conversation admission belongs to another identity"
            )

    @staticmethod
    def _select_identity(
        connection: sqlite3.Connection,
        identity: ConversationIdentity,
    ) -> sqlite3.Row | None:
        if isinstance(identity, FileChatIdentity):
            return connection.execute(
                """
                SELECT identity.*, session.workspace_ref, session.thread_ref,
                       session.status AS session_status,
                       session.created_at AS session_created_at,
                       session.updated_at AS session_updated_at,
                       session.metadata_json
                FROM conversation_identities AS identity
                JOIN conversations AS session
                  ON session.conversation_id = identity.conversation_id
                WHERE identity.identity_kind = 'file' AND identity.chat_id = ?
                """,
                (identity.chat_id,),
            ).fetchone()
        assert isinstance(identity, WeaponryChatIdentity)
        return connection.execute(
            """
            SELECT identity.*, session.workspace_ref, session.thread_ref,
                   session.status AS session_status,
                   session.created_at AS session_created_at,
                   session.updated_at AS session_updated_at,
                   session.metadata_json
            FROM conversation_identities AS identity
            JOIN conversations AS session
              ON session.conversation_id = identity.conversation_id
            WHERE identity.identity_kind = 'weaponry'
              AND identity.user_id = ? AND identity.architecture_id = ?
            ORDER BY identity.active DESC, identity.created_at DESC
            LIMIT 1
            """,
            (identity.user_id, identity.architecture_id),
        ).fetchone()

    @classmethod
    def _resolution_from_row(cls, row: sqlite3.Row) -> ConversationResolution:
        return ConversationResolution(
            session=ChatSession(
                conversation_id=row["conversation_id"],
                workspace_ref=row["workspace_ref"],
                thread_ref=row["thread_ref"],
                status=row["session_status"],
                created_at=row["session_created_at"],
                updated_at=row["session_updated_at"],
                metadata=json.loads(row["metadata_json"] or "{}"),
            ),
            binding=cls._binding_from_row(row),
        )

    @staticmethod
    def _binding_from_row(row: sqlite3.Row) -> ConversationIdentityBinding:
        return ConversationIdentityBinding(
            conversation_id=row["conversation_id"],
            identity_kind=row["identity_kind"],
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            architecture_id=row["architecture_id"],
            active=bool(row["active"]),
            created_at=row["created_at"],
            released_at=row["released_at"],
        )


__all__ = [
    "DEFAULT_CONVERSATION_ADMISSION_SECONDS",
    "SQLiteConversationIdentityRepository",
]
