"""供离线业务测试复用的端口替身。

本包中的实现只存在于测试代码，不访问网络、数据库或业务文件。生产代码不得导入本包；
测试通过应用层 Protocol 注入这些对象，以验证业务编排而不是具体外部协议。
"""

from .chat import FakeChatConversationFactory, FakeChatConversationPort
from .knowledge_index import FakeKnowledgeIndexFactory, FakeKnowledgeIndexPort
from .rag import (
    FakeDocumentRagFactory,
    FakeDocumentRagPort,
    FakeDocumentRagSession,
    FakeRagOutcome,
)
from .tasks import (
    FakeCallbackRecoveryPort,
    FakeProgressSnapshotPort,
    FakeProgressSubscriptionPort,
    FakeTaskReadPort,
)

__all__ = [
    "FakeChatConversationFactory",
    "FakeChatConversationPort",
    "FakeDocumentRagFactory",
    "FakeDocumentRagPort",
    "FakeDocumentRagSession",
    "FakeKnowledgeIndexFactory",
    "FakeKnowledgeIndexPort",
    "FakeRagOutcome",
    "FakeCallbackRecoveryPort",
    "FakeProgressSnapshotPort",
    "FakeProgressSubscriptionPort",
    "FakeTaskReadPort",
]
