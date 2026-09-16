"""报告领域可稳定识别的错误类型。

本模块只描述错误的业务分类，不负责把异常映射为 HTTP 状态码、回调状态或日志文本。
这些映射分别属于 Web Presenter、报告应用服务和基础设施 Adapter。通过稳定的 ``code``
与 ``stage``，后续接入本地 Dispatcher、Celery Worker 或 MySQL Repository 时无需依赖
具体异常实现，也不会把供应商异常直接泄漏到业务层。
"""

from __future__ import annotations


class ReportError(Exception):
    """所有报告业务错误的共同基类。"""

    code = "report_error"
    stage = "report"


class ReportDomainValidationError(ReportError, ValueError):
    """已经进入领域层的内部值不满足不变量。

    该异常不等价于公开 HTTP 400。Web Adapter 如何处理尚未覆盖的遗留异常输入，必须
    依据接口契约单独决定，不能因为领域对象更严格就擅自收紧公开参数语义。
    """

    code = "report_domain_validation"
    stage = "domain"


class ReportTaskConflictError(ReportError):
    """同一报告业务键当前不允许创建新的执行。"""

    code = "report_task_conflict"
    stage = "submission"


class ReportStaleExecutionError(ReportError):
    """当前执行已不是该报告业务键的最新拥有者。"""

    code = "report_stale_execution"
    stage = "execution"


class ReportInputError(ReportError):
    """报告源文件下载、读取或输入准备失败。"""

    code = "report_input_error"
    stage = "input"


class ReportTemplateError(ReportError):
    """报告模板下载、解析或内容校验失败。"""

    code = "report_template_error"
    stage = "template"


class ReportRagError(ReportError):
    """多文档 RAG 准备、查询或结果处理失败。"""

    code = "report_rag_error"
    stage = "rag"


class ReportArtifactError(ReportError):
    """报告输入或输出 Artifact 的持久化与读取失败。"""

    code = "report_artifact_error"
    stage = "artifact"


class ReportSourceNormalizationError(ReportInputError):
    """单个源文件规范化失败；兼容规则允许回退到原下载 Artifact。"""

    code = "report_source_normalization_error"
    stage = "normalization"


class ReportAuditError(ReportError):
    """完整 RAG 交互轨迹未能通过原子审计门禁。"""

    code = "report_audit_error"
    stage = "audit"


class ReportCallbackError(ReportError):
    """回调 Guard、投递或 outcome 持久化违反内部协议。"""

    code = "report_callback_error"
    stage = "callback"


class ReportCleanupError(ReportError):
    """业务终态之后的临时资源清理未完成。"""

    code = "report_cleanup_error"
    stage = "cleanup"


class ReportResourceConcurrencyError(ReportCleanupError):
    """资源恢复事实的乐观锁未命中，调用方必须重读最新版本后再决策。"""

    code = "report_resource_concurrency_error"
    stage = "cleanup_concurrency"


class ReportResourceNotReadyError(ReportCleanupError):
    """资源记录对应的 execution 尚未形成可判定所有权的终态。"""

    code = "report_resource_not_ready"
    stage = "cleanup_prepare"


class ReportPortContractError(ReportError):
    """某个 Adapter/Fake 返回了与抽象端口不一致的对象或身份。"""

    code = "report_port_contract_error"
    stage = "application"


class ReportTaskPersistenceError(ReportError):
    """任务事实进度/终态写入失败，提交结果可能不确定，禁止改写为业务失败。"""

    code = "report_task_persistence_error"
    stage = "task_persistence"


__all__ = [
    "ReportArtifactError",
    "ReportAuditError",
    "ReportCallbackError",
    "ReportCleanupError",
    "ReportDomainValidationError",
    "ReportError",
    "ReportInputError",
    "ReportRagError",
    "ReportPortContractError",
    "ReportResourceConcurrencyError",
    "ReportResourceNotReadyError",
    "ReportSourceNormalizationError",
    "ReportTaskPersistenceError",
    "ReportStaleExecutionError",
    "ReportTaskConflictError",
    "ReportTemplateError",
]
