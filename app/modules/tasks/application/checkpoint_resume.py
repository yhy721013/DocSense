"""恢复执行开始精确目标 Step Attempt 的共享门禁。"""

from __future__ import annotations

from app.modules.tasks.domain import TaskStep, TaskStepState
from app.modules.tasks.ports import TaskWorkflowContextPort


class CheckpointResumeError(RuntimeError):
    """冻结恢复计划与 Workflow 当前推进位置不一致。"""


def expected_retry_step(
    context: TaskWorkflowContextPort,
    *,
    step_key: str,
    idempotency_key: str,
    definition: object,
) -> TaskStep | None:
    """返回唯一可新建 Step Attempt 的 pending 投影；普通执行返回 ``None``。

    Workflow 在恢复模式下必须先通过业务 Resolver 跳过并还原全部前置步骤，因此它第一次
    调用 ``begin`` 时只能是 ``retry_from_step_key``。若仍从头调用，本函数会在任何新的
    Step Intent 或事务外副作用之前失败关闭。
    """

    loaded = context.loaded_input
    retry_from = loaded.retry_from_step_key
    if not retry_from:
        return None
    if step_key != retry_from:
        raise CheckpointResumeError(
            "恢复 Workflow 未从 retry_from_step_key 开始，禁止从头重放"
        )
    expected = next(
        (item for item in loaded.recovery_steps if item.step_key == retry_from),
        None,
    )
    if expected is None or expected.state is not TaskStepState.PENDING:
        raise CheckpointResumeError("恢复目标 Step 必须是 Decision 解锁的 pending 投影")
    if expected.idempotency_key != idempotency_key:
        raise CheckpointResumeError("恢复目标 Step 幂等键与冻结投影不一致")
    if (
        expected.definition_version != getattr(definition, "definition_version", None)
        or expected.effect_kind is not getattr(definition, "effect_kind", None)
        or expected.replay_policy is not getattr(definition, "replay_policy", None)
    ):
        raise CheckpointResumeError("恢复目标 Step Registry 身份漂移")
    return expected


__all__ = ["CheckpointResumeError", "expected_retry_step"]
