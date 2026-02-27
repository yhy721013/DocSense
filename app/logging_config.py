"""日志配置模块"""

import logging
import logging.config
from typing import Dict, Any


def setup_logging(log_level: str = "INFO") -> None:
    """配置应用程序日志，屏蔽第三方库的冗余日志"""

    # 日志配置字典
    LOGGING_CONFIG: Dict[str, Any] = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'standard': {
                'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            },
        },
        'handlers': {
            'console': {
                'level': log_level,
                'class': 'logging.StreamHandler',
                'formatter': 'standard',
                'stream': 'ext://sys.stdout'
            },
        },
        'loggers': {
            '': {  # root logger
                'handlers': ['console'],
                'level': log_level,
                'propagate': False
            },
            # ✅ 屏蔽第三方库的详细日志
            'pymongo': {
                'handlers': ['console'],
                'level': 'WARNING',  # 只显示警告及以上级别
                'propagate': False
            },
            'pymongo.topology': {
                'handlers': ['console'],
                'level': 'WARNING',  # 特别屏蔽心跳日志
                'propagate': False
            },
            'urllib3': {
                'handlers': ['console'],
                'level': 'WARNING',
                'propagate': False
            },
            'werkzeug': {
                'handlers': ['console'],
                'level': 'INFO',  # Flask的HTTP请求日志保持INFO级别
                'propagate': False
            }
        }
    }

    # 应用配置
    logging.config.dictConfig(LOGGING_CONFIG)

    # 确认配置生效
    logger = logging.getLogger(__name__)
    logger.info("✅ 日志系统配置完成")


def get_logger(name: str) -> logging.Logger:
    """获取指定名称的logger"""
    return logging.getLogger(name)
