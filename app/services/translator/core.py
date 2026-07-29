"""兼容旧 HYMTTranslator 导入路径。

语言转换唯一实现已迁入独立 Translation 模块；本文件不得再加入模型、Prompt 或重试逻辑。
"""

from app.modules.translation.adapters.hymt_runtime import HYMTTranslator

__all__ = ["HYMTTranslator"]
