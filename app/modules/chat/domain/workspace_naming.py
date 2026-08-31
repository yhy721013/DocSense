"""Chat 业务身份到远端 Workspace 展示名称的纯领域规则。

本模块只解释已经由持久化层确认的不可变身份绑定，不读取 HTTP 请求、数据库或
AnythingLLM。这样后台执行器只要凭 ``run_id`` 恢复权威 Binding，就能在任意任务进程中
得到相同名称；未来替换队列或数据库适配器时也不需要复制命名逻辑。
"""

from __future__ import annotations

from app.modules.chat.domain.identity import (
    ConversationIdentityBinding,
    IDENTITY_KIND_FILE,
    IDENTITY_KIND_WEAPONRY,
)


FILE_CHAT_WORKSPACE_PREFIX = "chat-id"
WEAPONRY_CHAT_WORKSPACE_USER_PREFIX = "wChat-user"
WEAPONRY_CHAT_WORKSPACE_ARCHITECTURE_PREFIX = "-arch"


def chat_workspace_name(binding: ConversationIdentityBinding) -> str:
    """根据不可变业务身份生成规范的 Workspace 展示名称。

    ``ConversationIdentityBinding`` 自身已经校验身份类型和字段组合。本函数仍显式检查
    必需字段，避免以后新增身份类型或构造路径时悄然生成 ``None``、空串等错误名称。
    名称不包含内部 ``conversation_id``；它仍由运行、租约和 Thread 命名独立使用。
    """

    if not isinstance(binding, ConversationIdentityBinding):
        raise TypeError("binding must be ConversationIdentityBinding")

    if binding.identity_kind == IDENTITY_KIND_FILE:
        if binding.chat_id is None:
            # 正常构造的 Binding 不会进入这里；保留失败关闭检查可以防止未来反序列化
            # 或迁移代码绕过领域构造器后生成不可追踪的供应商资源。
            raise ValueError("file identity binding requires chat_id")
        return f"{FILE_CHAT_WORKSPACE_PREFIX}{binding.chat_id}"

    if binding.identity_kind == IDENTITY_KIND_WEAPONRY:
        if binding.user_id is None or binding.architecture_id is None:
            raise ValueError(
                "weaponry identity binding requires user_id and architecture_id"
            )
        return (
            f"{WEAPONRY_CHAT_WORKSPACE_USER_PREFIX}{binding.user_id}"
            f"{WEAPONRY_CHAT_WORKSPACE_ARCHITECTURE_PREFIX}"
            f"{binding.architecture_id}"
        )

    # Binding 当前会在构造阶段拒绝未知类型；这里仍保留显式分支，使新增身份种类时必须
    # 同步决定 Workspace 命名，而不能意外回退到某个既有前缀。
    raise ValueError("unsupported conversation identity kind")


__all__ = [
    "FILE_CHAT_WORKSPACE_PREFIX",
    "WEAPONRY_CHAT_WORKSPACE_ARCHITECTURE_PREFIX",
    "WEAPONRY_CHAT_WORKSPACE_USER_PREFIX",
    "chat_workspace_name",
]
