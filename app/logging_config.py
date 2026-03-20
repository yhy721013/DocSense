import logging
import sys
import os

def setup_logging():
    """
    配置全局日志格式和级别。
    """
    log_level_str = os.getenv("DOCSENSE_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    log_format = "[%(asctime)s] %(levelname)s [%(name)s:%(lineno)d] %(message)s"
    
    # 基础配置，输出到 stderr
    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stderr)
        ]
    )
    
    # 抑制一些第三方库的详细日志
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    
    logging.info("日志系统初始化完成 (Level: %s)", log_level_str)
