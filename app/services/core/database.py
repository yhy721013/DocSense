import json
import logging
import sqlite3
import threading
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

class DatabaseService:
    _ACTIVE_REASSIGN_STATUSES = (
        "reserved",
        "running",
        "compensating",
        "recovery_required",
    )

    def __init__(self, db_path: str):
        self.db_path = db_path
        # 同一进程内串行化复合写事务；跨进程冲突仍由 SQLite 事务和唯一约束裁决。
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """创建启用外键校验的独立 SQLite 连接。

        DatabaseService 与各模块 Repository 共用知识库文件，所有连接必须采用相同的外键
        语义；否则一个适配器认为约束有效，另一个适配器却能写出 ``foreign_key_check``
        可见的损坏。连接仍按方法短生命周期使用，不跨线程共享。
        """

        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_db(self):
        """初始化建表，加上 IF NOT EXISTS 不必担心重复创建"""
        with self._lock:
            with self._connect() as conn:
                # architecture 与永久 Workspace 必须保持一对一，任一侧重复映射都应失败。
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS workspaces (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        architecture_id INTEGER NOT NULL UNIQUE,
                        workspace_slug TEXT NOT NULL UNIQUE
                    )
                """)
                # 文档名称只在所属 architecture 内唯一。使用代理主键可以同时保存不同
                # architecture 中的同名文件，避免后写入记录覆盖其他永久知识集合。
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS documents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_name TEXT NOT NULL,
                        original_name TEXT NOT NULL DEFAULT '',
                        ingested_file_name TEXT NOT NULL DEFAULT '',
                        architecture_id INTEGER NOT NULL,
                        anything_doc_id TEXT NOT NULL,
                        doc_path TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        UNIQUE (architecture_id, file_name)
                    )
                """)
                self._ensure_documents_schema(conn)
                conn.commit()
            logger.info("本地数据库初始化完成: db_path=%s", self.db_path)

    def _ensure_documents_schema(self, conn: sqlite3.Connection) -> None:
        """把历史 ``documents`` 表向前迁移到当前结构。

        该方法只在数据库初始化持有写锁时调用，所有 DDL、历史数据回填和表重建都处于
        同一个 SQLite 事务中。迁移必须在任何业务读写发生前完成，不能把修改表结构的
        副作用放进 ``list_document_records()`` 等查询方法，否则首次调用不同接口时会得到
        不一致的数据库契约。
        """
        cursor = conn.execute("PRAGMA table_info(documents)")
        columns = {row[1] for row in cursor.fetchall()}

        if "original_name" not in columns:
            conn.execute("ALTER TABLE documents ADD COLUMN original_name TEXT NOT NULL DEFAULT ''")
            logger.info("已补齐文档表原始文件名字段: db_path=%s", self.db_path)

        if "ingested_file_name" not in columns:
            # 该列保存实际上传 AnythingLLM 的文件名，必须独立于业务哈希名和业务原始名。
            # 历史开发数据不做字符串反推；空值会在读取链路中保持“缺少谱系”的可观察状态。
            conn.execute(
                "ALTER TABLE documents "
                "ADD COLUMN ingested_file_name TEXT NOT NULL DEFAULT ''"
            )
            logger.info("已补齐文档表实际上传文件名字段: db_path=%s", self.db_path)

        if "doc_path" not in columns:
            conn.execute("ALTER TABLE documents ADD COLUMN doc_path TEXT")
            logger.info("已补齐文档表外部文档路径字段: db_path=%s", self.db_path)

        if "metadata_json" not in columns:
            conn.execute(
                "ALTER TABLE documents "
                "ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
            logger.info("已补齐文档表元数据字段: db_path=%s", self.db_path)

        conn.execute(
            """
            UPDATE documents
            SET original_name = file_name
            WHERE original_name IS NULL OR original_name = ''
            """
        )

        if not self._has_document_identity_constraint(conn):
            self._migrate_documents_identity_constraint(conn)

    @staticmethod
    def _has_document_identity_constraint(conn: sqlite3.Connection) -> bool:
        """判断文档表是否具备 ``(architecture_id, file_name)`` 唯一约束。

        不能仅判断 ``file_name`` 是否仍为主键：某些中间版本可能已经移除了旧主键，
        却尚未建立新的复合唯一约束。逐个检查 SQLite 唯一索引可以覆盖建表约束和显式
        唯一索引两种实现，确保后续 UPSERT 的冲突目标确实存在。
        """
        for index_row in conn.execute("PRAGMA index_list(documents)").fetchall():
            # PRAGMA index_list 的第三列表示该索引是否唯一。普通查询索引不能作为
            # ON CONFLICT(architecture_id, file_name) 的冲突目标。
            if not bool(index_row[2]):
                continue
            index_name = str(index_row[1]).replace('"', '""')
            index_columns = [
                str(column_row[2])
                for column_row in conn.execute(
                    f'PRAGMA index_info("{index_name}")'
                ).fetchall()
            ]
            if index_columns == ["architecture_id", "file_name"]:
                return True
        return False

    def _migrate_documents_identity_constraint(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        """重建文档表，将全局文件名唯一规则改为分类内唯一规则。

        SQLite 不能原地修改主键或表级唯一约束，因此需要在当前初始化事务中重命名旧表、
        创建目标表并复制数据。任何一步失败都会使初始化失败并回滚，避免应用在结构不完整
        的数据库上继续运行。复制时若发现历史数据违反新约束，也应明确失败，不能静默丢行。
        """
        reassign_table = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'reassign_operations'
            """
        ).fetchone()
        if reassign_table is not None:
            # ``reassign_operations.document_row_id`` 冻结 documents.id。SQLite 重命名
            # documents 时还可能同步改写外键目标；在没有协调迁移 Operation/Step/Event
            # 前继续重建会令历史恢复事实指向错误行或不存在的旧表，因此必须启动失败。
            raise RuntimeError(
                "检测到 reassign 事实表，禁止单独重建 documents；"
                "请执行保持 documents.id 的协调数据库迁移"
            )
        legacy_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        legacy_has_id = "id" in legacy_columns
        legacy_table = "documents_legacy_identity"
        conn.execute(f"ALTER TABLE documents RENAME TO {legacy_table}")
        conn.execute(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT NOT NULL,
                original_name TEXT NOT NULL DEFAULT '',
                ingested_file_name TEXT NOT NULL DEFAULT '',
                architecture_id INTEGER NOT NULL,
                anything_doc_id TEXT NOT NULL,
                doc_path TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE (architecture_id, file_name)
            )
            """
        )
        if legacy_has_id:
            # 已存在代理主键时必须原样保留，否则 reassign 冻结的 document_row_id
            # 会在任何后续协调迁移中悄然指向另一份文档。
            conn.execute(
                f"""
                INSERT INTO documents (
                    id, file_name, original_name, ingested_file_name,
                    architecture_id, anything_doc_id, doc_path, metadata_json
                )
                SELECT id, file_name, original_name, ingested_file_name,
                       architecture_id, anything_doc_id, doc_path, metadata_json
                FROM {legacy_table}
                """
            )
        else:
            # 最早期旧表没有 id，此次迁移是代理主键的首次建立，不存在需要保持的
            # reassign 引用；按复制顺序由 SQLite 生成新 ID。
            conn.execute(
                f"""
                INSERT INTO documents (
                    file_name, original_name, ingested_file_name, architecture_id,
                    anything_doc_id, doc_path, metadata_json
                )
                SELECT file_name, original_name, ingested_file_name, architecture_id,
                       anything_doc_id, doc_path, metadata_json
                FROM {legacy_table}
                """
            )
        conn.execute(f"DROP TABLE {legacy_table}")
        logger.info(
            "文档表唯一约束迁移完成: constraint=(architecture_id,file_name) db_path=%s",
            self.db_path,
        )

    # ================= Workspace 表的增删改查 =================

    @staticmethod
    def _ensure_workspace_mapping(
        conn: sqlite3.Connection,
        architecture_id: int,
        workspace_slug: str,
    ) -> None:
        """在现有事务中写入或验证 architecture 与 Workspace 的一对一映射。"""
        try:
            conn.execute(
                """
                INSERT INTO workspaces (architecture_id, workspace_slug)
                VALUES (?, ?)
                """,
                (architecture_id, workspace_slug),
            )
        except sqlite3.IntegrityError as exc:
            row = conn.execute(
                """
                SELECT architecture_id, workspace_slug
                FROM workspaces
                WHERE architecture_id = ? OR workspace_slug = ?
                """,
                (architecture_id, workspace_slug),
            ).fetchone()
            if row == (architecture_id, workspace_slug):
                return
            raise ValueError(
                "工作区映射冲突: "
                f"architecture_id={architecture_id}, workspace_slug={workspace_slug}"
            ) from exc
    
    def get_workspace_slug(self, architecture_id: int) -> str | None:
        """根据类别ID寻找对应的 AnythingLLM 工作区slug"""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT workspace_slug FROM workspaces WHERE architecture_id = ?",
                (architecture_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def add_workspace(self, architecture_id: int, workspace_slug: str):
        """新增类别与工作区映射，并拒绝被静默忽略的冲突。

        相同 architecture 与相同 slug 的重复调用按幂等成功处理；任一侧已经绑定到其他值
        都表示本地记录和外部资源发生冲突，必须显式失败交给协调流程处理。
        """
        with self._lock:
            with self._connect() as conn:
                # 唯一约束冲突既可能是幂等重放，也可能是任一侧已绑定到其他值。统一
                # helper 会读取权威行进行区分，禁止使用 INSERT OR IGNORE 掩盖真实冲突。
                self._ensure_workspace_mapping(
                    conn,
                    architecture_id,
                    workspace_slug,
                )

    # ================= Document 表的增删改查 =================

    @staticmethod
    def _serialize_document_metadata(
        metadata: Mapping[str, Any] | None,
    ) -> str:
        """把业务元数据编码为规范 JSON，拒绝非映射和非有限数值。"""
        if metadata is None:
            metadata_payload: dict[str, Any] = {}
        elif isinstance(metadata, Mapping):
            metadata_payload = dict(metadata)
        else:
            raise TypeError("metadata 必须是 Mapping 或 None")
        return json.dumps(
            metadata_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _upsert_document_record(
        conn: sqlite3.Connection,
        *,
        file_name: str,
        architecture_id: int,
        anything_doc_id: str,
        doc_path: str,
        original_name: str,
        ingested_file_name: str,
        metadata_json: str,
    ) -> None:
        """在调用方事务中 UPSERT 文档行，不隐式提交或获取线程锁。"""
        conn.execute(
            """
            INSERT INTO documents (
                file_name, original_name, ingested_file_name, architecture_id,
                anything_doc_id, doc_path, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(architecture_id, file_name) DO UPDATE SET
                original_name = excluded.original_name,
                ingested_file_name = excluded.ingested_file_name,
                architecture_id = excluded.architecture_id,
                anything_doc_id = excluded.anything_doc_id,
                doc_path = excluded.doc_path,
                metadata_json = excluded.metadata_json
            """,
            (
                file_name,
                original_name if str(original_name or "").strip() else file_name,
                ingested_file_name,
                architecture_id,
                anything_doc_id,
                doc_path,
                metadata_json,
            ),
        )
    
    def save_document_record(
        self,
        file_name: str,
        architecture_id: int,
        anything_doc_id: str,
        doc_path: str = "",
        original_name: str = "",
        ingested_file_name: str = "",
        metadata: Mapping[str, Any] | None = None,
    ):
        """使用显式 UPSERT 保存文档及本地权威业务元数据。

        与 SQLite ``REPLACE`` 不同，``ON CONFLICT DO UPDATE`` 不会先删除旧行，因此不会
        破坏未来外键关系。调用方负责在覆盖外部文档引用前完成阶段 8 的所有权协调；本方法
        只保证本地行更新具有原子性。
        """
        metadata_json = self._serialize_document_metadata(metadata)
        # 该方法仍供少量内部调试/测试场景直接写入文档记录。为避免绕过正式索引链路时
        # 把业务哈希名误写成实际上传名，这里同样要求调用方明确提供已上传文件的基名。
        normalized_ingested_file_name = (
            str(ingested_file_name or "")
            .replace("\\", "/")
            .rsplit("/", 1)[-1]
            .strip()
        )
        if (
            not normalized_ingested_file_name
            or normalized_ingested_file_name in {".", ".."}
        ):
            raise ValueError("ingested_file_name必须是有效文件名")
        with self._lock:
            with self._connect() as conn:
                self._upsert_document_record(
                    conn,
                    file_name=file_name,
                    architecture_id=architecture_id,
                    anything_doc_id=anything_doc_id,
                    doc_path=doc_path,
                    original_name=original_name,
                    ingested_file_name=normalized_ingested_file_name,
                    metadata_json=metadata_json,
                )

    def commit_indexed_document(
        self,
        *,
        architecture_id: int,
        workspace_slug: str,
        file_name: str,
        original_name: str,
        ingested_file_name: str,
        anything_doc_id: str,
        doc_path: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """原子提交永久 Workspace 映射和文档权威记录。

        AnythingLLM 的绑定已经在外部完成时，本方法是本地提交点。映射或文档任一步失败
        都会回滚同一 SQLite 事务，协调记录随后保持 ``external_succeeded``，供任务重试
        直接恢复本地提交而不是重新上传文档。
        """
        if isinstance(architecture_id, bool) or not isinstance(architecture_id, int):
            raise TypeError("architecture_id 必须是整数")
        if architecture_id < 1:
            raise ValueError("architecture_id 必须是正整数")
        required_values = {
            "workspace_slug": workspace_slug,
            "file_name": file_name,
            "ingested_file_name": ingested_file_name,
            "anything_doc_id": anything_doc_id,
            "doc_path": doc_path,
        }
        normalized = {
            name: str(value or "").strip()
            for name, value in required_values.items()
        }
        for name, value in normalized.items():
            if not value:
                raise ValueError(f"{name} 不能为空")
        metadata_json = self._serialize_document_metadata(metadata)
        # original_name 属于甲方业务展示字段。仅做空值校验，不能使用 strip 后的文本
        # 覆盖原值，避免后续 weaponry callback 丢失 originalFileName 原貌。
        persisted_original_name = str(original_name or "")
        if not persisted_original_name.strip():
            persisted_original_name = normalized["file_name"]

        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._ensure_workspace_mapping(
                    conn,
                    architecture_id,
                    normalized["workspace_slug"],
                )
                self._upsert_document_record(
                    conn,
                    file_name=normalized["file_name"],
                    architecture_id=architecture_id,
                    anything_doc_id=normalized["anything_doc_id"],
                    doc_path=normalized["doc_path"],
                    original_name=persisted_original_name,
                    ingested_file_name=normalized["ingested_file_name"],
                    metadata_json=metadata_json,
                )
        logger.info(
            "永久知识库本地记录已提交: architecture_id=%s file_name=%s "
            "has_ingested_file_name=%s metadata_key_count=%d",
            architecture_id,
            normalized["file_name"],
            bool(normalized["ingested_file_name"]),
            len(metadata or {}),
        )

    def delete_document_by_location(
        self,
        *,
        workspace_slug: str,
        doc_path: str,
    ) -> int:
        """按集合与不透明外部位置删除本地文档记录，并返回实际删除行数。"""
        normalized_workspace = str(workspace_slug or "").strip()
        normalized_path = str(doc_path or "").strip()
        if not normalized_workspace or not normalized_path:
            raise ValueError("workspace_slug 和 doc_path 不能为空")
        with self._lock:
            with self._connect() as conn:
                # 必须先取得 SQLite 写保留锁，再读取活动 Operation。否则 SELECT 本身不会
                # 开启写事务，另一个连接可能在门禁检查与 DELETE 之间成功保留同一文档。
                conn.execute("BEGIN IMMEDIATE")
                row_ids = tuple(
                    int(row[0])
                    for row in conn.execute(
                        """
                        SELECT documents.id
                        FROM documents
                        JOIN workspaces
                          ON workspaces.architecture_id = documents.architecture_id
                        WHERE documents.doc_path = ?
                          AND workspaces.workspace_slug = ?
                        """,
                        (normalized_path, normalized_workspace),
                    ).fetchall()
                )
                self._ensure_documents_not_reassigning(conn, row_ids)
                cursor = conn.execute(
                    """
                    DELETE FROM documents
                    WHERE doc_path = ?
                      AND architecture_id = (
                          SELECT architecture_id FROM workspaces
                          WHERE workspace_slug = ?
                      )
                    """,
                    (normalized_path, normalized_workspace),
                )
                deleted_count = int(cursor.rowcount)
        logger.info(
            "永久知识库本地文档解绑完成: deleted_count=%d "
            "has_workspace_ref=%s has_document_path=%s",
            deleted_count,
            bool(normalized_workspace),
            bool(normalized_path),
        )
        return deleted_count

    def _ensure_documents_not_reassigning(
        self,
        connection: sqlite3.Connection,
        document_row_ids: tuple[int, ...],
    ) -> None:
        """拒绝删除仍由分类变更 Saga 持有保护的业务行。

        Operation 是可独立留存的历史快照，所以终态记录不会阻止删除；但活动状态包含未知
        外部副作用和恢复 fencing，绕过它删除 ``documents`` 会让补偿失去本地权威锚点。
        DatabaseService 可能在 Reassign Schema 创建前使用，因此先检查事实表是否存在。
        """

        if not document_row_ids:
            return
        table_exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'reassign_operations'
            """
        ).fetchone()
        if table_exists is None:
            return
        row_placeholders = ", ".join("?" for _ in document_row_ids)
        status_placeholders = ", ".join("?" for _ in self._ACTIVE_REASSIGN_STATUSES)
        active = connection.execute(
            f"""
            SELECT 1
            FROM reassign_operations
            WHERE document_row_id IN ({row_placeholders})
              AND status IN ({status_placeholders})
            LIMIT 1
            """,
            (*document_row_ids, *self._ACTIVE_REASSIGN_STATUSES),
        ).fetchone()
        if active is not None:
            logger.warning(
                "拒绝删除存在活动分类变更的文档记录: document_count=%d",
                len(document_row_ids),
            )
            raise RuntimeError("文档存在活动中的分类变更，禁止删除本地记录")

    def get_document_record(
        self,
        file_name: str,
        *,
        architecture_id: int | None = None,
    ) -> dict | None:
        """获取文档记录；同名文件跨 architecture 时要求调用方显式消歧。

        旧接口没有 architecture 参数，因此在仅命中一行时保持兼容。命中多行时明确失败，
        禁止随机返回某个永久集合的文档并导致错误解绑或错误检索。
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if architecture_id is None:
                rows = conn.execute(
                    "SELECT * FROM documents WHERE file_name = ? ORDER BY architecture_id",
                    (file_name,),
                ).fetchall()
                if len(rows) > 1:
                    raise ValueError(
                        "存在跨 architecture 的同名文档，必须提供 architecture_id"
                    )
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM documents
                    WHERE file_name = ? AND architecture_id = ?
                    """,
                    (file_name, architecture_id),
                ).fetchall()
            if not rows:
                return None
            row = rows[0]
            result = dict(row)
            result["metadata"] = self._deserialize_document_metadata(
                result.pop("metadata_json")
            )
            return result

    def list_document_records(self) -> list[dict]:
        """按文件名和分类升序返回全部文档记录。

        数据库结构迁移由初始化阶段统一完成，因此本方法是无副作用的纯查询。无记录时
        返回空列表，任何正常路径都不得返回 ``None``，以维持公开类型标注承诺的契约。
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT file_name, original_name, ingested_file_name, architecture_id,
                       anything_doc_id, doc_path, metadata_json
                FROM documents
                ORDER BY file_name ASC, architecture_id ASC
                """
            )
            records: list[dict] = []
            for row in cursor.fetchall():
                record = dict(row)
                record["metadata"] = self._deserialize_document_metadata(
                    record.pop("metadata_json")
                )
                records.append(record)
            return records

    def list_document_records_by_architecture_id(
        self,
        architecture_id: int,
        *,
        limit: int | None = None,
    ) -> list[dict]:
        """按精确类别 ID 返回可用于快照解析的直接文档记录。

        本查询刻意不读取 architecture 树，也不在 Python 中过滤全量目录。复合唯一索引
        ``(architecture_id, file_name)`` 可直接服务等值条件；代理主键 ``id`` 只用于同名
        异常数据下的确定排序，不承担公开或业务身份。调用方可传入正整数 ``limit``，
        在 SQL 层截断候选读取，避免异常大类别先被完整装入内存。
        """
        if isinstance(architecture_id, bool) or not isinstance(
            architecture_id,
            int,
        ):
            raise TypeError("architecture_id 必须是整数")
        if architecture_id < 1 or architecture_id > 9223372036854775807:
            raise ValueError("architecture_id 必须是正整数")
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
        ):
            raise ValueError("limit 必须是正整数或 None")

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            query = """
                SELECT id, file_name, original_name, ingested_file_name,
                       architecture_id, anything_doc_id, doc_path, metadata_json
                FROM documents
                WHERE architecture_id = ?
                ORDER BY file_name ASC, id ASC
            """
            parameters: tuple[int, ...] = (architecture_id,)
            if limit is not None:
                query += "\nLIMIT ?"
                parameters = (architecture_id, limit)
            cursor = conn.execute(query, parameters)
            records: list[dict] = []
            for row in cursor.fetchall():
                record = dict(row)
                record["metadata"] = self._deserialize_document_metadata(
                    record.pop("metadata_json")
                )
                records.append(record)
            return records

    @staticmethod
    def _deserialize_document_metadata(value: str) -> dict:
        """严格解析本地业务元数据，禁止以空对象掩盖数据库损坏。"""
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("documents.metadata_json 必须是 JSON 对象")
        return parsed
            
    def delete_document_record(
        self,
        file_name: str,
        *,
        architecture_id: int | None = None,
    ) -> None:
        """删除指定文档；跨 architecture 同名时拒绝模糊删除。"""
        with self._lock:
            try:
                with self._connect() as conn:
                    # 门禁读取与删除必须属于同一写事务，和 Reassignment Repository 的
                    # ``BEGIN IMMEDIATE`` 竞争同一数据库写锁，关闭 TOCTOU 窗口。
                    conn.execute("BEGIN IMMEDIATE")
                    if architecture_id is None:
                        count = conn.execute(
                            "SELECT COUNT(*) FROM documents WHERE file_name = ?",
                            (file_name,),
                        ).fetchone()[0]
                        if count > 1:
                            raise ValueError(
                                "存在跨 architecture 的同名文档，禁止模糊删除"
                            )
                        row_ids = tuple(
                            int(row[0])
                            for row in conn.execute(
                                "SELECT id FROM documents WHERE file_name = ?",
                                (file_name,),
                            ).fetchall()
                        )
                        self._ensure_documents_not_reassigning(conn, row_ids)
                        conn.execute(
                            "DELETE FROM documents WHERE file_name = ?",
                            (file_name,),
                        )
                    else:
                        row_ids = tuple(
                            int(row[0])
                            for row in conn.execute(
                                """
                                SELECT id FROM documents
                                WHERE file_name = ? AND architecture_id = ?
                                """,
                                (file_name, architecture_id),
                            ).fetchall()
                        )
                        self._ensure_documents_not_reassigning(conn, row_ids)
                        conn.execute(
                            """
                            DELETE FROM documents
                            WHERE file_name = ? AND architecture_id = ?
                            """,
                            (file_name, architecture_id),
                        )
                logger.info("本地文档记录已删除: file_name=%s", file_name)
            except Exception:
                logger.exception("删除文档记录失败: file_name=%s", file_name)
                raise

    def get_original_name(self, file_name: str) -> str:
        """根据哈希文件名查询原始文件名，若无记录则回退返回 file_name 本身"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT original_name FROM documents WHERE file_name = ?",
                (file_name,),
            ).fetchall()
            if len(rows) > 1:
                raise ValueError("存在跨 architecture 的同名文档，无法唯一解析原始文件名")
            row = rows[0] if rows else None
            if row and row[0]:
                return row[0]
            return file_name
