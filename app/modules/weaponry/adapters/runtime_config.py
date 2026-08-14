"""武器谱本地运行时的严格配置与固定策略构造。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os

from app.modules.tasks.http_deadlines import required_http_lease_seconds
from app.modules.weaponry.domain import (
    AUXILIARY_GUIDANCE_NONE,
    AUXILIARY_GUIDANCE_TERMS_RULES_COLUMN_COMPACT_V2,
    EVIDENCE_RANKING_STRATEGY,
    EVIDENCE_REFERENCE_FILTER_STRATEGY,
    EVIDENCE_SCORE_PROTOCOL,
    EVIDENCE_SCORE_SEMANTICS,
    EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
    EXTRACTION_PROMPT_VERSION,
    FILE_AGGREGATE_STRATEGY,
    MAX_TABLE_ROWS,
    RETRIEVAL_QUERY_VERSION,
    TABLE_MERGE_POLICY_VERSION,
    AuxiliaryGuidancePolicySnapshot,
    EvidenceSelectionPolicy,
    WeaponryExecutionPolicySnapshot,
)

from .production_profile import (
    WeaponryProductionSelectionProfileConfig,
    build_weaponry_production_selection_policy,
)


WEAPONRY_RUNTIME_MODE_SINGLE_INSTANCE = "single_instance"
_INSTALLED_EXTRACTION_MAX_ATTEMPTS = 2


class WeaponryRuntimeConfigurationError(RuntimeError):
    """武器谱运行配置与已安装适配器能力不一致。"""


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise WeaponryRuntimeConfigurationError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise WeaponryRuntimeConfigurationError(f"{name} 不能为空")
    return normalized


def _optional_text(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name=name)


def _positive_number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WeaponryRuntimeConfigurationError(
            f"{name} 必须是正有限数字"
        )
    normalized = float(value)
    if (
        normalized != normalized
        or normalized in (float("inf"), float("-inf"))
        or normalized <= 0.0
    ):
        raise WeaponryRuntimeConfigurationError(
            f"{name} 必须是正有限数字"
        )
    return normalized


def _positive_int(
    value: object,
    *,
    name: str,
    max_value: int = 1000,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WeaponryRuntimeConfigurationError(
            f"{name} 必须是 1~{max_value} 的整数"
        )
    if value < 1 or value > max_value:
        raise WeaponryRuntimeConfigurationError(
            f"{name} 必须是 1~{max_value} 的整数"
        )
    return value


@dataclass(frozen=True)
class WeaponryRuntimeConfig:
    """阶段 1D 的武器谱单实例运行清单。

    scan/batch/sweep 数值全部是每次轮询的有界工作量，不是业务积压上限。配置刻意没有
    Query 字符数、辅助语义词数量或 Selected Evidence 字符配额；合法请求若超过真实
    供应商能力，必须由 Adapter 明确记录供应商错误，不能在运行时静默截断。
    """

    runtime_mode: str
    scan_interval_seconds: float
    accepted_batch_size: int
    dispatch_failure_retry_seconds: float
    maintenance_interval_seconds: float
    maintenance_limit: int
    running_sample_limit: int
    stop_timeout_seconds: float
    cleanup_http_timeout_seconds: float
    cleanup_lease_seconds: float
    provider_fingerprint: str
    embedding_fingerprint: str
    document_processing_fingerprint: str
    extraction_model_fingerprint: str
    query_version: str = RETRIEVAL_QUERY_VERSION
    score_semantics: str = EVIDENCE_SCORE_SEMANTICS
    score_protocol: str = EVIDENCE_SCORE_PROTOCOL
    ranking_strategy: str = EVIDENCE_RANKING_STRATEGY
    reference_filter_strategy: str = EVIDENCE_REFERENCE_FILTER_STRATEGY
    extraction_context_strategy: str = (
        EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1
    )
    input_candidate_top_n: int = 8
    table_candidate_top_n: int = 16
    max_table_rows: int = MAX_TABLE_ROWS
    extraction_max_attempts: int = _INSTALLED_EXTRACTION_MAX_ATTEMPTS
    terms_rule_context_enabled: bool = False
    terms_workspace_name: str | None = None
    terms_dir: str | None = None
    terms_catalog_fingerprint: str | None = None
    terms_candidate_top_n: int = 0
    terms_max_context_chars: int = 0
    # 真实供应商联调工具生成的只读能力证明。缺失时允许开发环境启动，但生产就绪度
    # 必须保持 false，不能由配置指纹或 Fake 测试替代。
    production_attestation_path: str | None = None
    # 当前本地开发环境保持 false；Docker/生产进程必须显式设为 true，使无效证明在组合根
    # 构造阶段 fail-fast，而不再依赖部署人员记得调用只读 readiness 方法。
    production_gate_required: bool = False

    def __post_init__(self) -> None:
        mode = str(self.runtime_mode or "").strip().lower()
        if mode != WEAPONRY_RUNTIME_MODE_SINGLE_INSTANCE:
            raise WeaponryRuntimeConfigurationError(
                "当前仅安装 single_instance 武器谱 Dispatcher；多实例必须等待"
                "共享数据库、可靠队列和分布式执行租约完成"
            )
        object.__setattr__(self, "runtime_mode", mode)

        for name in (
            "scan_interval_seconds",
            "dispatch_failure_retry_seconds",
            "maintenance_interval_seconds",
            "stop_timeout_seconds",
            "cleanup_http_timeout_seconds",
            "cleanup_lease_seconds",
        ):
            object.__setattr__(
                self,
                name,
                _positive_number(getattr(self, name), name=name),
            )
        for name in (
            "accepted_batch_size",
            "maintenance_limit",
            "running_sample_limit",
            "input_candidate_top_n",
            "table_candidate_top_n",
        ):
            object.__setattr__(
                self,
                name,
                _positive_int(getattr(self, name), name=name),
            )
        required_lease = required_http_lease_seconds(
            self.cleanup_http_timeout_seconds
        )
        if self.cleanup_lease_seconds < required_lease:
            raise WeaponryRuntimeConfigurationError(
                "cleanup_lease_seconds 必须覆盖连接、响应读取和安全余量，"
                f"当前至少需要 {required_lease:.3f} 秒"
            )

        for name in (
            "provider_fingerprint",
            "embedding_fingerprint",
            "document_processing_fingerprint",
            "extraction_model_fingerprint",
            "query_version",
            "score_semantics",
            "score_protocol",
            "ranking_strategy",
            "reference_filter_strategy",
            "extraction_context_strategy",
        ):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name=name),
            )
        expected_values = {
            "query_version": RETRIEVAL_QUERY_VERSION,
            "score_semantics": EVIDENCE_SCORE_SEMANTICS,
            "score_protocol": EVIDENCE_SCORE_PROTOCOL,
            "ranking_strategy": EVIDENCE_RANKING_STRATEGY,
            "reference_filter_strategy": EVIDENCE_REFERENCE_FILTER_STRATEGY,
            # 当前唯一已安装的生产 Extraction Adapter 只接受 Provided-Evidence。
            # evidence_only_context_v1 只有在供应商能力验证和 Adapter 落地后才能开放。
            "extraction_context_strategy": (
                EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1
            ),
        }
        for name, expected in expected_values.items():
            if getattr(self, name) != expected:
                raise WeaponryRuntimeConfigurationError(
                    f"{name} 与当前已安装策略不一致"
                )
        if self.max_table_rows != MAX_TABLE_ROWS:
            raise WeaponryRuntimeConfigurationError(
                f"max_table_rows 当前必须固定为 {MAX_TABLE_ROWS}"
            )
        if self.extraction_max_attempts != _INSTALLED_EXTRACTION_MAX_ATTEMPTS:
            raise WeaponryRuntimeConfigurationError(
                "extraction_max_attempts 与当前字段执行器固定重试策略不一致"
            )
        if not isinstance(self.terms_rule_context_enabled, bool):
            raise WeaponryRuntimeConfigurationError(
                "terms_rule_context_enabled 必须是 bool"
            )
        object.__setattr__(
            self,
            "production_attestation_path",
            _optional_text(
                self.production_attestation_path,
                name="production_attestation_path",
            ),
        )
        if not isinstance(self.production_gate_required, bool):
            raise WeaponryRuntimeConfigurationError(
                "production_gate_required 必须是 bool"
            )

        if not self.terms_rule_context_enabled:
            # 关闭状态必须是一个真正的零术语配置：既不允许携带目录/workspace，也不为
            # “将来可能开启”预留 Top-N 或字符数，避免组合根误装 Terms Adapter。
            if any(
                value is not None
                for value in (
                    self.terms_workspace_name,
                    self.terms_dir,
                    self.terms_catalog_fingerprint,
                )
            ) or self.terms_candidate_top_n != 0 or self.terms_max_context_chars != 0:
                raise WeaponryRuntimeConfigurationError(
                    "术语辅助关闭时不得携带术语 workspace、目录、指纹或配额"
                )
            return

        for name in ("terms_workspace_name", "terms_dir"):
            object.__setattr__(
                self,
                name,
                _optional_text(getattr(self, name), name=name),
            )
            if getattr(self, name) is None:
                raise WeaponryRuntimeConfigurationError(
                    f"术语辅助启用时 {name} 不能为空"
                )
        object.__setattr__(
            self,
            "terms_catalog_fingerprint",
            _optional_text(
                self.terms_catalog_fingerprint,
                name="terms_catalog_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "terms_candidate_top_n",
            _positive_int(
                self.terms_candidate_top_n,
                name="terms_candidate_top_n",
            ),
        )
        object.__setattr__(
            self,
            "terms_max_context_chars",
            _positive_int(
                self.terms_max_context_chars,
                name="terms_max_context_chars",
                max_value=10_000_000,
            ),
        )


@dataclass(frozen=True)
class WeaponryRuntimeCapabilities:
    """组合根声明的已安装 Adapter 能力，而非从任务失败中动态猜测。"""

    provider_fingerprint: str
    embedding_fingerprint: str
    document_processing_fingerprint: str
    extraction_model_fingerprint: str
    query_version: str
    score_semantics: str
    score_protocol: str
    ranking_strategy: str
    reference_filter_strategy: str
    extraction_context_strategy: str

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name=name),
            )

@dataclass(frozen=True)
class WeaponryRuntimePolicies:
    """组合根固定注入 Parser/Submit 的三组不可变执行策略。"""

    evidence_selection: EvidenceSelectionPolicy
    execution: WeaponryExecutionPolicySnapshot
    auxiliary_guidance: AuxiliaryGuidancePolicySnapshot


def validate_weaponry_runtime_capabilities(
    config: WeaponryRuntimeConfig,
    capabilities: WeaponryRuntimeCapabilities,
) -> None:
    """启动前逐项比对配置和真实/Fake Adapter 能力，漂移即 fail-fast。"""

    if not isinstance(config, WeaponryRuntimeConfig):
        raise TypeError("config 必须是 WeaponryRuntimeConfig")
    if not isinstance(capabilities, WeaponryRuntimeCapabilities):
        raise TypeError("capabilities 必须是 WeaponryRuntimeCapabilities")
    for name in capabilities.__dataclass_fields__:
        configured = getattr(config, name)
        installed = getattr(capabilities, name)
        if configured != installed:
            raise WeaponryRuntimeConfigurationError(
                f"武器谱运行能力不匹配: component={name}"
            )


def build_weaponry_runtime_policies(
    config: WeaponryRuntimeConfig,
) -> WeaponryRuntimePolicies:
    """从一次启动配置构造唯一策略；运行中不读取环境变量或切换 profile。"""

    if not isinstance(config, WeaponryRuntimeConfig):
        raise TypeError("config 必须是 WeaponryRuntimeConfig")
    selection = build_weaponry_production_selection_policy(
        WeaponryProductionSelectionProfileConfig(
            provider_fingerprint=config.provider_fingerprint,
            embedding_fingerprint=config.embedding_fingerprint,
            document_processing_fingerprint=(
                config.document_processing_fingerprint
            ),
            input_candidate_top_n=config.input_candidate_top_n,
            table_candidate_top_n=config.table_candidate_top_n,
            reject_reference_like=True,
            reference_filter_strategy=config.reference_filter_strategy,
        )
    )
    execution = WeaponryExecutionPolicySnapshot(
        extraction_strategy=FILE_AGGREGATE_STRATEGY,
        extraction_prompt_version=EXTRACTION_PROMPT_VERSION,
        extraction_context_strategy=config.extraction_context_strategy,
        extraction_model_fingerprint=config.extraction_model_fingerprint,
        table_merge_policy_version=TABLE_MERGE_POLICY_VERSION,
        max_table_rows=config.max_table_rows,
    )
    if config.terms_rule_context_enabled:
        if not config.terms_catalog_fingerprint:
            raise WeaponryRuntimeConfigurationError(
                "术语辅助策略只能在本地目录自动指纹冻结后构造"
            )
        auxiliary = AuxiliaryGuidancePolicySnapshot(
            policy_id=AUXILIARY_GUIDANCE_TERMS_RULES_COLUMN_COMPACT_V2,
            catalog_fingerprint=config.terms_catalog_fingerprint or "",
            top_n=config.terms_candidate_top_n,
            max_context_chars=config.terms_max_context_chars,
        )
    else:
        auxiliary = AuxiliaryGuidancePolicySnapshot(
            policy_id=AUXILIARY_GUIDANCE_NONE,
            catalog_fingerprint="",
            top_n=0,
            max_context_chars=0,
        )
    return WeaponryRuntimePolicies(selection, execution, auxiliary)


def _strict_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name, str(default))
    try:
        return float(raw.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise WeaponryRuntimeConfigurationError(
            f"{name} 必须是正有限数字"
        ) from exc


def _strict_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, str(default))
    try:
        return int(raw.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise WeaponryRuntimeConfigurationError(
            f"{name} 必须是 1~1000 的整数"
        ) from exc


def _strict_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise WeaponryRuntimeConfigurationError(
            f"{name} 必须是 true 或 false"
        )
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise WeaponryRuntimeConfigurationError(
        f"{name} 必须是 true 或 false"
    )


def _required_env(environ: Mapping[str, str], name: str) -> str:
    return _required_text(environ.get(name), name=name)


def load_weaponry_runtime_config(
    environ: Mapping[str, str] | None = None,
) -> WeaponryRuntimeConfig:
    """严格读取阶段 1D 配置；术语关闭时不读取任何术语专属键。

    ``environ`` 参数使离线测试可以注入带访问审计的 Mapping，直接证明 false 路径没有
    解析 workspace、目录或目录指纹。生产调用省略该参数即可读取 ``os.environ``。
    """

    source = os.environ if environ is None else environ
    legacy_mode = source.get("WEAPONRY_ANALYSE_MODE")
    if legacy_mode is not None:
        normalized_mode = _required_text(
            legacy_mode,
            name="WEAPONRY_ANALYSE_MODE",
        )
        if normalized_mode == "1":
            raise WeaponryRuntimeConfigurationError(
                "WEAPONRY_ANALYSE_MODE=1 已废弃，只允许固定 file_aggregate_v1"
            )
        if normalized_mode != "2":
            raise WeaponryRuntimeConfigurationError(
                "WEAPONRY_ANALYSE_MODE 已删除；迁移期只允许显式值 2"
            )

    terms_enabled = _strict_bool(
        source,
        "WEAPONRY_TERMS_RULE_CONTEXT_ENABLED",
        False,
    )
    terms_values: dict[str, object]
    if terms_enabled:
        # 只有 true 分支允许触碰这些键；不要把三个 get 提到公共字典或默认参数中。
        terms_values = {
            "terms_workspace_name": source.get(
                "WEAPONRY_TERMS_WORKSPACE_NAME",
                "weaponry-terms-rules",
            ),
            "terms_dir": source.get("WEAPONRY_TERMS_DIR", "terms"),
            # 指纹不再从环境变量读取；生产组合根根据本地术语卡内容自动计算并冻结。
            "terms_catalog_fingerprint": None,
            "terms_candidate_top_n": _strict_int(
                source,
                "DOCSENSE_WEAPONRY_TERMS_CANDIDATE_TOP_N",
                5,
            ),
            "terms_max_context_chars": _strict_int(
                source,
                "DOCSENSE_WEAPONRY_TERMS_MAX_CONTEXT_CHARS",
                1200,
            ),
        }
    else:
        terms_values = {
            "terms_workspace_name": None,
            "terms_dir": None,
            "terms_catalog_fingerprint": None,
            "terms_candidate_top_n": 0,
            "terms_max_context_chars": 0,
        }

    return WeaponryRuntimeConfig(
        runtime_mode=source.get(
            "DOCSENSE_WEAPONRY_RUNTIME_MODE",
            WEAPONRY_RUNTIME_MODE_SINGLE_INSTANCE,
        ),
        scan_interval_seconds=_strict_float(
            source,
            "DOCSENSE_WEAPONRY_SCAN_INTERVAL_SECONDS",
            1.0,
        ),
        accepted_batch_size=_strict_int(
            source,
            "DOCSENSE_WEAPONRY_ACCEPTED_BATCH_SIZE",
            50,
        ),
        dispatch_failure_retry_seconds=_strict_float(
            source,
            "DOCSENSE_WEAPONRY_DISPATCH_FAILURE_RETRY_SECONDS",
            30.0,
        ),
        maintenance_interval_seconds=_strict_float(
            source,
            "DOCSENSE_WEAPONRY_MAINTENANCE_INTERVAL_SECONDS",
            30.0,
        ),
        maintenance_limit=_strict_int(
            source,
            "DOCSENSE_WEAPONRY_MAINTENANCE_LIMIT",
            50,
        ),
        running_sample_limit=_strict_int(
            source,
            "DOCSENSE_WEAPONRY_RUNNING_SAMPLE_LIMIT",
            20,
        ),
        stop_timeout_seconds=_strict_float(
            source,
            "DOCSENSE_WEAPONRY_STOP_TIMEOUT_SECONDS",
            5.0,
        ),
        cleanup_http_timeout_seconds=_strict_float(
            source,
            "DOCSENSE_WEAPONRY_CLEANUP_HTTP_TIMEOUT_SECONDS",
            60.0,
        ),
        cleanup_lease_seconds=_strict_float(
            source,
            "DOCSENSE_WEAPONRY_CLEANUP_LEASE_SECONDS",
            130.0,
        ),
        provider_fingerprint=_required_env(
            source,
            "DOCSENSE_WEAPONRY_PROVIDER_FINGERPRINT",
        ),
        embedding_fingerprint=_required_env(
            source,
            "DOCSENSE_WEAPONRY_EMBEDDING_FINGERPRINT",
        ),
        document_processing_fingerprint=_required_env(
            source,
            "DOCSENSE_WEAPONRY_DOCUMENT_PROCESSING_FINGERPRINT",
        ),
        extraction_model_fingerprint=_required_env(
            source,
            "DOCSENSE_WEAPONRY_EXTRACTION_MODEL_FINGERPRINT",
        ),
        query_version=source.get(
            "DOCSENSE_WEAPONRY_QUERY_VERSION",
            RETRIEVAL_QUERY_VERSION,
        ),
        score_semantics=source.get(
            "DOCSENSE_WEAPONRY_SCORE_SEMANTICS",
            EVIDENCE_SCORE_SEMANTICS,
        ),
        score_protocol=source.get(
            "DOCSENSE_WEAPONRY_SCORE_PROTOCOL",
            EVIDENCE_SCORE_PROTOCOL,
        ),
        ranking_strategy=source.get(
            "DOCSENSE_WEAPONRY_RANKING_STRATEGY",
            EVIDENCE_RANKING_STRATEGY,
        ),
        reference_filter_strategy=source.get(
            "DOCSENSE_WEAPONRY_REFERENCE_FILTER_STRATEGY",
            EVIDENCE_REFERENCE_FILTER_STRATEGY,
        ),
        extraction_context_strategy=source.get(
            "DOCSENSE_WEAPONRY_EXTRACTION_CONTEXT_STRATEGY",
            EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
        ),
        input_candidate_top_n=_strict_int(
            source,
            "DOCSENSE_WEAPONRY_INPUT_CANDIDATE_TOP_N",
            8,
        ),
        table_candidate_top_n=_strict_int(
            source,
            "DOCSENSE_WEAPONRY_TABLE_CANDIDATE_TOP_N",
            16,
        ),
        max_table_rows=_strict_int(
            source,
            "DOCSENSE_WEAPONRY_MAX_TABLE_ROWS",
            MAX_TABLE_ROWS,
        ),
        extraction_max_attempts=_strict_int(
            source,
            "DOCSENSE_WEAPONRY_EXTRACTION_MAX_ATTEMPTS",
            _INSTALLED_EXTRACTION_MAX_ATTEMPTS,
        ),
        terms_rule_context_enabled=terms_enabled,
        production_attestation_path=_optional_text(
            source.get("DOCSENSE_WEAPONRY_PRODUCTION_ATTESTATION_PATH"),
            name="DOCSENSE_WEAPONRY_PRODUCTION_ATTESTATION_PATH",
        ),
        production_gate_required=_strict_bool(
            source,
            "DOCSENSE_WEAPONRY_REQUIRE_PRODUCTION_GATE",
            False,
        ),
        **terms_values,
    )


__all__ = [
    "WEAPONRY_RUNTIME_MODE_SINGLE_INSTANCE",
    "WeaponryRuntimeConfig",
    "WeaponryRuntimeConfigurationError",
    "WeaponryRuntimeCapabilities",
    "WeaponryRuntimePolicies",
    "build_weaponry_runtime_policies",
    "load_weaponry_runtime_config",
    "validate_weaponry_runtime_capabilities",
]
