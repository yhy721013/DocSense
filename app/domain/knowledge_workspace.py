"""永久知识谱系 Workspace 的共享纯命名规则。

永久知识谱系同时被 Analysis 入库、Weaponry 读取和 Reassign 分类迁移使用。如果各业务模块
分别拼接名称，前向创建、故障查回和恢复流程很容易在后续演进中产生不同前缀。本模块因此只
承担一项稳定职责：把数据库权威的知识谱系整数 ID 转换为外部 Workspace 展示名称。

本模块不读取环境、数据库、文件、时钟或网络，也不依赖 AnythingLLM。调用方必须先完成各自
公开参数兼容与领域校验，再把已经规范化的整数传入；AnythingLLM 返回的 slug 仍是不透明引用。
"""

from __future__ import annotations


# 与 SQLite INTEGER、Reassign 既有兼容投影保持一致。Analysis 当前只传正整数，Reassign 为了
# 保持已经冻结的公开兼容输入仍可能得到 0 或负数；共享命名层只保证它们是稳定的 64 位整数。
ARCHITECTURE_ID_MIN = -(2**63)
ARCHITECTURE_ID_MAX = 2**63 - 1
PERMANENT_ARCHITECTURE_WORKSPACE_PREFIX = "archId-"


def permanent_architecture_workspace_name(architecture_id: int) -> str:
    """返回永久知识谱系 Workspace 的确定性展示名称。

    布尔值虽然是 Python ``int`` 的子类，但在命名中代表调用方遗漏了兼容投影或领域校验，必须
    显式拒绝。函数也拒绝超出数据库可表示范围的整数，避免远端先创建、随后本地映射失败。
    """

    if isinstance(architecture_id, bool) or not isinstance(architecture_id, int):
        raise TypeError("architecture_id 必须是有符号 64 位整数")
    if not ARCHITECTURE_ID_MIN <= architecture_id <= ARCHITECTURE_ID_MAX:
        raise ValueError("architecture_id 超出有符号 64 位整数范围")
    return f"{PERMANENT_ARCHITECTURE_WORKSPACE_PREFIX}{architecture_id}"


__all__ = [
    "ARCHITECTURE_ID_MAX",
    "ARCHITECTURE_ID_MIN",
    "PERMANENT_ARCHITECTURE_WORKSPACE_PREFIX",
    "permanent_architecture_workspace_name",
]

