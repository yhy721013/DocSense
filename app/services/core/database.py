import json
import logging
import sqlite3
import threading
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

class DatabaseService:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock() # 异步多线程场景下写库必备的锁
        self._init_db()

    def _init_db(self):
        """初始化建表，加上 IF NOT EXISTS 不必担心重复创建"""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                # 1. 创建工作区映射表 (按你设计的 3 个字段)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS workspaces (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        architecture_id INTEGER NOT NULL UNIQUE,
                        workspace_slug TEXT NOT NULL UNIQUE
                    )
                """)
                # 2. 创建文档明细表 (按你设计的 3 个字段)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS documents (
                        file_name TEXT PRIMARY KEY,
                        original_name TEXT NOT NULL DEFAULT '',
                        architecture_id INTEGER NOT NULL,
                        anything_doc_id TEXT NOT NULL,
                        doc_path TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    )
                """)
                self._ensure_documents_schema(conn)
                conn.commit()
            logger.info("数据库初始化完成: %s", self.db_path)

    def _ensure_documents_schema(self, conn: sqlite3.Connection) -> None:
        """兼容已存在的旧版 documents 表。"""
        cursor = conn.execute("PRAGMA table_info(documents)")
        columns = {row[1] for row in cursor.fetchall()}

        if "original_name" not in columns:
            conn.execute("ALTER TABLE documents ADD COLUMN original_name TEXT NOT NULL DEFAULT ''")
            logger.info("已为 documents 表补充 original_name 列: %s", self.db_path)

        if "doc_path" not in columns:
            conn.execute("ALTER TABLE documents ADD COLUMN doc_path TEXT")
            logger.info("已为 documents 表补充 doc_path 列: %s", self.db_path)

        if "metadata_json" not in columns:
            conn.execute(
                "ALTER TABLE documents "
                "ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
            logger.info("已为 documents 表补充 metadata_json 列: %s", self.db_path)

        conn.execute(
            """
            UPDATE documents
            SET original_name = file_name
            WHERE original_name IS NULL OR original_name = ''
            """
        )

    # ================= Workspace 表的增删改查 =================
    
    def get_workspace_slug(self, architecture_id: int) -> str | None:
        """根据类别ID寻找对应的 AnythingLLM 工作区slug"""
        with sqlite3.connect(self.db_path) as conn:
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
            with sqlite3.connect(self.db_path) as conn:
                try:
                    conn.execute(
                        """
                        INSERT INTO workspaces (architecture_id, workspace_slug)
                        VALUES (?, ?)
                        """,
                        (architecture_id, workspace_slug),
                    )
                except sqlite3.IntegrityError as exc:
                    # 唯一约束冲突既可能是同一映射的幂等重放，也可能是 architecture 或
                    # slug 被另一条记录占用。必须读取冲突后的权威行进行区分，不能使用
                    # INSERT OR IGNORE 把两类结果都伪装成成功。
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

    # ================= Document 表的增删改查 =================
    
    def save_document_record(
        self,
        file_name: str,
        architecture_id: int,
        anything_doc_id: str,
        doc_path: str = "",
        original_name: str = "",
        metadata: Mapping[str, Any] | None = None,
    ):
        """使用显式 UPSERT 保存文档及本地权威业务元数据。

        与 SQLite ``REPLACE`` 不同，``ON CONFLICT DO UPDATE`` 不会先删除旧行，因此不会
        破坏未来外键关系。调用方负责在覆盖外部文档引用前完成阶段 8 的所有权协调；本方法
        只保证本地行更新具有原子性。
        """
        if metadata is None:
            metadata_payload: dict[str, Any] = {}
        elif isinstance(metadata, Mapping):
            metadata_payload = dict(metadata)
        else:
            raise TypeError("metadata 必须是 Mapping 或 None")
        metadata_json = json.dumps(
            metadata_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO documents (
                        file_name, original_name, architecture_id,
                        anything_doc_id, doc_path, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_name) DO UPDATE SET
                        original_name = excluded.original_name,
                        architecture_id = excluded.architecture_id,
                        anything_doc_id = excluded.anything_doc_id,
                        doc_path = excluded.doc_path,
                        metadata_json = excluded.metadata_json
                """, (
                    file_name,
                    original_name or file_name,
                    architecture_id,
                    anything_doc_id,
                    doc_path,
                    metadata_json,
                ))
                conn.commit()

    def get_document_record(self, file_name: str) -> dict | None:
        """获取特定文档的入库信息（用于删除文件前的反向定位）"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM documents WHERE file_name = ?", (file_name,))
            row = cursor.fetchone()
            if row is None:
                return None
            result = dict(row)
            result["metadata"] = self._deserialize_document_metadata(
                result.pop("metadata_json")
            )
            return result

    def list_document_records(self) -> list[dict]:
        """按文件名升序返回全部文档记录。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT file_name, original_name, architecture_id,
                       anything_doc_id, doc_path, metadata_json
                FROM documents
                ORDER BY file_name ASC
                """
            )
            records = []
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
            
    def delete_document_record(self, file_name: str):
        """当文件需要删除时，从数据库抹掉该记录"""
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("DELETE FROM documents WHERE file_name = ?", (file_name,))
                    conn.commit()
                logger.info("已删除文档记录: %s", file_name)
            except Exception as e:
                logger.error("删除文档记录失败 %s: %s", file_name, e)

    def update_document_architecture(self, file_name: str, new_architecture_id: int):
        """更新文档的分类节点"""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE documents SET architecture_id = ? WHERE file_name = ?",
                    (new_architecture_id, file_name)
                )
                conn.commit()
            logger.info("已更新文档类别: file_name=%s, new_architecture_id=%s", file_name, new_architecture_id)

    def get_original_name(self, file_name: str) -> str:
        """根据哈希文件名查询原始文件名，若无记录则回退返回 file_name 本身"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT original_name FROM documents WHERE file_name = ?", (file_name,))
            row = cursor.fetchone()
            if row and row[0]:
                return row[0]
            return file_name


class ChatDatabaseService:
    """对话会话持久化（独立数据库 chat_sessions.sqlite3）"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS chats (
                        chat_id     TEXT PRIMARY KEY,
                        file_original_names  TEXT NOT NULL,
                        turn_timestamps TEXT NOT NULL DEFAULT '[]',
                        workspace_slug TEXT NOT NULL,
                        thread_slug    TEXT NOT NULL,
                        created_at  TEXT NOT NULL,
                        updated_at  TEXT NOT NULL
                    )
                """)
                conn.commit()
            logger.info("对话数据库初始化完成: %s", self.db_path)

    def create_chat(
        self,
        chat_id: str,
        file_original_names: list[str],
        workspace_slug: str,
        thread_slug: str,
    ) -> dict:
        import json
        from datetime import datetime, timezone

        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        now_ms = int(now_dt.timestamp() * 1000)
        file_original_names_json = json.dumps([file_original_names], ensure_ascii=False)
        turn_timestamps_json = json.dumps([now_ms], ensure_ascii=False)
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO chats (
                        chat_id, file_original_names, turn_timestamps, workspace_slug, thread_slug, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (chat_id, file_original_names_json, turn_timestamps_json, workspace_slug, thread_slug, now, now),
                )
                conn.commit()
        logger.info("已创建对话记录: chat_id=%s", chat_id)
        return {
            "chat_id": chat_id,
            "file_original_names": file_original_names,
            "turn_timestamps": [now_ms],
            "workspace_slug": workspace_slug,
            "thread_slug": thread_slug,
            "created_at": now,
            "updated_at": now,
        }

    def get_chat(self, chat_id: str) -> dict | None:
        import json

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            if not row:
                return None
            record = dict(row)
            record["file_original_names"] = json.loads(record["file_original_names"])
            record["turn_timestamps"] = json.loads(record.get("turn_timestamps") or "[]")
            return record

    def list_chats(self) -> list[dict]:
        """按最近更新时间倒序返回全部对话记录。"""
        import json

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT chat_id, file_original_names, turn_timestamps, workspace_slug, thread_slug, created_at, updated_at
                FROM chats
                ORDER BY updated_at DESC
                """
            )
            rows = []
            for row in cursor.fetchall():
                record = dict(row)
                record["file_original_names"] = json.loads(record["file_original_names"])
                record["turn_timestamps"] = json.loads(record.get("turn_timestamps") or "[]")
                rows.append(record)
            return rows

    def append_file_original_names(self, chat_id: str, new_file_original_names: list[str]) -> None:
        """将新增文件原名列表作为一个新的回合追加到已有引用列表中（记录每次交互的文件列表）。"""
        import json
        from datetime import datetime, timezone

        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT file_original_names, turn_timestamps FROM chats WHERE chat_id = ?", (chat_id,)
                )
                row = cursor.fetchone()
                existing: list[list[str]] = json.loads(row["file_original_names"]) if row else []
                existing_turn_timestamps: list[int] = json.loads(row["turn_timestamps"] or "[]") if row else []
                existing.append(new_file_original_names)
                now_dt = datetime.now(timezone.utc)
                now_ms = int(now_dt.timestamp() * 1000)
                existing_turn_timestamps.append(now_ms)
                now = now_dt.isoformat()
                merged_json = json.dumps(existing, ensure_ascii=False)
                merged_turn_timestamps_json = json.dumps(existing_turn_timestamps, ensure_ascii=False)
                conn.execute(
                    "UPDATE chats SET file_original_names = ?, turn_timestamps = ?, updated_at = ? WHERE chat_id = ?",
                    (merged_json, merged_turn_timestamps_json, now, chat_id),
                )
                conn.commit()
        logger.info("已追加对话引用文件: chat_id=%s, new_count=%d", chat_id, len(new_file_original_names))

    def delete_chat(self, chat_id: str) -> None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
                conn.commit()
        logger.info("已删除对话记录: chat_id=%s", chat_id)
