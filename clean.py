import os
import sys
import shutil
import logging
from pathlib import Path
import time

# 配置日志
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# 添加项目根目录到 Python 路径
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))


def clean_runtime():
    runtime_dir = root_dir / ".runtime"
    if runtime_dir.exists() and runtime_dir.is_dir():
        logger.info(f"正在删除 {runtime_dir} ...")
        # 多次尝试，以防杀毒软件或者其他进程临时占用
        for _ in range(3):
            try:
                shutil.rmtree(runtime_dir)
                logger.info("成功删除 .runtime 文件夹。")
                break
            except Exception as e:
                logger.warning(f"删除 .runtime 失败: {e}，等待后重试...")
                time.sleep(1)
        else:
            logger.error("重试多次后仍无法删除 .runtime 文件夹。可能由于残留进程占用。")
    else:
        logger.info(".runtime 文件夹不存在或已被删除。")

def clean_anythingllm():
    # 延迟导入以避免在模块加载阶段初始化 sqlite3 数据库从而锁定 .runtime 内的文件
    from app.services.core.config import load_anythingllm_config
    from app.services.utils.anythingllm_client import AnythingLLMClient

    try:
        config = load_anythingllm_config()
        client = AnythingLLMClient(config=config)
    except Exception as e:
        logger.error(f"加载 AnythingLLM 配置或客户端失败: {e}")
        return

    # 1. 删除所有 Workspaces
    logger.info("获取所有 AnythingLLM Workspaces ...")
    workspaces = client.list_workspaces()
    for ws in workspaces:
        ws_slug = ws.get("slug")
        ws_name = ws.get("name")
        if ws_slug:
            logger.info(f"正在删除 Workspace: {ws_name} ({ws_slug})...")
            success = client.delete_workspace(ws_slug)
            if success:
                logger.info(f"成功删除 Workspace: {ws_name}")
            else:
                logger.error(f"删除 Workspace: {ws_name} 失败。")

    # 2. 删除所有上传给 AnythingLLM 的文档文件
    # 直接利用 anythingllm_client 的逻辑解析存储路径
    storage_root = client._resolve_storage_root()
    if storage_root:
        docs_dir = Path(storage_root) / "documents"
        if docs_dir.exists() and docs_dir.is_dir():
            logger.info(f"检测到 AnythingLLM document 存储路径: {docs_dir}")
            logger.info("正在删除所有上传的文档数据...")
            try:
                shutil.rmtree(docs_dir)
                logger.info(f"成功删除 AnythingLLM 文档数据文件夹: {docs_dir}")
            except Exception as e:
                logger.error(f"删除文档存储文件夹失败: {e}")
        else:
            logger.info("AnythingLLM 文档数据文件夹不存在，无需删除。")
            
        vector_cache_dir = Path(storage_root) / "vector-cache"
        if vector_cache_dir.exists() and vector_cache_dir.is_dir():
            try:
                shutil.rmtree(vector_cache_dir)
                logger.info(f"成功删除 AnythingLLM 向量缓存文件夹: {vector_cache_dir}")
            except Exception as e:
                logger.error(f"删除验证缓存文件夹失败: {e}")
    else:
        logger.warning("未能解析出 AnythingLLM storage root，跳过本地文档数据清理。")


def main():
    logger.info("=== 开始执行测试数据清理脚本 ===")
    clean_runtime()
    clean_anythingllm()
    logger.info("=== 测试数据清理脚本执行完成 ===")

if __name__ == "__main__":
    main()
