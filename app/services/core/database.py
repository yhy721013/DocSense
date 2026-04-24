import sqlite3
import threading
import logging

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
                        architecture_id INTEGER NOT NULL,
                        anything_doc_id TEXT NOT NULL,
                        doc_path TEXT
                    )
                """)
                conn.commit()
            logger.info("数据库初始化完成: %s", self.db_path)

    # ================= Workspace 表的增删改查 =================
    
    def get_workspace_slug(self, architecture_id: int) -> str | None:
        """根据类别ID寻找对应的 AnythingLLM 工作区slug"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT workspace_slug FROM workspaces WHERE architecture_id = ?", (architecture_id,))
            row = cursor.fetchone()
            return row[0] if row else None

    def add_workspace(self, architecture_id: int, workspace_slug: str):
        """新增一个类别和工作区的对应关系"""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                # 若存在则忽略，防止并发时重复插入报错
                conn.execute("""
                    INSERT OR IGNORE INTO workspaces (architecture_id, workspace_slug)
                    VALUES (?, ?)
                """, (architecture_id, workspace_slug))
                conn.commit()

    # ================= Document 表的增删改查 =================
    
    def save_document_record(self, file_name: str, architecture_id: int, anything_doc_id: str, doc_path: str = ""):
        """文件解析成功后，将其信息存入表中"""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                # 用 REPLACE 防止同一个文件被多次解析时报主键冲突
                conn.execute("""
                    REPLACE INTO documents (file_name, architecture_id, anything_doc_id, doc_path)
                    VALUES (?, ?, ?, ?)
                """, (file_name, architecture_id, anything_doc_id, doc_path))
                conn.commit()

    def get_document_record(self, file_name: str) -> dict | None:
        """获取特定文档的入库信息（用于删除文件前的反向定位）"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM documents WHERE file_name = ?", (file_name,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_document_records(self) -> list[dict]:
        """按文件名升序返回全部文档记录。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT file_name, architecture_id, anything_doc_id, doc_path
                FROM documents
                ORDER BY file_name ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]
            
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
                        file_names  TEXT NOT NULL,
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
        file_names: list[str],
        workspace_slug: str,
        thread_slug: str,
    ) -> dict:
        import json
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        file_names_json = json.dumps(file_names, ensure_ascii=False)
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO chats (chat_id, file_names, workspace_slug, thread_slug, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (chat_id, file_names_json, workspace_slug, thread_slug, now, now),
                )
                conn.commit()
        logger.info("已创建对话记录: chat_id=%s", chat_id)
        return {
            "chat_id": chat_id,
            "file_names": file_names,
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
            record["file_names"] = json.loads(record["file_names"])
            return record

    def list_chats(self) -> list[dict]:
        """按最近更新时间倒序返回全部对话记录。"""
        import json

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT chat_id, file_names, workspace_slug, thread_slug, created_at, updated_at
                FROM chats
                ORDER BY updated_at DESC
                """
            )
            rows = []
            for row in cursor.fetchall():
                record = dict(row)
                record["file_names"] = json.loads(record["file_names"])
                rows.append(record)
            return rows

    def append_file_names(self, chat_id: str, new_file_names: list[str]) -> None:
        """将新增文件追加到已有引用列表（去重，保持顺序）。"""
        import json
        from datetime import datetime, timezone

        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT file_names FROM chats WHERE chat_id = ?", (chat_id,)
                )
                row = cursor.fetchone()
                existing: list[str] = json.loads(row["file_names"]) if row else []
                existing_set = set(existing)
                merged = existing + [fn for fn in new_file_names if fn not in existing_set]
                now = datetime.now(timezone.utc).isoformat()
                merged_json = json.dumps(merged, ensure_ascii=False)
                conn.execute(
                    "UPDATE chats SET file_names = ?, updated_at = ? WHERE chat_id = ?",
                    (merged_json, now, chat_id),
                )
                conn.commit()
        logger.info("已追加对话引用文件: chat_id=%s, new_count=%d", chat_id, len(new_file_names))

    def delete_chat(self, chat_id: str) -> None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
                conn.commit()
        logger.info("已删除对话记录: chat_id=%s", chat_id)
