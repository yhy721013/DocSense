"""文件对话领域使用的稳定数值边界。"""

from __future__ import annotations


# 前端使用 JavaScript 原生 ``Number`` 解析 JSON。超过该上限的整数无法保证精确往返，
# 可能在 history 返回后被舍入成另一个 architectureId。Chat 公开合同因此只接受
# ECMAScript 安全整数范围；Weaponry 等其他业务仍保留各自已经批准的 64 位 ID 合同。
MAX_CHAT_ARCHITECTURE_ID = 9_007_199_254_740_991

# 下列默认值与现有运行设置保持一致。生产组合根会显式注入环境配置；这些常量只为
# 直接构造应用服务的离线测试和替代组合根提供确定性默认值，领域代码不会读取环境。
DEFAULT_CHAT_MAX_FILES_PER_REQUEST = 20
DEFAULT_CHAT_MAX_MESSAGE_CHARS = 12_000
DEFAULT_CHAT_MAX_OUTPUT_CHARS = 100_000
DEFAULT_CHAT_MAX_CONCURRENT_STREAMS = 4


__all__ = [
    "DEFAULT_CHAT_MAX_CONCURRENT_STREAMS",
    "DEFAULT_CHAT_MAX_FILES_PER_REQUEST",
    "DEFAULT_CHAT_MAX_MESSAGE_CHARS",
    "DEFAULT_CHAT_MAX_OUTPUT_CHARS",
    "MAX_CHAT_ARCHITECTURE_ID",
]
