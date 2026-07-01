import logging
import sys
import os


def setup_logging():
    """
    配置全局日志格式和级别。
    使用 force=True 确保 Flask debug 重载器重启后也能正确覆盖 handler。
    """
    log_level_str = os.getenv("DOCSENSE_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    log_format = "[%(asctime)s] %(levelname)s [%(name)s:%(lineno)d] %(message)s"

    # force=True 确保即使 root logger 已有 handler 也重新配置
    logging.basicConfig(
        level=log_level,
        format=log_format,
        force=True,
        handlers=[
            logging.StreamHandler(sys.stderr)
        ]
    )

    # 确保 root logger 级别正确（basicConfig 在有 handler 时可能不更新 level）
    logging.getLogger().setLevel(log_level)

    # 抑制一些第三方库的详细日志
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("argostranslate").setLevel(logging.WARNING)
    logging.getLogger("argostranslate.utils").setLevel(logging.WARNING)

    logging.info("日志系统初始化完成 (Level: %s)", log_level_str)
