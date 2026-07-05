import logging
import os
import sys


LOG_FORMAT = "[%(asctime)s] %(levelname)s [%(name)s:%(lineno)d] %(message)s"
"""DocSense 统一日志格式。"""

LOG_DATE_FORMAT = "%y-%m-%d %H:%M:%S"
"""紧凑的本地时间格式，不输出世纪位和毫秒。"""

_THIRD_PARTY_LOG_LEVELS = {
    "werkzeug": logging.WARNING,
    "urllib3": logging.WARNING,
    "argostranslate": logging.WARNING,
    "argostranslate.utils": logging.WARNING,
}
"""需要限制详细输出的第三方 logger 及其最低级别。"""


def apply_third_party_log_levels() -> None:
    """幂等应用第三方库日志级别策略。

    该函数既在应用日志初始化时调用，也会在 Argos Translate 延迟导入完成后再次调用。
    Argos 的 ``utils`` 模块会在导入时主动把自身 logger 改成 ``INFO``，因此仅在进程启动
    阶段设置一次并不可靠。集中提供可重复执行的策略函数，可以避免业务代码复制 logger
    名称，并确保依赖导入顺序不会改变最终日志级别。
    """
    for logger_name, level in _THIRD_PARTY_LOG_LEVELS.items():
        logging.getLogger(logger_name).setLevel(level)


def setup_logging() -> None:
    """配置应用根日志格式、级别和第三方库降噪策略。"""
    log_level_str = os.getenv("DOCSENSE_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    # 基础配置，输出到 stderr
    logging.basicConfig(
        level=log_level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stderr)
        ],
    )

    apply_third_party_log_levels()

    logging.info("日志系统初始化完成 (Level: %s)", log_level_str)
