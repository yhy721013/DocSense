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
                        anything_doc_id TEXT NOT NULL
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
    
    def save_document_record(self, file_name: str, architecture_id: int, anything_doc_id: str):
        """文件解析成功后，将其信息存入表中（由于 status 不存，只存这三个）"""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                # 用 REPLACE 防止同一个文件被多次解析时报主键冲突
                conn.execute("""
                    REPLACE INTO documents (file_name, architecture_id, anything_doc_id)
                    VALUES (?, ?, ?)
                """, (file_name, architecture_id, anything_doc_id))
                conn.commit()

    def get_document_record(self, file_name: str) -> dict | None:
        """获取特定文档的入库信息（用于删除文件前的反向定位）"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM documents WHERE file_name = ?", (file_name,))
            row = cursor.fetchone()
            return dict(row) if row else None
            
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
