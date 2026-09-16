"""文件对话领域使用的稳定数值边界。"""

from __future__ import annotations


# 前端使用 JavaScript 原生 ``Number`` 解析 JSON。超过该上限的整数无法保证精确往返，
# 可能在 history 返回后被舍入成另一个 architectureId。Chat 公开合同因此只接受
# ECMAScript 安全整数范围；Weaponry 等其他业务仍保留各自已经批准的 64 位 ID 合同。
MAX_CHAT_ARCHITECTURE_ID = 9_007_199_254_740_991


__all__ = ["MAX_CHAT_ARCHITECTURE_ID"]
