"""
测试数据清理脚本 (clean.py)

该脚本的主要用途是：
在我们的单元测试、集成测试或本地调试完成后，自动清理残留产生的测试数据和环境状态。
这些残留数据主要包括两部分：
1. 项目本地的临时测试数据：例如存放在 `DOCSENSE_RUNTIME_DIR` 目录下的 SQLite 数据库（如 knowledge_base.sqlite3、chat_sessions.sqlite3）、OCR 缓存等本地文件。
2. AnythingLLM 服务端的测试数据：例如在测试交互流程时临时创建的工作区（Workspaces）以及向 AnythingLLM 系统中上传的各类测试文档文件。

执行该脚本后能将项目和 AnythingLLM 的状态重置为一个干净的环境，避免此前的测试数据影响下一轮测试的结果或者过度占用存储空间。
建议用法：在需要清理的任何时候，通过所在虚拟环境直接运行 `python clean.py`。
"""

import os
import sys
import shutil
import logging
from pathlib import Path
import time

from dotenv import load_dotenv

# 配置标准输出日志，方便在终端运行脚本时直接观察清理进度
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# 添加项目根目录到 Python 模块搜索路径
# 这样能够在这份独立脚本中，直接像项目入口一样无缝 import 项目内的各种系统依赖模块（比如 app.）
root_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(root_dir))
load_dotenv(root_dir / ".env", override=False)


def _runtime_dir() -> Path:
    raw_value = os.getenv("DOCSENSE_RUNTIME_DIR", "").strip()
    if not raw_value:
        return (root_dir / ".runtime").resolve()
    runtime_dir = Path(raw_value).expanduser()
    if not runtime_dir.is_absolute():
        raise RuntimeError("DOCSENSE_RUNTIME_DIR必须配置为绝对路径")
    resolved = runtime_dir.resolve()
    filesystem_root = Path(resolved.anchor).resolve()
    if resolved in {filesystem_root, root_dir.resolve()}:
        raise RuntimeError(
            "DOCSENSE_RUNTIME_DIR禁止指向文件系统根目录或项目根目录"
        )
    return resolved


def _component_database_path(
    env_names: tuple[str, ...],
    default_name: str,
) -> Path:
    """按正式配置规则解析数据库路径，避免兼容覆盖项逃逸清理范围。"""

    for env_name in env_names:
        raw_value = os.getenv(env_name, "").strip()
        if not raw_value:
            continue
        candidate = Path(raw_value).expanduser()
        if not candidate.is_absolute():
            candidate = root_dir / candidate
        return candidate.resolve()
    return (_runtime_dir() / default_name).resolve()


def _is_within(path: Path, directory: Path) -> bool:
    """使用 Path 关系判断归属，避免字符串前缀把相邻目录误判为子目录。"""

    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _remove_database_files(database_path: Path) -> None:
    """删除 SQLite 主文件及同目录 WAL/SHM；任何失败都必须阻断发布。"""

    for candidate in (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    ):
        if not candidate.exists():
            continue
        if not candidate.is_file() and not candidate.is_symlink():
            raise RuntimeError(f"数据库清理目标不是文件: {candidate}")
        candidate.unlink()
        logger.info("成功删除运行时数据库文件: %s", candidate)


def clean_runtime():
    """
    清理 DOCSENSE_RUNTIME_DIR 指向的运行时目录。
    
    其中包括: 
    - 测试用或临时创建的 SQLite 数据库
    - 下载的文件、系统临时解析缓存等
    """
    runtime_dir = _runtime_dir()
    if runtime_dir.exists() and runtime_dir.is_dir():
        logger.info(f"正在清理本地临时运行时目录的内容: {runtime_dir} ...")
        # 多次尝试，以防 Windows 下杀毒软件、文件句柄未完全释放等导致暂时被占用
        for _ in range(3):
            try:
                for item in runtime_dir.iterdir():
                    if item.is_file() or item.is_symlink():
                        item.unlink()
                    elif item.is_dir():
                        shutil.rmtree(item)
                logger.info("成功清理运行时目录内部文件。")
                break
            except Exception as e:
                logger.warning(f"清理运行时目录失败: {e}，等待后重试...")
                time.sleep(1)
        else:
            # 发布流程以退出码判断清理结果；只记日志会让旧任务库被误带入新版本。
            raise RuntimeError(
                "重试多次后仍无法清理运行时目录，可能存在残留进程占用"
            )
    else:
        logger.info("运行时目录不存在或已被删除，无需操作。")

    # 组件级数据库变量拥有高于 DOCSENSE_RUNTIME_DIR 的兼容优先级。显式路径位于
    # runtime 外时，上面的目录清空不会覆盖它们，必须逐一删除并让失败非零退出。
    database_paths = {
        _component_database_path(
            ("DOCSENSE_LLM_TASK_DB",),
            "llm_tasks.sqlite3",
        ),
        _component_database_path(
            ("DOCSENSE_KNOWLEDGE_BASE_DB", "KNOWLEDGE_BASE_DB_PATH"),
            "knowledge_base.sqlite3",
        ),
        _component_database_path(
            ("DOCSENSE_CHAT_DB",),
            "chat_sessions.sqlite3",
        ),
    }
    for database_path in sorted(database_paths, key=str):
        if _is_within(database_path, runtime_dir):
            continue
        logger.warning(
            "检测到运行时目录外的组件数据库，将按发布清库策略显式删除: %s",
            database_path,
        )
        _remove_database_files(database_path)

def clean_anythingllm():
    """
    清理 AnythingLLM 相关测试数据
    
    包括:
    1. 调用 API 删除 AnythingLLM 系统中目前存在的所有 Workspaces
    2. 物理删除已上传到底层磁盘的所有 Document 文档文件（含初始文档和缓存）
    """
    # 【重点策略】：我们在这里进行包的即时（延迟）导入，而不是在文件头部导入。
    # 因为导入 app.services.core.config 时系统有可能在后台初始化配置所关连的一些数据库（比如连接并创建 SQLite 文件），
    # 这会导致运行时目录下的 SQLite 立即被数据库引擎创建并获得文件锁，如果将导入置放于头部则会导致先前的 clean_runtime() 无法删除被锁定的文件。
    from app.services.core.config import load_anythingllm_config
    from app.services.utils.anythingllm_client import AnythingLLMClient

    try:
        # 加载现有环境中的 AnythingLLM 环境配置（含 URL 和 API 密钥等）
        config = load_anythingllm_config()
        # 实例化我们项目内置提供的通信客户端，用于后续与 AnythingLLM 交互通信
        client = AnythingLLMClient(config=config)
    except Exception as e:
        logger.error(f"加载 AnythingLLM 配置或客户端失败: {e}")
        return

    # === [环节 1] 删除所有 Workspaces ===
    logger.info("获取所有 AnythingLLM Workspaces ...")
    workspaces = client.list_workspaces()
    for ws in workspaces:
        ws_slug = ws.get("slug")
        ws_name = ws.get("name")
        if ws_slug:
            logger.info(f"正在准备调用 API 删除 Workspace: {ws_name} (标识: {ws_slug})...")
            # 通过官方提供的 HTTP 接口请求删除（在 AnythingLLM 会产生系统级联清理并解除对应的关系绑定）
            success = client.delete_workspace(ws_slug)
            if success:
                logger.info(f"成功删除 Workspace: {ws_name}")
            else:
                logger.error(f"删除 Workspace: {ws_name} 失败。")

    # === [环节 2] 清理所有上传给 AnythingLLM 的文档及底层文件 ===
    # AnythingLLM 默认本地运行时，会把用户文档放置在 Storage 目录底下。我们采用了从文件系统直接干预的方案，彻底重置文档目录内容而避免繁琐复杂的 API ID查询或失效 404 调用。
    
    # 内部提供的 _resolve_storage_root() 能跨系统自动寻找 AnythingLLM 专属本地存储包根目录 (AppData, ~/.anythingllm 等)
    storage_root = client._resolve_storage_root()
    if storage_root:
        # documents 是接收任何原始文件及拆解分片文件的主要存放所
        docs_dir = Path(storage_root) / "documents"
        if docs_dir.exists() and docs_dir.is_dir():
            logger.info(f"检测到 AnythingLLM document 内部物理存储路径: {docs_dir}")
            logger.info("正在尝试底层目录文件清空...")
            try:
                if not os.access(docs_dir, os.W_OK):
                    logger.warning(f"目录 {docs_dir} 为只读状态 (Docker 挂载)，跳过物理删除。")
                else:
                    shutil.rmtree(docs_dir)
                    logger.info(f"成功清理 AnythingLLM 本地的所有文档数据文件夹: {docs_dir}")
            except Exception as e:
                logger.error(f"删除文档存储文件夹失败，请检查文件占用权限: {e}")
        else:
            logger.info("AnythingLLM 物理文档数据文件夹不存在，无需删除。")
            
        # vector-cache 用来缓存解析生成的特征向量与映射包
        vector_cache_dir = Path(storage_root) / "vector-cache"
        if vector_cache_dir.exists() and vector_cache_dir.is_dir():
            try:
                if not os.access(vector_cache_dir, os.W_OK):
                    logger.warning(f"目录 {vector_cache_dir} 为只读状态 (Docker 挂载)，跳过物理删除。")
                else:
                    shutil.rmtree(vector_cache_dir)
                    logger.info(f"成功删除 AnythingLLM 底层向量缓存文件夹: {vector_cache_dir}")
            except Exception as e:
                logger.error(f"删除底层向量缓存文件夹失败: {e}")
    else:
        logger.warning("未能有效解析出 AnythingLLM storage root，本次将跳过对于本地物理文档目录的强制清理。")


def main():
    """
    主执行入口
    """
    logger.info("=== 开始执行测试数据及环境状态清理脚本 ===")
    
    # 步骤一：先在文件未锁定时清理运行时目录和各 SQLite DB
    clean_runtime()
    
    # 步骤二：清理 AnythingLLM 上的所有业务状态与存储记录
    clean_anythingllm()
    
    logger.info("=== 测试数据及环境状态清理脚本全部执行完毕 ===")

if __name__ == "__main__":
    main()

