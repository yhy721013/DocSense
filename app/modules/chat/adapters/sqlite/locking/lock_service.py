"""用于文件对话请求隔离的持久化 ChatRun 锁服务。"""

from __future__ import annotations

import os
import socket
import uuid
import json

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from app.modules.chat.domain.models import (
    MESSAGE_COMMITTED,
    MESSAGE_DISCARDED,
    MESSAGE_PENDING,
    MESSAGE_ROLE_ASSISTANT,
    LEASE_ACTIVE,
    LEASE_CLEANUP_PENDING,
    LEASE_PLANNED,
    RESOURCE_THREAD,
    RUN_ABORTED,
    RUN_ACCEPTED,
    RUN_ACTIVE_STATUSES,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    SESSION_ACTIVE,
    SESSION_DELETED,
    SESSION_DELETING,
    SESSION_ERROR,
    ChatMessageSourceChunk,
    ChatRun,
)
from app.modules.chat.domain.document_candidates import (
    ChatDocumentSelectionCandidates,
)
from app.modules.chat.domain.document_scope import (
    CHAT_SCOPE_MODE_ARCHITECTURE,
    CHAT_SCOPE_MODE_FILES,
    ChatScopeRevision,
    ChatScopeSelector,
    decide_chat_architecture_scope,
    decide_chat_document_scope,
    decide_chat_session_scope_binding,
)
from app.modules.chat.domain.events import ChatStreamEvent
from app.modules.chat.domain.identity import (
    ConversationIdentity,
    FileChatIdentity,
    IDENTITY_KIND_FILE,
)
from app.modules.chat.ports.coordination import (
    ChatAdmissionBusyError,
    ChatAdmissionLease,
    ChatRunBusyError,
    ChatRunInactiveError,
    ChatRunLease,
    ChatRunLeaseCapabilities,
    ChatRunLeaseLostError,
    ChatSessionDeleteBusyError,
    ChatSessionUnavailableError,
    SINGLE_INSTANCE_CHAT_RUN_LEASE_CAPABILITIES,
)
from app.modules.chat.adapters.sqlite.event_repository import ChatRunEventRepository
from app.modules.chat.adapters.sqlite.identity_repository import (
    SQLiteConversationIdentityRepository,
)
from app.modules.chat.adapters.sqlite.repositories import (
    ChatRunRepository,
    ChatMessageSourceRepository,
    ChatScopeRepository,
    ChatSessionScopeBindingRepository,
    _connection_scope,
    _optional_canonical_text,
    _optional_text,
    _required_text,
    _utc_now_iso,
    chat_scope_revision_id_for_run,
    ensure_chat_schema,
)


DEFAULT_STALE_RUN_SECONDS = 6 * 60 * 60
DEFAULT_CHAT_ADMISSION_SECONDS = 5 * 60
logger = logging.getLogger(__name__)


def _default_owner_instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ChatRunLockService:
    """SQLite 单实例运行协调器及其兼容锁服务入口。

    名称保留是为了兼容已存在的应用装配与测试；它并不是分布式锁实现。共享
    持久化落地后，应由新的 ``ChatRunCoordinator`` 适配器替换本类，并以真实
    租约和围栏令牌把心跳和终态提交收敛为条件更新。
    """

    def __init__(
        self,
        db_path: str,
        *,
        owner_instance_id: str | None = None,
        stale_after_seconds: float | None = DEFAULT_STALE_RUN_SECONDS,
    ) -> None:
        self.db_path = _required_text(db_path, name="db_path")
        self.owner_instance_id = _optional_text(
            owner_instance_id,
        ) or _default_owner_instance_id()
        if stale_after_seconds is not None and stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive or None")
        self.stale_after_seconds = stale_after_seconds
        ensure_chat_schema(self.db_path)
        self._runs = ChatRunRepository(self.db_path, initialize=False)
        logger.info(
            "文件对话 SQLite 单实例运行协调器已初始化: db_path=%s "
            "has_owner_instance=%s stale_after_seconds=%s",
            self.db_path,
            bool(self.owner_instance_id),
            self.stale_after_seconds,
        )

    @property
    def lease_capabilities(self) -> ChatRunLeaseCapabilities:
        """返回当前 SQLite 实现的真实租约能力，明确不含跨实例围栏。"""
        return SINGLE_INSTANCE_CHAT_RUN_LEASE_CAPABILITIES

    def try_acquire_chat_run(
        self,
        *,
        identity: ConversationIdentity,
        run_id: str | None = None,
        user_message: str | None = None,
        user_files: tuple[tuple[str, str], ...] = (),
        input_documents: tuple[tuple[str, str, str, str, str], ...] = (),
        document_candidates: ChatDocumentSelectionCandidates | None = None,
        scope_selector: ChatScopeSelector | None = None,
        admission_lease: ChatAdmissionLease | None = None,
        max_files_per_request: int | None = None,
    ) -> ChatRun:
        normalized_identity = SQLiteConversationIdentityRepository._require_identity(
            identity
        )
        normalized_run_id = _optional_text(run_id) or uuid.uuid4().hex
        if document_candidates is not None and not isinstance(
            document_candidates,
            ChatDocumentSelectionCandidates,
        ):
            raise TypeError(
                "document_candidates must be ChatDocumentSelectionCandidates "
                "or None"
            )
        if document_candidates is not None and (
            user_files or input_documents
        ):
            raise ValueError(
                "document_candidates cannot be combined with legacy "
                "document tuples"
            )
        normalized_scope_selector = self._normalize_scope_selector(
            scope_selector=scope_selector,
            document_candidates=document_candidates,
            user_files=user_files,
            input_documents=input_documents,
        )
        if admission_lease is not None:
            self._validate_admission_lease_shape(
                lease=admission_lease,
                identity=normalized_identity,
                scope_selector=normalized_scope_selector,
            )
        if max_files_per_request is not None and (
            isinstance(max_files_per_request, bool)
            or not isinstance(max_files_per_request, int)
            or max_files_per_request < 1
        ):
            raise ValueError("max_files_per_request must be a positive integer")
        now = _utc_now_iso()
        active_statuses = tuple(sorted(RUN_ACTIVE_STATUSES))

        with _connection_scope(self.db_path) as connection:
            # ``BEGIN IMMEDIATE`` 语句会在 SQLite 中立即取得写锁，保证“检查活动运行”和
            # “插入新运行”处于同一个临界区。后续替换为 PostgreSQL 或 Redis 时，
            # 这里就是分布式互斥语义的边界。
            connection.execute("BEGIN IMMEDIATE")
            if admission_lease is not None:
                self._require_admission_in_transaction(
                    connection,
                    lease=admission_lease,
                    scope_selector=normalized_scope_selector,
                    now=now,
                )
            identity_row = SQLiteConversationIdentityRepository._select_identity(
                connection,
                normalized_identity,
            )
            identity_resolution = (
                None
                if identity_row is None
                else SQLiteConversationIdentityRepository._resolution_from_row(
                    identity_row
                )
            )
            if (
                identity_resolution is not None
                and identity_resolution.binding.active
            ):
                normalized_conversation_id = (
                    identity_resolution.conversation_id
                )
                session_created = False
            else:
                # Released Weaponry 世代不占用当前复合身份；File identity 永不释放，
                # 因此只有从未存在的 File 或已释放的 Weaponry 会进入这里。
                normalized_conversation_id = str(uuid.uuid4())
                session_created = True
            logger.info(
                "尝试获取 Chat 运行锁: identity_kind=%s run_id=%s "
                "session_created=%s has_owner_instance=%s",
                normalized_identity.identity_kind,
                normalized_run_id,
                session_created,
                bool(self.owner_instance_id),
            )
            if session_created:
                connection.execute(
                    """
                    INSERT INTO conversations (
                        conversation_id, workspace_ref, thread_ref, status,
                        created_at, updated_at, metadata_json
                    ) VALUES (?, '', '', 'active', ?, ?, '{}')
                    """,
                    (normalized_conversation_id, now, now),
                )
                public_fields = (
                    SQLiteConversationIdentityRepository._identity_fields(
                        normalized_identity
                    )
                )
                connection.execute(
                    """
                    INSERT INTO conversation_identities(
                        conversation_id, identity_kind, chat_id, user_id,
                        architecture_id, active, created_at, released_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, '')
                    """,
                    (
                        normalized_conversation_id,
                        normalized_identity.identity_kind,
                        public_fields[0],
                        public_fields[1],
                        public_fields[2],
                        now,
                    ),
                )
            session_row = connection.execute(
                "SELECT status FROM conversations WHERE conversation_id = ?",
                (normalized_conversation_id,),
            ).fetchone()
            if session_row is None:
                raise ValueError("chat_session was not created")
            if session_row["status"] != SESSION_ACTIVE:
                raise ChatSessionUnavailableError(
                    conversation_id=normalized_conversation_id,
                    status=session_row["status"],
                )
            existing_binding = (
                ChatSessionScopeBindingRepository.get_in_transaction(
                    connection,
                    conversation_id=normalized_conversation_id,
                )
            )
            if session_created and existing_binding is not None:
                raise ValueError(
                    "new chat session unexpectedly has scope binding"
                )
            if not session_created and existing_binding is None:
                logger.error(
                    "既有文件对话缺少不可变范围绑定，受理失败关闭: "
                    "conversation_id=%s run_id=%s",
                    normalized_conversation_id,
                    normalized_run_id,
                )
                raise ValueError(
                    "existing chat session is missing immutable scope binding"
                )
            scope_binding, creates_binding = (
                decide_chat_session_scope_binding(
                    conversation_id=normalized_conversation_id,
                    selector=normalized_scope_selector,
                    existing_binding=existing_binding,
                    created_at=now,
                )
            )
            if creates_binding:
                ChatSessionScopeBindingRepository.create_in_transaction(
                    connection,
                    binding=scope_binding,
                )
            # 不可变模式和 architecture ID 必须在活跃运行检查前裁决；否则同一
            # chatId 的错误模式请求会被不稳定地伪装成普通“流正在进行”。
            self._expire_stale_runs(
                connection,
                conversation_id=normalized_conversation_id,
                now=now,
                active_statuses=active_statuses,
            )
            active_row = connection.execute(
                f"""
                SELECT * FROM chat_runs
                WHERE conversation_id = ? AND status IN ({",".join("?" for _ in active_statuses)})
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (normalized_conversation_id, *active_statuses),
            ).fetchone()
            if active_row is not None:
                logger.warning(
                    "文件对话运行锁获取失败，存在活跃运行: conversation_id=%s active_run_id=%s active_status=%s",
                    normalized_conversation_id,
                    active_row["run_id"],
                    active_row["status"],
                )
                raise ChatRunBusyError(
                    conversation_id=normalized_conversation_id,
                    active_run_id=active_row["run_id"],
                )

            history_user_files = user_files
            effective_input_documents = input_documents
            explicit_candidate_count = len(input_documents)
            default_candidate_count = 0
            selection_mode = "legacy_input"
            effective_scope_revision_id = ""
            scope_decision = None
            current_scope_head = None
            if document_candidates is not None:
                explicit_candidate_count = len(
                    document_candidates.explicit_documents
                )
                default_candidate_count = len(
                    document_candidates.new_session_default_documents
                )
                current_scope_head = (
                    ChatScopeRepository.get_head_in_transaction(
                        connection,
                        conversation_id=normalized_conversation_id,
                    )
                )
                if session_created:
                    if current_scope_head is not None:
                        raise ValueError(
                            "new chat session unexpectedly has scope head"
                        )
                    current_scope_documents = None
                else:
                    if current_scope_head is None:
                        logger.error(
                            "既有文件对话缺少活动范围 Head，受理失败关闭: "
                            "conversation_id=%s run_id=%s",
                            normalized_conversation_id,
                            normalized_run_id,
                        )
                        raise ValueError(
                            "existing chat session is missing active scope"
                        )
                    current_scope_revision = (
                        ChatScopeRepository.get_revision_in_transaction(
                            connection,
                            scope_revision_id=(
                                current_scope_head.scope_revision_id
                            ),
                        )
                    )
                    if (
                        current_scope_revision is None
                        or current_scope_revision.conversation_id
                        != normalized_conversation_id
                    ):
                        raise ValueError(
                            "chat scope head points to invalid revision"
                        )
                    current_scope_documents = (
                        current_scope_revision.members
                    )
                if scope_binding.scope_mode == CHAT_SCOPE_MODE_ARCHITECTURE:
                    architecture_candidates = (
                        document_candidates.architecture_candidates
                    )
                    if architecture_candidates is None:
                        raise ValueError(
                            "architecture selector requires architecture candidates"
                        )
                    scope_decision = decide_chat_architecture_scope(
                        session_created=session_created,
                        requested_architecture_id=(
                            normalized_scope_selector.architecture_id
                        ),
                        bound_architecture_id=scope_binding.architecture_id,
                        architecture_candidates=architecture_candidates,
                        current_scope_documents=current_scope_documents,
                    )
                    if not session_created:
                        if (
                            current_scope_revision is None
                            or current_scope_revision.source_architecture_id
                            != scope_binding.architecture_id
                        ):
                            raise ValueError(
                                "architecture scope revision does not match binding"
                            )
                else:
                    scope_decision = decide_chat_document_scope(
                        session_created=session_created,
                        requested_documents=(
                            document_candidates.explicit_documents
                        ),
                        automatic_initial_documents=(
                            document_candidates.new_session_default_documents
                        ),
                        current_scope_documents=current_scope_documents,
                    )
                selection_mode = scope_decision.selection_mode
                history_user_files = tuple(
                    (
                        requested.file_name,
                        requested.original_name,
                    )
                    for requested in scope_decision.requested_files
                )
                effective_input_documents = tuple(
                    document.to_input_tuple()
                    for document in scope_decision.effective_documents
                )
            effective_count = len(effective_input_documents)
            if (
                max_files_per_request is not None
                and effective_count > max_files_per_request
            ):
                logger.warning(
                    "文件对话运行受理被拒绝：有效文件数量超过上限: "
                    "conversation_id=%s run_id=%s selection_mode=%s "
                    "session_created=%s explicit_candidate_count=%d "
                    "default_candidate_count=%d effective_file_count=%d limit=%d",
                    normalized_conversation_id,
                    normalized_run_id,
                    selection_mode,
                    session_created,
                    explicit_candidate_count,
                    default_candidate_count,
                    effective_count,
                    max_files_per_request,
                )
                # 当前事务同时持有首次 session 插入；抛出异常会由连接上下文统一回滚，
                # 不得留下只有 session 而没有 run/input/message 的半成品事实。
                raise ValueError("fileNames超过文件对话数量上限")

            logger.info(
                "文件对话受理事务已选择有效文档: conversation_id=%s run_id=%s "
                "scope_mode=%s requested_architecture_id=%s "
                "bound_architecture_id=%s architecture_candidate_outcome=%s "
                "selection_mode=%s "
                "session_created=%s explicit_candidate_count=%d "
                "default_candidate_count=%d effective_file_count=%d",
                normalized_conversation_id,
                normalized_run_id,
                scope_binding.scope_mode,
                normalized_scope_selector.architecture_id,
                scope_binding.architecture_id,
                (
                    document_candidates.architecture_candidates.resolution_outcome
                    if (
                        document_candidates is not None
                        and document_candidates.architecture_candidates is not None
                    )
                    else ""
                ),
                selection_mode,
                session_created,
                explicit_candidate_count,
                default_candidate_count,
                effective_count,
            )
            connection.execute(
                """
                INSERT INTO chat_runs (
                    run_id, conversation_id, status, abort_requested,
                    owner_instance_id, heartbeat_at, error_message,
                    created_at, started_at, completed_at, updated_at
                ) VALUES (?, ?, ?, 0, ?, NULL, '', ?, NULL, NULL, ?)
                """,
                (
                    normalized_run_id,
                    normalized_conversation_id,
                    RUN_ACCEPTED,
                    self.owner_instance_id,
                    now,
                    now,
                ),
            )
            # 受理流程刻意停在 ``accepted``。同步执行器会在打开远端资源前立即领取运行；
            # 未来工作进程也会使用真实租约和围栏令牌执行相同的条件迁移。这样可避免从未
            # 被消费的 HTTP 响应被记录为正在运行的模型调用。
            row = connection.execute(
                "SELECT * FROM chat_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if row is None:
                raise ValueError("chat_run was not created")
            if scope_decision is not None:
                if scope_decision.creates_scope_revision:
                    revision = ChatScopeRevision(
                        scope_revision_id=chat_scope_revision_id_for_run(
                            normalized_run_id
                        ),
                        conversation_id=normalized_conversation_id,
                        source_mode=scope_decision.scope_source_mode,
                        source_run_id=normalized_run_id,
                        members=scope_decision.effective_documents,
                        created_at=now,
                        source_architecture_id=(
                            scope_decision.source_architecture_id
                        ),
                    )
                    ChatScopeRepository.append_and_set_head_in_transaction(
                        connection,
                        revision=revision,
                        expected_current_revision_id=(
                            None
                            if current_scope_head is None
                            else current_scope_head.scope_revision_id
                        ),
                    )
                    effective_scope_revision_id = (
                        revision.scope_revision_id
                    )
                else:
                    if current_scope_head is None:
                        raise ValueError(
                            "active scope reuse requires current scope head"
                        )
                    effective_scope_revision_id = (
                        current_scope_head.scope_revision_id
                    )
            if user_message is not None:
                self._append_run_input(
                    connection,
                    run_id=normalized_run_id,
                    message=user_message,
                    documents=(
                        ()
                        if effective_scope_revision_id
                        else effective_input_documents
                    ),
                    requested_files=history_user_files,
                    effective_scope_revision_id=(
                        effective_scope_revision_id
                    ),
                    selection_mode=selection_mode,
                    requested_architecture_id=(
                        scope_decision.requested_architecture_id
                        if scope_decision is not None
                        else None
                    ),
                    created_at=now,
                )
                self._append_user_pending(
                    connection,
                    conversation_id=normalized_conversation_id,
                    run_id=normalized_run_id,
                    message=user_message,
                    files=history_user_files,
                    architecture_id=scope_binding.architecture_id,
                    created_at=now,
                )
            if admission_lease is not None:
                consumed = connection.execute(
                    """
                    DELETE FROM conversation_admissions
                    WHERE identity_key = ? AND admission_token = ?
                      AND owner_instance_id = ?
                    """,
                    (
                        admission_lease.identity.identity_key,
                        admission_lease.admission_token,
                        admission_lease.owner_instance_id,
                    ),
                )
                if consumed.rowcount != 1:
                    raise ValueError(
                        "chat admission guard was changed before acceptance"
                    )
            logger.info(
                "文件对话运行锁获取成功: conversation_id=%s run_id=%s "
                "selection_mode=%s session_created=%s "
                "requested_file_count=%d effective_file_count=%d "
                "scope_revision_id=%s has_owner_instance=%s",
                normalized_conversation_id,
                normalized_run_id,
                selection_mode,
                session_created,
                len(history_user_files),
                effective_count,
                effective_scope_revision_id,
                bool(self.owner_instance_id),
            )
            return self._row(row)

    def reserve_chat_admission(
        self,
        *,
        identity: ConversationIdentity,
        scope_selector: ChatScopeSelector,
    ) -> ChatAdmissionLease:
        """在不创建 Conversation/Scope/run 的前提下取得同一公开身份准入权。"""
        normalized_identity = SQLiteConversationIdentityRepository._require_identity(
            identity
        )
        if not isinstance(scope_selector, ChatScopeSelector):
            raise TypeError("scope_selector must be ChatScopeSelector")
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires_at = (
            now_dt + timedelta(seconds=DEFAULT_CHAT_ADMISSION_SECONDS)
        ).isoformat()
        admission_token = uuid.uuid4().hex
        active_statuses = tuple(sorted(RUN_ACTIVE_STATUSES))

        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            # 崩溃遗留的 Guard 只是一条临时协调事实；按过期时间回收不会改变任何
            # Session、Scope、run 或消息。
            expired = connection.execute(
                "DELETE FROM conversation_admissions WHERE expires_at <= ?",
                (now,),
            )
            if expired.rowcount:
                logger.warning(
                    "已回收过期文件对话准入 Guard: expired_count=%d",
                    expired.rowcount,
                )

            identity_row = SQLiteConversationIdentityRepository._select_identity(
                connection,
                normalized_identity,
            )
            resolution = (
                None
                if identity_row is None
                else SQLiteConversationIdentityRepository._resolution_from_row(
                    identity_row
                )
            )
            normalized_conversation_id = ""
            if resolution is not None and resolution.binding.active:
                normalized_conversation_id = resolution.conversation_id
                if resolution.session.status != SESSION_ACTIVE:
                    raise ChatSessionUnavailableError(
                        conversation_id=normalized_conversation_id,
                        status=resolution.session.status,
                    )
                existing_binding = (
                    ChatSessionScopeBindingRepository.get_in_transaction(
                        connection,
                        conversation_id=normalized_conversation_id,
                    )
                )
                if existing_binding is None:
                    raise ValueError(
                        "existing chat session is missing immutable scope binding"
                    )
                if self._has_inflight_title_generation(
                    connection=connection,
                    conversation_id=normalized_conversation_id,
                ):
                    raise ChatSessionUnavailableError(
                        conversation_id=normalized_conversation_id,
                        status="title_active",
                    )
                # 这里只校验不可变模式和 ID，不创建新 Binding。
                decide_chat_session_scope_binding(
                    conversation_id=normalized_conversation_id,
                    selector=scope_selector,
                    existing_binding=existing_binding,
                    created_at=now,
                )

                self._expire_stale_runs(
                    connection,
                    conversation_id=normalized_conversation_id,
                    now=now,
                    active_statuses=active_statuses,
                )
                active_row = connection.execute(
                    f"""
                    SELECT run_id
                    FROM chat_runs
                    WHERE conversation_id = ?
                      AND status IN ({",".join("?" for _ in active_statuses)})
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (normalized_conversation_id, *active_statuses),
                ).fetchone()
                if active_row is not None:
                    raise ChatRunBusyError(
                        conversation_id=normalized_conversation_id,
                        active_run_id=active_row["run_id"],
                    )

            public_fields = SQLiteConversationIdentityRepository._identity_fields(
                normalized_identity
            )
            try:
                connection.execute(
                    """
                    INSERT INTO conversation_admissions (
                        identity_key, identity_kind, chat_id, user_id,
                        architecture_id, admission_token, owner_instance_id,
                        scope_mode, requested_scope_architecture_id,
                        expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_identity.identity_key,
                        normalized_identity.identity_kind,
                        public_fields[0],
                        public_fields[1],
                        public_fields[2],
                        admission_token,
                        self.owner_instance_id,
                        scope_selector.scope_mode,
                        scope_selector.architecture_id,
                        expires_at,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                logger.info(
                    "Chat 准入 Guard 竞争失败: identity_kind=%s",
                    normalized_identity.identity_kind,
                )
                raise ChatAdmissionBusyError(
                    identity_kind=normalized_identity.identity_kind,
                    conversation_id=normalized_conversation_id,
                ) from exc

        logger.info(
            "Chat 准入 Guard 已创建: identity_kind=%s scope_mode=%s "
            "requested_architecture_id=%s expires_at=%s",
            normalized_identity.identity_kind,
            scope_selector.scope_mode,
            scope_selector.architecture_id,
            expires_at,
        )
        return ChatAdmissionLease(
            identity=normalized_identity,
            conversation_id=normalized_conversation_id,
            owner_instance_id=self.owner_instance_id,
            admission_token=admission_token,
            scope_mode=scope_selector.scope_mode,
            architecture_id=scope_selector.architecture_id,
            expires_at=expires_at,
        )

    def release_chat_admission(self, *, lease: ChatAdmissionLease) -> None:
        """按 token 幂等释放 Guard，禁止旧请求删除后来创建的新 Guard。"""
        if not isinstance(lease, ChatAdmissionLease):
            raise TypeError("lease must be ChatAdmissionLease")
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                """
                DELETE FROM conversation_admissions
                WHERE identity_key = ? AND admission_token = ?
                  AND owner_instance_id = ?
                """,
                (
                    lease.identity.identity_key,
                    lease.admission_token,
                    lease.owner_instance_id,
                ),
            )
        if deleted.rowcount:
            logger.info(
                "Chat 准入 Guard 已释放: identity_kind=%s",
                lease.identity.identity_kind,
            )

    def _require_admission_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        lease: ChatAdmissionLease,
        scope_selector: ChatScopeSelector,
        now: str,
    ) -> None:
        """在最终受理事务内验证 Guard 仍归当前请求所有且尚未过期。"""
        row = connection.execute(
            """
            SELECT *
            FROM conversation_admissions
            WHERE identity_key = ? AND admission_token = ?
            """,
            (lease.identity.identity_key, lease.admission_token),
        ).fetchone()
        if row is None:
            raise ValueError("chat admission guard is missing")
        if row["expires_at"] <= now:
            raise ValueError("chat admission guard has expired")
        if (
            row["owner_instance_id"] != lease.owner_instance_id
            or row["scope_mode"] != scope_selector.scope_mode
            or row["requested_scope_architecture_id"]
            != scope_selector.architecture_id
        ):
            raise ValueError("chat admission guard identity is inconsistent")

    @staticmethod
    def _validate_admission_lease_shape(
        *,
        lease: ChatAdmissionLease,
        identity: ConversationIdentity,
        scope_selector: ChatScopeSelector,
    ) -> None:
        if not isinstance(lease, ChatAdmissionLease):
            raise TypeError("admission_lease must be ChatAdmissionLease or None")
        if lease.identity != identity:
            raise ValueError("admission_lease belongs to another identity")
        if (
            lease.scope_mode != scope_selector.scope_mode
            or lease.architecture_id != scope_selector.architecture_id
        ):
            raise ValueError("admission_lease selector does not match request")

    @staticmethod
    def _normalize_scope_selector(
        *,
        scope_selector: ChatScopeSelector | None,
        document_candidates: ChatDocumentSelectionCandidates | None,
        user_files: tuple[tuple[str, str], ...],
        input_documents: tuple[tuple[str, str, str, str, str], ...],
    ) -> ChatScopeSelector:
        """规范内部选择器，并为既有 file 调用构造等价 Binding 输入。

        architecture 路径禁止推断 ID，必须由已经通过 Web 规范化的显式选择器提供；
        兼容的 file 路径则可从现有不可变候选恢复文件名，从而不迫使旧内部调用方
        一次性修改方法签名。
        """
        if scope_selector is not None and not isinstance(
            scope_selector,
            ChatScopeSelector,
        ):
            raise TypeError("scope_selector must be ChatScopeSelector or None")
        architecture_candidates = (
            None
            if document_candidates is None
            else document_candidates.architecture_candidates
        )
        if scope_selector is None:
            if architecture_candidates is not None:
                raise ValueError(
                    "architecture candidates require an explicit scope selector"
                )
            if document_candidates is not None:
                inferred_file_names = tuple(
                    document.file_name
                    for document in document_candidates.explicit_documents
                )
            elif input_documents:
                inferred_file_names = tuple(
                    file_name for file_name, _, _, _, _ in input_documents
                )
            else:
                inferred_file_names = tuple(
                    file_name for file_name, _ in user_files
                )
            return ChatScopeSelector.for_files(inferred_file_names)

        if scope_selector.scope_mode == CHAT_SCOPE_MODE_ARCHITECTURE:
            if architecture_candidates is None:
                raise ValueError(
                    "architecture selector requires architecture candidates"
                )
            if (
                architecture_candidates.architecture_id
                != scope_selector.architecture_id
            ):
                raise ValueError(
                    "architecture selector and candidates ID do not match"
                )
            return scope_selector

        if architecture_candidates is not None:
            raise ValueError(
                "file selector cannot contain architecture candidates"
            )
        if document_candidates is not None:
            explicit_file_names = tuple(
                document.file_name
                for document in document_candidates.explicit_documents
            )
            if explicit_file_names != scope_selector.file_names:
                raise ValueError(
                    "file selector does not match explicit document candidates"
                )
        return scope_selector

    def begin_chat_deletion(self, *, conversation_id: str) -> None:
        """原子阻止新运行，并在存在活动运行时拒绝删除。"""
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        logger.info("开始进入文件对话删除准入临界区: conversation_id=%s", normalized_conversation_id)
        active_statuses = tuple(sorted(RUN_ACTIVE_STATUSES))
        now = _utc_now_iso()
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT status FROM conversations WHERE conversation_id = ?",
                (normalized_conversation_id,),
            ).fetchone()
            if session is None:
                logger.warning("文件对话删除准入失败：会话不存在: conversation_id=%s", normalized_conversation_id)
                raise ValueError("chat_session 不存在")
            status = session["status"]
            if status == SESSION_DELETED:
                logger.info("文件对话已删除，无需再次进入删除状态: conversation_id=%s", normalized_conversation_id)
                return
            if status == SESSION_DELETING:
                logger.warning("文件对话删除准入被拒绝：会话已处于删除中: conversation_id=%s", normalized_conversation_id)
                raise ChatSessionDeleteBusyError(
                    conversation_id=normalized_conversation_id,
                    reason="当前对话正在删除",
                )
            self._expire_stale_runs(
                connection,
                conversation_id=normalized_conversation_id,
                now=now,
                active_statuses=active_statuses,
            )
            active = connection.execute(
                f"""
                SELECT run_id FROM chat_runs
                WHERE conversation_id = ? AND status IN ({",".join("?" for _ in active_statuses)})
                LIMIT 1
                """,
                (normalized_conversation_id, *active_statuses),
            ).fetchone()
            if active is not None:
                logger.warning(
                    "文件对话删除准入被拒绝：存在活动运行: conversation_id=%s active_run_id=%s",
                    normalized_conversation_id,
                    active["run_id"],
                )
                raise ChatSessionDeleteBusyError(
                    conversation_id=normalized_conversation_id,
                    reason="当前对话存在进行中的流式响应，请先中断后删除",
                )
            if self._has_inflight_title_generation(
                connection=connection,
                conversation_id=normalized_conversation_id,
            ):
                logger.warning(
                    "文件对话删除准入被拒绝：标题临时线程仍在生成: conversation_id=%s",
                    normalized_conversation_id,
                )
                raise ChatSessionDeleteBusyError(
                    conversation_id=normalized_conversation_id,
                    reason="当前对话正在生成标题，请稍后重试",
                )
            if status not in {SESSION_ACTIVE, SESSION_ERROR}:
                logger.warning(
                    "文件对话删除准入被拒绝：会话状态不允许删除: conversation_id=%s status=%s",
                    normalized_conversation_id,
                    status,
                )
                raise ChatSessionUnavailableError(
                    conversation_id=normalized_conversation_id,
                    status=status,
                )
            cursor = connection.execute(
                """
                UPDATE conversations
                SET status = ?, updated_at = ?
                WHERE conversation_id = ? AND status = ?
                """,
                (SESSION_DELETING, now, normalized_conversation_id, status),
            )
            if cursor.rowcount != 1:
                raise ValueError("chat_session status was changed concurrently")
            logger.info("文件对话已进入删除中状态: conversation_id=%s", normalized_conversation_id)

    def complete_run_with_messages(
        self,
        *,
        run_id: str,
        user_message_id: str,
        assistant_message_id: str,
        assistant_content: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """兼容无来源的文件对话成功提交入口。"""
        return self.complete_run_with_reply(
            run_id=run_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            assistant_content=assistant_content,
            source_chunks=(),
            terminal_event=terminal_event,
        )

    def complete_run_with_reply(
        self,
        *,
        run_id: str,
        user_message_id: str,
        assistant_message_id: str,
        assistant_content: str,
        source_chunks: tuple[ChatMessageSourceChunk, ...] = (),
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """原子提交用户、assistant、全部来源 Chunk、事件和成功终态。"""
        return self._finish_run_with_user(
            run_id=run_id,
            user_message_id=user_message_id,
            terminal_status=RUN_SUCCEEDED,
            error_message="",
            assistant_message_id=assistant_message_id,
            assistant_content=assistant_content,
            source_chunks=source_chunks,
            terminal_event=terminal_event,
        )

    def fail_run_with_user(
        self,
        *,
        run_id: str,
        user_message_id: str,
        error_message: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        return self._finish_run_with_user(
            run_id=run_id,
            user_message_id=user_message_id,
            terminal_status=RUN_FAILED,
            error_message=_required_text(error_message, name="error_message"),
            terminal_event=terminal_event,
        )

    def abort_run_with_user(
        self,
        *,
        run_id: str,
        user_message_id: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        return self._finish_run_with_user(
            run_id=run_id,
            user_message_id=user_message_id,
            terminal_status=RUN_ABORTED,
            error_message="",
            terminal_event=terminal_event,
        )

    def complete_run(self, run_id: str) -> ChatRun:
        return self._runs.update_status(run_id=run_id, status=RUN_SUCCEEDED)

    def fail_run(self, run_id: str, *, error_message: str) -> ChatRun:
        return self._runs.update_status(
            run_id=run_id,
            status=RUN_FAILED,
            error_message=_required_text(error_message, name="error_message"),
        )

    def discard_unstarted_run(
        self,
        *,
        run_id: str,
        error_message: str,
    ) -> ChatRun:
        """将已受理但从未执行的运行标记失败，且不暴露其输入。

        浏览器可能在 HTTP 处理器受理请求后、Flask 开始迭代 SSE 生成器前断开连接。
        此时尚未发生模型请求，因此既定历史策略是丢弃待处理用户消息，而不是展示没有
        回答的一轮对话。
        """
        normalized_run_id = _required_text(run_id, name="run_id")
        normalized_error = _required_text(error_message, name="error_message")
        logger.info(
            "开始收敛未启动的文件对话运行: run_id=%s error_chars=%d",
            normalized_run_id,
            len(normalized_error),
        )
        now = _utc_now_iso()
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._get_run_with_connection(connection, normalized_run_id)
            if run.status in {RUN_SUCCEEDED, RUN_FAILED, RUN_ABORTED}:
                return run
            if run.status != RUN_ACCEPTED:
                raise ChatRunLeaseLostError(
                    run_id=normalized_run_id,
                    reason=f"run has already started: {run.status}",
                )
            connection.execute(
                """
                UPDATE chat_messages
                SET status = ?
                WHERE run_id = ? AND status = ?
                """,
                (MESSAGE_DISCARDED, normalized_run_id, MESSAGE_PENDING),
            )
            cursor = connection.execute(
                """
                UPDATE chat_runs
                SET status = ?, error_message = ?, completed_at = ?, updated_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    RUN_FAILED,
                    normalized_error,
                    now,
                    now,
                    normalized_run_id,
                    RUN_ACCEPTED,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("accepted chat_run was changed concurrently")
            self._append_internal_failure_event_if_missing(
                connection=connection,
                run_id=normalized_run_id,
                error_message=normalized_error,
            )
            settled = self._get_run_with_connection(connection, normalized_run_id)
            logger.info(
                "未启动的文件对话运行已收敛: conversation_id=%s run_id=%s status=%s",
                settled.conversation_id,
                settled.run_id,
                settled.status,
            )
            return settled

    def abort_run(self, run_id: str) -> ChatRun:
        return self._runs.mark_aborted(run_id)

    def request_abort(self, run_id: str) -> ChatRun:
        normalized_run_id = _required_text(run_id, name="run_id")
        logger.debug("开始写入文件对话中断标记: run_id=%s", normalized_run_id)
        try:
            run = self._runs.request_abort(normalized_run_id)
            logger.info(
                "文件对话中断标记已在 SQLite 中写入: conversation_id=%s run_id=%s status=%s",
                run.conversation_id,
                run.run_id,
                run.status,
            )
            return run
        except ValueError as exc:
            current = self._runs.get(normalized_run_id)
            if current is not None and current.status not in RUN_ACTIVE_STATUSES:
                logger.info(
                    "文件对话中断标记未写入：运行已进入终态: run_id=%s status=%s",
                    normalized_run_id,
                    current.status,
                )
                raise ChatRunInactiveError(
                    run_id=normalized_run_id,
                    status=current.status,
                ) from exc
            raise

    def issue_execution_lease(self, *, run_id: str) -> ChatRunLease:
        """领取一条已受理运行，并签发其内部执行租约。

        领取操作是 ``accepted`` 变为 ``running`` 的唯一位置。未来共享持久化适配器
        会在该条件更新中加入租约与围栏条件；当前适配器保持相同生命周期边界，且不假装
        支持围栏能力。
        """
        normalized_run_id = _required_text(run_id, name="run_id")
        logger.debug("开始在 SQLite 中领取文件对话运行执行权: run_id=%s", normalized_run_id)
        now = _utc_now_iso()
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._get_run_with_connection(connection, normalized_run_id)
            if run.owner_instance_id != self.owner_instance_id:
                logger.warning(
                    "文件对话运行执行权领取失败：运行属于其他实例: "
                    "run_id=%s owner_matches=%s",
                    normalized_run_id,
                    False,
                )
                raise ChatRunLeaseLostError(
                    run_id=normalized_run_id,
                    reason="run is owned by another instance",
                )
            if run.status != RUN_ACCEPTED:
                logger.warning(
                    "文件对话运行执行权领取失败：运行状态不可领取: run_id=%s status=%s",
                    normalized_run_id,
                    run.status,
                )
                raise ChatRunLeaseLostError(
                    run_id=normalized_run_id,
                    reason=f"run cannot be claimed from status: {run.status}",
                )
            cursor = connection.execute(
                """
                UPDATE chat_runs
                SET status = ?, heartbeat_at = ?, started_at = ?, updated_at = ?
                WHERE run_id = ? AND status = ? AND owner_instance_id = ?
                """,
                (
                    RUN_RUNNING,
                    now,
                    now,
                    now,
                    normalized_run_id,
                    RUN_ACCEPTED,
                    self.owner_instance_id,
                ),
            )
            if cursor.rowcount != 1:
                logger.warning(
                    "文件对话运行执行权领取失败：状态在领取期间发生变化: run_id=%s",
                    normalized_run_id,
                )
                raise ChatRunLeaseLostError(
                    run_id=normalized_run_id,
                    reason="run changed before it could be claimed",
                )
            run = self._get_run_with_connection(connection, normalized_run_id)
        logger.info(
            "文件对话运行执行权已在 SQLite 中领取: conversation_id=%s run_id=%s "
            "has_owner_instance=%s",
            run.conversation_id,
            run.run_id,
            bool(run.owner_instance_id),
        )
        return ChatRunLease(
            run_id=run.run_id,
            conversation_id=run.conversation_id,
            owner_instance_id=run.owner_instance_id,
        )

    def validate_execution_lease(self, *, lease: ChatRunLease) -> ChatRun:
        """校验一个内部执行租约仍对应本实例的活动运行。

        当前校验与后续写入之间无法构成跨实例围栏原子条件，因此只可用于单实例模式。
        未来适配器必须把租约与围栏条件合并到心跳和终态更新中。
        """
        if not isinstance(lease, ChatRunLease):
            raise TypeError("lease must be ChatRunLease")
        run = self._runs.get(lease.run_id)
        if run is None:
            raise ChatRunLeaseLostError(
                run_id=lease.run_id,
                reason="run does not exist",
            )
        if run.conversation_id != lease.conversation_id:
            raise ChatRunLeaseLostError(
                run_id=lease.run_id,
                reason="conversation_id does not match",
            )
        if run.owner_instance_id != lease.owner_instance_id:
            raise ChatRunLeaseLostError(
                run_id=lease.run_id,
                reason="owner_instance_id does not match",
            )
        if run.owner_instance_id != self.owner_instance_id:
            raise ChatRunLeaseLostError(
                run_id=lease.run_id,
                reason="run is owned by another instance",
            )
        if run.status not in RUN_ACTIVE_STATUSES:
            raise ChatRunLeaseLostError(
                run_id=lease.run_id,
                reason=f"run is no longer active: {run.status}",
            )
        return run

    def heartbeat_execution_lease(self, *, lease: ChatRunLease) -> ChatRun:
        """通过执行租约刷新心跳，保留未来围栏条件更新的稳定签名。"""
        self.validate_execution_lease(lease=lease)
        return self.heartbeat_run(lease.run_id)

    def complete_run_with_execution_lease(
        self,
        *,
        lease: ChatRunLease,
        user_message_id: str,
        assistant_message_id: str,
        assistant_content: str,
        source_chunks: tuple[ChatMessageSourceChunk, ...] = (),
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """通过执行租约提交成功终态及完整助手消息。"""
        self.validate_execution_lease(lease=lease)
        return self.complete_run_with_reply(
            run_id=lease.run_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            assistant_content=assistant_content,
            source_chunks=source_chunks,
            terminal_event=terminal_event,
        )

    def fail_run_with_execution_lease(
        self,
        *,
        lease: ChatRunLease,
        user_message_id: str,
        error_message: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """通过执行租约提交失败终态，并按既有规则保留用户消息。"""
        self.validate_execution_lease(lease=lease)
        return self.fail_run_with_user(
            run_id=lease.run_id,
            user_message_id=user_message_id,
            error_message=error_message,
            terminal_event=terminal_event,
        )

    def abort_run_with_execution_lease(
        self,
        *,
        lease: ChatRunLease,
        user_message_id: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """通过执行租约提交中断终态，并丢弃未完成助手输出。"""
        self.validate_execution_lease(lease=lease)
        return self.abort_run_with_user(
            run_id=lease.run_id,
            user_message_id=user_message_id,
            terminal_event=terminal_event,
        )

    def expire_stale_runs_for_chat(self, *, conversation_id: str) -> tuple[ChatRun, ...]:
        """将一个对话中的过期活动运行标记为失败并返回它们。

        `/llm/chat` 会在领取新锁前使过期运行失效。中断操作在报告活动流可中断前也必须
        使用同一规则；否则已崩溃工作进程会被标记为已中断，但已不存在执行器读取中断标记。
        """
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        if self.stale_after_seconds is None:
            return ()
        now = _utc_now_iso()
        active_statuses = tuple(sorted(RUN_ACTIVE_STATUSES))
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            stale_run_ids = self._expire_stale_runs(
                connection,
                conversation_id=normalized_conversation_id,
                now=now,
                active_statuses=active_statuses,
            )
            if not stale_run_ids:
                return ()
            placeholders = ",".join("?" for _ in stale_run_ids)
            rows = connection.execute(
                f"""
                SELECT * FROM chat_runs
                WHERE run_id IN ({placeholders})
                ORDER BY created_at ASC
                """,
                stale_run_ids,
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def heartbeat_run(self, run_id: str) -> ChatRun:
        normalized_run_id = _required_text(run_id, name="run_id")
        now = _utc_now_iso()
        active_statuses = tuple(sorted(RUN_ACTIVE_STATUSES))
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM chat_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if current is None:
                raise ValueError("chat_run 不存在")
            if current["status"] not in RUN_ACTIVE_STATUSES:
                logger.debug(
                    "跳过非活跃文件对话运行的心跳刷新: run_id=%s status=%s",
                    normalized_run_id,
                    current["status"],
                )
                return self._row(current)
            cursor = connection.execute(
                f"""
                UPDATE chat_runs
                SET heartbeat_at = ?, updated_at = ?
                WHERE run_id = ? AND status IN ({",".join("?" for _ in active_statuses)})
                """,
                (now, now, normalized_run_id, *active_statuses),
            )
            if cursor.rowcount != 1:
                raise ValueError("chat_run status was changed concurrently")
            row = connection.execute(
                "SELECT * FROM chat_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if row is None:
                raise ValueError("chat_run 不存在")
            logger.debug(
                "文件对话运行心跳写入成功: run_id=%s status=%s",
                normalized_run_id,
                row["status"],
            )
            return self._row(row)

    def _expire_stale_runs(
        self,
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
        now: str,
        active_statuses: tuple[str, ...],
    ) -> tuple[str, ...]:
        if self.stale_after_seconds is None:
            return ()
        now_dt = _parse_utc(now)
        if now_dt is None:
            return ()
        rows = connection.execute(
            f"""
            SELECT * FROM chat_runs
            WHERE conversation_id = ? AND status IN ({",".join("?" for _ in active_statuses)})
            """,
            (conversation_id, *active_statuses),
        ).fetchall()
        stale_run_ids: list[str] = []
        for row in rows:
            last_seen = (
                _parse_utc(row["heartbeat_at"])
                or _parse_utc(row["updated_at"])
                or _parse_utc(row["created_at"])
            )
            if last_seen is None:
                continue
            age_seconds = (now_dt - last_seen).total_seconds()
            if age_seconds > self.stale_after_seconds:
                stale_run_ids.append(row["run_id"])

        for stale_run_id in stale_run_ids:
            stale_row = connection.execute(
                "SELECT status FROM chat_runs WHERE run_id = ?",
                (stale_run_id,),
            ).fetchone()
            if stale_row is None:
                continue
            stale_status = stale_row["status"]
            logger.warning(
                "文件对话运行心跳超时，已标记失败并释放锁: "
                "conversation_id=%s run_id=%s stale_after_seconds=%s",
                conversation_id,
                stale_run_id,
                self.stale_after_seconds,
            )
            connection.execute(
                f"""
                UPDATE chat_runs
                SET status = ?,
                    error_message = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE run_id = ? AND status IN ({",".join("?" for _ in active_statuses)})
                """,
                (
                    RUN_FAILED,
                    "chat run heartbeat expired",
                    now,
                    now,
                    stale_run_id,
                    *active_statuses,
                ),
            )
            # 运行中状态已到达模型执行边界，因此恢复后其用户轮次仍应可见。已受理状态
            # 从未开始执行，应遵循明确的丢弃策略。
            message_status = (
                MESSAGE_COMMITTED
                if stale_status == RUN_RUNNING
                else MESSAGE_DISCARDED
            )
            connection.execute(
                """
                UPDATE chat_messages
                SET status = ?
                WHERE run_id = ? AND status = ?
                """,
                (message_status, stale_run_id, MESSAGE_PENDING),
            )
            self._append_internal_failure_event_if_missing(
                connection=connection,
                run_id=stale_run_id,
                error_message="chat run heartbeat expired",
            )
        return tuple(stale_run_ids)

    @staticmethod
    def _has_inflight_title_generation(
        *,
        connection: sqlite3.Connection,
        conversation_id: str,
    ) -> bool:
        """返回当前是否有标题生成占用临时线程租约。

        标题生成会在调用外部供应商前记录 planned 租约。在进入 ``deleting`` 的同一事务中
        检查该租约，可消除标题生成与上下文删除间原本不可避免的“检查后创建”竞争。
        确定性租约前缀是内部身份契约，绝不属于面向 HTTP 的值。
        """
        rows = connection.execute(
            """
            SELECT lease_id
            FROM chat_resource_leases
            WHERE conversation_id = ?
              AND run_id = ''
              AND resource_type = ?
              AND status IN (?, ?, ?)
            """,
            (
                conversation_id,
                RESOURCE_THREAD,
                LEASE_PLANNED,
                LEASE_ACTIVE,
                LEASE_CLEANUP_PENDING,
            ),
        ).fetchall()
        prefix = f"chat:{conversation_id}:temporary_thread:"
        return any(str(row["lease_id"]).startswith(prefix) for row in rows)

    @staticmethod
    def _append_internal_failure_event_if_missing(
        *,
        connection: sqlite3.Connection,
        run_id: str,
        error_message: str,
    ) -> None:
        """追加一条恢复用终态事件，且不重复已有事件。"""
        terminal = connection.execute(
            """
            SELECT 1 FROM chat_run_events
            WHERE run_id = ? AND event_type IN ('done', 'error', 'aborted')
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if terminal is not None:
            return
        ChatRunEventRepository.append_in_transaction(
            connection=connection,
            run_id=run_id,
            event=ChatStreamEvent("error", {"error": error_message}),
        )

    @staticmethod
    def _append_run_input(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        message: str,
        documents: tuple[tuple[str, str, str, str, str], ...],
        created_at: str,
        requested_files: tuple[tuple[str, str], ...] = (),
        effective_scope_revision_id: str = "",
        selection_mode: str = "legacy_input",
        requested_architecture_id: int | None = None,
    ) -> None:
        normalized_message = _required_text(message, name="user_message")
        normalized_documents = tuple(
            {
                "file_name": _required_text(file_name, name="file_name"),
                "original_name": _required_text(
                    original_name,
                    name="original_name",
                ),
                "document_ref": _required_text(
                    document_ref,
                    name="document_ref",
                ),
                "external_location": _optional_text(external_location),
                "structured_source_key": _optional_canonical_text(
                    structured_source_key,
                    name="structured_source_key",
                ),
            }
            for (
                file_name,
                original_name,
                document_ref,
                external_location,
                structured_source_key,
            ) in documents
        )
        if len({item["file_name"] for item in normalized_documents}) != len(
            normalized_documents
        ):
            raise ValueError("input_documents contains duplicate file_name")
        normalized_requested_files = tuple(
            {
                "file_name": _required_text(file_name, name="file_name"),
                "original_name": _required_text(
                    original_name,
                    name="original_name",
                ),
            }
            for file_name, original_name in requested_files
        )
        if len(
            {item["file_name"] for item in normalized_requested_files}
        ) != len(normalized_requested_files):
            raise ValueError("requested_files contains duplicate file_name")
        normalized_scope_revision_id = _optional_text(
            effective_scope_revision_id
        )
        normalized_selection_mode = _required_text(
            selection_mode,
            name="selection_mode",
        )
        if normalized_scope_revision_id and normalized_documents:
            raise ValueError(
                "scope run input cannot duplicate effective documents"
            )
        connection.execute(
            """
            INSERT INTO chat_run_inputs (
                run_id, message, files_json, created_at,
                requested_files_json, effective_scope_revision_id,
                selection_mode, requested_architecture_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                normalized_message,
                json.dumps(
                    normalized_documents,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                created_at,
                json.dumps(
                    normalized_requested_files,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                normalized_scope_revision_id,
                normalized_selection_mode,
                requested_architecture_id,
            ),
        )

    @staticmethod
    def _append_user_pending(
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
        run_id: str,
        message: str,
        files: tuple[tuple[str, str], ...],
        architecture_id: int | None,
        created_at: str,
    ) -> None:
        normalized_message = _required_text(message, name="user_message")
        normalized_files = tuple(
            (_required_text(file_name, name="file_name"), _optional_text(original_name))
            for file_name, original_name in files
        )
        if len({file_name for file_name, _ in normalized_files}) != len(normalized_files):
            raise ValueError("user_files contains duplicate file_name")
        sequence_row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence
            FROM chat_messages WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        message_id = f"{run_id}:user"
        connection.execute(
            """
            INSERT INTO chat_messages (
                message_id, conversation_id, run_id, role, content,
                status, sequence_no, created_at, architecture_id
            ) VALUES (?, ?, ?, 'user', ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                conversation_id,
                run_id,
                normalized_message,
                MESSAGE_PENDING,
                int(sequence_row["next_sequence"]),
                created_at,
                architecture_id,
            ),
        )
        for file_name, original_name in normalized_files:
            connection.execute(
                """
                INSERT INTO chat_message_files (message_id, file_name, original_name)
                VALUES (?, ?, ?)
                """,
                (message_id, file_name, original_name),
            )

    def _finish_run_with_user(
        self,
        *,
        run_id: str,
        user_message_id: str,
        terminal_status: str,
        error_message: str,
        assistant_message_id: str = "",
        assistant_content: str = "",
        source_chunks: tuple[ChatMessageSourceChunk, ...] = (),
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        normalized_run_id = _required_text(run_id, name="run_id")
        normalized_user_message_id = _required_text(
            user_message_id,
            name="user_message_id",
        )
        normalized_terminal_event = self._validate_terminal_event(
            terminal_event=terminal_event,
            terminal_status=terminal_status,
        )
        normalized_source_chunks = tuple(source_chunks)
        if terminal_status != RUN_SUCCEEDED and normalized_source_chunks:
            raise ValueError("source chunks are only valid for a succeeded run")
        if normalized_source_chunks and not assistant_content:
            raise ValueError("source chunks require an assistant message")
        now = _utc_now_iso()
        logger.debug(
            "开始提交文件对话运行终态: run_id=%s terminal_status=%s has_assistant_content=%s has_terminal_event=%s",
            normalized_run_id,
            terminal_status,
            bool(assistant_content),
            normalized_terminal_event is not None,
        )
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._get_run_with_connection(connection, normalized_run_id)
            if run.status not in RUN_ACTIVE_STATUSES:
                if run.status == terminal_status:
                    return run
                raise ChatRunInactiveError(
                    run_id=normalized_run_id,
                    status=run.status,
                )
            if terminal_status == RUN_SUCCEEDED and run.abort_requested:
                # Abort 与完成共享同一 SQLite 写事务。成功提交必须在任何消息状态变更前
                # 观察 abort_requested，确保 Abort 先赢时不会留下 assistant 或 Chunk。
                raise ChatRunInactiveError(
                    run_id=normalized_run_id,
                    status="abort_requested",
                )
            user = connection.execute(
                """
                SELECT status FROM chat_messages
                WHERE message_id = ? AND conversation_id = ? AND run_id = ? AND role = 'user'
                """,
                (normalized_user_message_id, run.conversation_id, normalized_run_id),
            ).fetchone()
            if user is None:
                raise ValueError("chat run user message 不存在")
            if user["status"] == MESSAGE_PENDING:
                cursor = connection.execute(
                    """
                    UPDATE chat_messages SET status = ?
                    WHERE message_id = ? AND status = ?
                    """,
                    (MESSAGE_COMMITTED, normalized_user_message_id, MESSAGE_PENDING),
                )
                if cursor.rowcount != 1:
                    raise ValueError("chat user message was changed concurrently")
            elif user["status"] != MESSAGE_COMMITTED:
                raise ValueError("chat run user message is not committable")
            if terminal_status == RUN_SUCCEEDED and assistant_content:
                existing = connection.execute(
                    "SELECT message_id FROM chat_messages WHERE message_id = ?",
                    (assistant_message_id,),
                ).fetchone()
                if existing is None:
                    sequence_row = connection.execute(
                        """
                        SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence
                        FROM chat_messages WHERE conversation_id = ?
                        """,
                        (run.conversation_id,),
                    ).fetchone()
                    connection.execute(
                        """
                        INSERT INTO chat_messages (
                            message_id, conversation_id, run_id, role, content,
                            status, sequence_no, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _required_text(assistant_message_id, name="assistant_message_id"),
                            run.conversation_id,
                            normalized_run_id,
                            MESSAGE_ROLE_ASSISTANT,
                            assistant_content,
                            MESSAGE_COMMITTED,
                            int(sequence_row["next_sequence"]),
                            now,
                        ),
                    )
                    ChatMessageSourceRepository.append_many_in_transaction(
                        connection=connection,
                        message_id=_required_text(
                            assistant_message_id,
                            name="assistant_message_id",
                        ),
                        chunks=normalized_source_chunks,
                    )
                elif normalized_source_chunks:
                    raise ValueError(
                        "assistant message already exists before source chunk commit"
                    )
            if terminal_status == RUN_SUCCEEDED:
                cursor = connection.execute(
                    """
                    UPDATE chat_runs
                    SET status = ?, error_message = ?, completed_at = ?, updated_at = ?
                    WHERE run_id = ? AND status = ? AND abort_requested = 0
                    """,
                    (
                        terminal_status,
                        _optional_text(error_message),
                        now,
                        now,
                        normalized_run_id,
                        run.status,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE chat_runs
                    SET status = ?, error_message = ?, completed_at = ?, updated_at = ?
                    WHERE run_id = ? AND status = ?
                    """,
                    (
                        terminal_status,
                        _optional_text(error_message),
                        now,
                        now,
                        normalized_run_id,
                        run.status,
                    ),
                )
            if cursor.rowcount != 1:
                raise ValueError("chat_run status was changed concurrently")
            if normalized_terminal_event is not None:
                ChatRunEventRepository.append_in_transaction(
                    connection=connection,
                    run_id=normalized_run_id,
                    event=normalized_terminal_event,
                )
            row = connection.execute(
                "SELECT * FROM chat_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if row is None:
                raise ValueError("chat_run 不存在")
            completed = self._row(row)
            logger.info(
                "文件对话运行终态已写入 SQLite: conversation_id=%s run_id=%s status=%s has_error=%s",
                completed.conversation_id,
                completed.run_id,
                completed.status,
                bool(completed.error_message),
            )
            return completed

    @staticmethod
    def _validate_terminal_event(
        *,
        terminal_event: ChatStreamEvent | None,
        terminal_status: str,
    ) -> ChatStreamEvent | None:
        if terminal_event is None:
            return None
        if not isinstance(terminal_event, ChatStreamEvent):
            raise TypeError("terminal_event must be ChatStreamEvent")
        expected_type = {
            RUN_SUCCEEDED: "done",
            RUN_FAILED: "error",
            RUN_ABORTED: "aborted",
        }.get(terminal_status)
        if expected_type is None:
            raise ValueError("terminal_status does not support a stream event")
        if terminal_event.event_type != expected_type:
            raise ValueError(
                "terminal_event type does not match the requested run terminal status"
            )
        return terminal_event

    @staticmethod
    def _get_run_with_connection(
        connection: sqlite3.Connection,
        run_id: str,
    ) -> ChatRun:
        row = connection.execute(
            "SELECT * FROM chat_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError("chat_run 不存在")
        return ChatRunLockService._row(row)

    @staticmethod
    def _row(row: sqlite3.Row) -> ChatRun:
        return ChatRun(
            run_id=row["run_id"],
            conversation_id=row["conversation_id"],
            status=row["status"],
            abort_requested=bool(row["abort_requested"]),
            owner_instance_id=row["owner_instance_id"],
            heartbeat_at=row["heartbeat_at"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            updated_at=row["updated_at"],
        )


__all__ = [
    "ChatAdmissionBusyError",
    "ChatRunBusyError",
    "ChatRunInactiveError",
    "ChatRunLockService",
    "ChatSessionDeleteBusyError",
    "ChatSessionUnavailableError",
    "DEFAULT_STALE_RUN_SECONDS",
    "DEFAULT_CHAT_ADMISSION_SECONDS",
]
