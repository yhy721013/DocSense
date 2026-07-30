"""供离线业务测试复用的端口替身。

本包中的实现只存在于测试代码，不访问网络、数据库或业务文件。生产代码不得导入本包；
测试通过应用层 Protocol 注入这些对象，以验证业务编排而不是具体外部协议。
"""

from .chat import FakeChatConversationFactory, FakeChatConversationPort
from .analysis import (
    AnalysisFakeExpectation,
    StrictAnalysisFakeScript,
    StrictAnalysisPortFake,
)
from .knowledge_index import FakeKnowledgeIndexFactory, FakeKnowledgeIndexPort
from .rag import (
    FakeDocumentRagFactory,
    FakeDocumentRagPort,
    FakeDocumentRagSession,
    FakeRagOutcome,
)
from .reassign import (
    FakeReassignmentKnowledgePort,
    FakeReassignmentKnowledgePortFactory,
    FakeReassignmentRepository,
    FakeReassignmentUnitOfWork,
    PostCommitFailureReassignmentRepository,
)
from .report import (
    FakeProgressPublisherPort,
    FakeReportArtifactPort,
    FakeReportAuditPort,
    FakeReportCallbackPort,
    FakeReportDispatcherPort,
    FakeReportFilePort,
    FakeReportRagPort,
    FakeReportResourceStorePort,
    FakeReportTaskCommandPort,
    InvocationRecorder,
    sample_failed_report_trace,
    sample_report_trace,
)
from .tasks import (
    FakeCallbackRecoveryCommandPort,
    FakeCallbackRecoveryPort,
    FakeProgressSnapshotPort,
    FakeProgressSubscriptionPort,
    FakeTaskReadPort,
)
from .weaponry import (
    FakeAuxiliaryGuidancePort,
    FakeEvidenceExtractionPort,
    FakeTargetEvidenceRetrievalPort,
    FakeWeaponryCallbackPort,
    FakeWeaponryDispatcherPort,
    FakeWeaponryDocumentScopePort,
    FakeWeaponryExternalResourceCleanupPort,
    FakeWeaponryInteractionAuditPort,
    FakeWeaponryProgressPublisherPort,
    FakeWeaponryResourceStorePort,
    FakeWeaponryTaskCommandPort,
    FakeWeaponryTranslationPort,
    WeaponryInvocation,
    WeaponryInvocationRecorder,
)

__all__ = [
    "AnalysisFakeExpectation",
    "FakeChatConversationFactory",
    "FakeChatConversationPort",
    "FakeDocumentRagFactory",
    "FakeDocumentRagPort",
    "FakeDocumentRagSession",
    "FakeKnowledgeIndexFactory",
    "FakeKnowledgeIndexPort",
    "FakeRagOutcome",
    "FakeReassignmentKnowledgePort",
    "FakeReassignmentKnowledgePortFactory",
    "FakeReassignmentRepository",
    "FakeReassignmentUnitOfWork",
    "PostCommitFailureReassignmentRepository",
    "FakeProgressPublisherPort",
    "FakeReportArtifactPort",
    "FakeReportAuditPort",
    "FakeReportCallbackPort",
    "FakeReportDispatcherPort",
    "FakeReportFilePort",
    "FakeReportRagPort",
    "FakeReportResourceStorePort",
    "FakeReportTaskCommandPort",
    "FakeCallbackRecoveryCommandPort",
    "FakeCallbackRecoveryPort",
    "FakeProgressSnapshotPort",
    "FakeProgressSubscriptionPort",
    "FakeTaskReadPort",
    "FakeAuxiliaryGuidancePort",
    "FakeEvidenceExtractionPort",
    "FakeTargetEvidenceRetrievalPort",
    "FakeWeaponryCallbackPort",
    "FakeWeaponryDispatcherPort",
    "FakeWeaponryDocumentScopePort",
    "FakeWeaponryExternalResourceCleanupPort",
    "FakeWeaponryInteractionAuditPort",
    "FakeWeaponryProgressPublisherPort",
    "FakeWeaponryResourceStorePort",
    "FakeWeaponryTaskCommandPort",
    "FakeWeaponryTranslationPort",
    "InvocationRecorder",
    "WeaponryInvocation",
    "WeaponryInvocationRecorder",
    "sample_failed_report_trace",
    "sample_report_trace",
    "StrictAnalysisFakeScript",
    "StrictAnalysisPortFake",
]
