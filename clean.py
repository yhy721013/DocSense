"""
测试数据清理脚本 (clean.py)

该脚本的主要用途是：
在我们的单元测试、集成测试或本地调试完成后，自动清理残留产生的测试数据和环境状态。
这些残留数据主要包括两部分：
1. 项目本地的临时测试数据：例如存放在 `.runtime` 目录下的 SQLite 数据库（如 knowledge_base.sqlite3、chat_sessions.sqlite3）、OCR 缓存等本地文件。
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

# 配置标准输出日志，方便在终端运行脚本时直接观察清理进度
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# 添加项目根目录到 Python 模块搜索路径
# 这样能够在这份独立脚本中，直接像项目入口一样无缝 import 项目内的各种系统依赖模块（比如 app.）
root_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(root_dir))


def clean_runtime():
    """
    清理本地运行时产生的所有临时文件夹 (.runtime)
    
    其中包括: 
    - 测试用或临时创建的 SQLite 数据库
    - 下载的文件、系统临时解析缓存等
    """
    runtime_dir = root_dir / ".runtime"
    if runtime_dir.exists() and runtime_dir.is_dir():
        logger.info(f"正在删除本地临时运行时目录: {runtime_dir} ...")
        # 多次尝试，以防 Windows 下杀毒软件、文件句柄未完全释放等导致暂时被占用（引发 [WinError 32] 错误）
        for _ in range(3):
            try:
                shutil.rmtree(runtime_dir)
                logger.info("成功删除 .runtime 文件夹。")
                break
            except Exception as e:
                logger.warning(f"删除 .runtime 失败: {e}，等待后重试...")
                time.sleep(1) # 发现失败后等待1秒再次重发尝试
        else:
            # 如果三次都失败，则打印最终的 Error，提醒可能存在必须手动干预的后台占用进程
            logger.error("重试多次后仍无法删除 .runtime 文件夹。可能由于残留进程占用。")
    else:
        logger.info(".runtime 文件夹不存在或已被删除，无需操作。")

def clean_anythingllm():
    """
    清理 AnythingLLM 相关测试数据
    
    包括:
    1. 调用 API 删除 AnythingLLM 系统中目前存在的所有 Workspaces
    2. 物理删除已上传到底层磁盘的所有 Document 文档文件（含初始文档和缓存）
    """
    # 【重点策略】：我们在这里进行包的即时（延迟）导入，而不是在文件头部导入。
    # 因为导入 app.services.core.config 时系统有可能在后台初始化配置所关连的一些数据库（比如连接并创建 SQLite 文件），
    # 这导致 .runtime 目录下的 SQLite 立即被数据库引擎创建并获得文件锁，如果将导入置放于头部则会导致先前的 `clean_runtime()` 函数操作中 shutil.rmtree() 无法删除被锁定的 .runtime 文件。
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
            logger.info("正在执行底层目录文件清空...")
            try:
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
    
    # 步骤一：先由于未锁定文件的情况下强制清理本地产生的 .runtime 和各 SQLite DB
    clean_runtime()
    
    # 步骤二：清理 AnythingLLM 上的所有业务状态与存储记录
    clean_anythingllm()
    
    logger.info("=== 测试数据及环境状态清理脚本全部执行完毕 ===")

if __name__ == "__main__":
    main()

