"""业务 Step 续跑快照的最小共享契约。

通用 Task Control 只定义不可变身份和有界 canonical JSON 约束，不拥有任何业务表，
也不解释 ``payload``。Report、Weaponry、Analysis 各自在自己的组件 Schema 与 Adapter
中持久化并解析这些快照，避免把正文、凭据或供应商原始响应塞入根控制表。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Protocol, runtime_checkable

from app.modules.tasks.domain import TaskExecutionAuthority, TaskId
from .clock import require_persisted_utc


MAX_CONTINUATION_PAYLOAD_BYTES = 16 * 1024


def _sha256(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError(f"{name} 必须是 SHA-256")
    return normalized


def canonical_continuation_json(payload: Mapping[str, Any]) -> str:
    """序列化有界 JSON；拒绝 NaN、非字符串键和不可序列化对象。"""

    if not isinstance(payload, Mapping):
        raise TypeError("payload 必须是 Mapping")
    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("续跑 payload 必须是可序列化的有限 JSON object") from exc
    if len(encoded.encode("utf-8")) > MAX_CONTINUATION_PAYLOAD_BYTES:
        raise ValueError("续跑 payload 超过 16 KiB 上限")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise ValueError("续跑 payload 顶层必须是 JSON object")
    return encoded


@dataclass(frozen=True, slots=True)
class TaskStepContinuationDraft:
    """Workflow 在 Step intent 前形成的业务续跑引用草案。"""

    schema_version: int
    input_payload_fingerprint: str
    execution_profile_fingerprint: str
    payload: Mapping[str, Any]
    predecessor_checkpoint_digest: str = ""

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ValueError("schema_version 必须是正整数")
        object.__setattr__(
            self,
            "input_payload_fingerprint",
            _sha256(self.input_payload_fingerprint, name="input_payload_fingerprint"),
        )
        object.__setattr__(
            self,
            "execution_profile_fingerprint",
            _sha256(
                self.execution_profile_fingerprint,
                name="execution_profile_fingerprint",
            ),
        )
        predecessor = self.predecessor_checkpoint_digest.strip().lower()
        if predecessor:
            predecessor = _sha256(predecessor, name="predecessor_checkpoint_digest")
        object.__setattr__(self, "predecessor_checkpoint_digest", predecessor)
        canonical = canonical_continuation_json(self.payload)
        # 只保存 canonical JSON 解码后的普通对象，消除调用方后续修改 Mapping 的风险。
        object.__setattr__(self, "payload", json.loads(canonical))

    @property
    def payload_json(self) -> str:
        return canonical_continuation_json(self.payload)

    @property
    def payload_digest(self) -> str:
        return hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TaskStepContinuationSnapshot:
    """与一个不可变 Step Attempt 绑定的业务续跑快照。"""

    task_id: TaskId
    step_key: str
    step_attempt_no: int
    task_attempt_no: int
    task_fencing_token: int
    source_step_attempt_no: int
    draft: TaskStepContinuationDraft
    payload_digest: str
    created_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.step_key, str) or not self.step_key.strip():
            raise ValueError("step_key 不能为空")
        object.__setattr__(self, "step_key", self.step_key.strip())
        for name in ("step_attempt_no", "task_attempt_no", "task_fencing_token"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
        if type(self.source_step_attempt_no) is not int or self.source_step_attempt_no < 0:
            raise ValueError("source_step_attempt_no 必须是非负整数")
        if self.source_step_attempt_no >= self.step_attempt_no:
            raise ValueError("source_step_attempt_no 必须小于当前 step_attempt_no")
        if not isinstance(self.draft, TaskStepContinuationDraft):
            raise TypeError("draft 必须是 TaskStepContinuationDraft")
        digest = _sha256(self.payload_digest, name="payload_digest")
        if digest != self.draft.payload_digest:
            raise ValueError("payload_digest 与 canonical payload 不一致")
        object.__setattr__(self, "payload_digest", digest)
        object.__setattr__(
            self,
            "created_at",
            require_persisted_utc(self.created_at, name="created_at"),
        )


@runtime_checkable
class TaskStepContinuationStorePort(Protocol):
    """业务组件 UoW 内的追加式续跑快照 Store。"""

    def save(
        self,
        *,
        authority: TaskExecutionAuthority,
        step_key: str,
        step_attempt_no: int,
        source_step_attempt_no: int,
        draft: TaskStepContinuationDraft,
        created_at: str,
    ) -> TaskStepContinuationSnapshot: ...

    def get(
        self,
        task_id: TaskId,
        step_key: str,
        step_attempt_no: int,
    ) -> TaskStepContinuationSnapshot | None: ...


__all__ = [
    "MAX_CONTINUATION_PAYLOAD_BYTES",
    "TaskStepContinuationDraft",
    "TaskStepContinuationSnapshot",
    "TaskStepContinuationStorePort",
    "canonical_continuation_json",
]
