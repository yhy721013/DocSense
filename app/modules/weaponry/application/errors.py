"""武器谱 Application 的稳定失败分类。

这些异常只描述应用编排、任务事实与 Port 契约问题，不携带 Flask 状态码、供应商 URL、
请求正文或外部响应。公开 HTTP/Callback 映射分别由后续 Presenter 与本层终态规则完成。
"""

from __future__ import annotations


class WeaponryApplicationError(Exception):
    """武器谱应用层可稳定识别错误的共同基类。"""

    code = "weaponry_application_error"
    stage = "application"


class WeaponryTaskConflictError(WeaponryApplicationError):
    """同一 architectureId 的活动任务或 Callback Guard 阻止新受理。"""

    code = "weaponry_task_conflict"
    stage = "submission"


class WeaponryPortContractError(WeaponryApplicationError):
    """Adapter/Fake 返回对象、身份或调用顺序违反供应商无关 Port。"""

    code = "weaponry_port_contract_error"
    stage = "port_contract"


class WeaponryTaskPersistenceError(WeaponryApplicationError):
    """任务进度或终态写入结果不确定，禁止补写第二个终态。"""

    code = "weaponry_task_persistence_error"
    stage = "task_persistence"


class WeaponryAuditError(WeaponryApplicationError):
    """关键交互 reserve/complete 未可靠提交，禁止进入成功终态。"""

    code = "weaponry_audit_error"
    stage = "audit"


class WeaponryExecutionError(WeaponryApplicationError):
    """任务级准备或领域执行失败；对外只能投影兼容失败回调。"""

    code = "weaponry_execution_error"
    stage = "execution"

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message 必须是非空 str")
        if error_code is not None:
            if not isinstance(error_code, str) or not error_code.strip():
                raise ValueError("error_code 必须是非空 str 或 None")
            self.code = error_code.strip()
        super().__init__(message.strip())


class WeaponryStaleExecutionError(WeaponryApplicationError):
    """慢外部调用返回后发现 execution 已失去 latest 所有权。"""

    code = "weaponry_stale_execution"
    stage = "execution"


class WeaponryScenePreservationError(WeaponryExecutionError):
    """外部写副作用结果未知，任务失败但资源现场必须隔离等待对账。"""

    stage = "scene_preservation"


__all__ = [
    "WeaponryApplicationError",
    "WeaponryAuditError",
    "WeaponryExecutionError",
    "WeaponryPortContractError",
    "WeaponryScenePreservationError",
    "WeaponryStaleExecutionError",
    "WeaponryTaskConflictError",
    "WeaponryTaskPersistenceError",
]
