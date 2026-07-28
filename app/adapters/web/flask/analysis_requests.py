"""``POST /llm/analysis`` 的 Flask 入站解析器。

本模块逐项复刻已冻结的入站校验和错误文案，但不读取数据库、不创建线程，也不发布
Progress。路由只将解析后的不可变请求交给 Application 的原子受理用例，不能在这里添加
任务预查、外部 I/O 或公开字段。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.modules.analysis.domain.architecture_tree import (
    ArchitectureTreeValidationError,
)
from app.modules.analysis.domain.errors import AnalysisContractError
from app.modules.analysis.domain.models import (
    MAX_ANALYSIS_PARAMS_PER_REQUEST,
    MAX_ANALYSIS_REQUEST_BYTES,
)
from app.modules.analysis.domain.ranges import validate_analysis_architecture_ranges
from app.modules.analysis.domain.task_inputs import (
    ANALYSIS_BUSINESS_TYPE,
    AnalysisDocumentProcessingPolicySnapshot,
    AnalysisPolicySnapshot,
    AnalysisSubmissionSnapshot,
    FrozenJsonArray,
    FrozenJsonObject,
)
from app.modules.analysis.ports.batch_commands import AnalysisBatchCommand


logger = logging.getLogger(__name__)


class _AnalysisFlaskRequest(Protocol):
    """避免解析器依赖 Flask 具体类型的最小请求形状。"""

    content_length: int | None
    stream: Any

    def get_json(self, *, silent: bool = False) -> object:
        ...


class AnalysisRequestValidationError(ValueError):
    """文件分析请求违反已冻结公开合同时的可呈现错误。"""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ParsedAnalysisRequest:
    """完成公开校验后的不可变请求投影，不携带任务或 execution 身份。"""

    request_projection: FrozenJsonObject
    params: tuple[FrozenJsonObject, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request_projection, FrozenJsonObject):
            raise TypeError("request_projection 必须是 FrozenJsonObject")
        params = tuple(self.params)
        if not params or any(not isinstance(item, FrozenJsonObject) for item in params):
            raise ValueError("params 必须是非空 FrozenJsonObject 序列")
        projection_params = self.request_projection.get("params")
        if not isinstance(projection_params, FrozenJsonArray) or (
            projection_params.values != params
        ):
            raise ValueError("request_projection.params 与 params 不一致")
        object.__setattr__(self, "params", params)

    def to_batch_command(
        self,
        *,
        policy_snapshot: AnalysisPolicySnapshot,
        trace_id: str,
        legacy_office_allowed_version_series: str = "26.2",
    ) -> AnalysisBatchCommand:
        """注入已冻结的运行策略后，构造后续事务 Adapter 可受理的内部命令。

        ``policy_snapshot`` 必须由路由受理点从已验证容器配置读取一次，Worker 不得重新读取
        环境变量。该方法不写任务表、不创建线程，也不改变任何 HTTP 输出。
        """

        if not isinstance(policy_snapshot, AnalysisPolicySnapshot):
            raise TypeError("policy_snapshot 必须是 AnalysisPolicySnapshot")
        submissions = tuple(
            AnalysisSubmissionSnapshot.from_frozen_params(
                params,
                policy_snapshot=policy_snapshot,
                document_processing_policy=(
                    AnalysisDocumentProcessingPolicySnapshot.for_source(
                        str(params.get("filePath") or ""),
                        business_file_name=str(params.get("fileName") or ""),
                        allowed_version_series=legacy_office_allowed_version_series,
                    )
                ),
            )
            for params in self.params
        )
        return AnalysisBatchCommand(
            request_projection=self.request_projection,
            submissions=submissions,
            trace_id=trace_id,
        )


def parse_analysis_flask_request(
    request: _AnalysisFlaskRequest,
) -> ParsedAnalysisRequest:
    """从 Flask 兼容请求对象读取有界 JSON，再委托纯 payload 校验。

    此函数刻意保持旧路由对 malformed JSON 的行为：普通 JSONDecodeError 被降为 ``{}``，
    随后仍按 ``businessType必须为file`` 返回；只有无法安全解析的结构异常才返回既有的
    ``请求JSON格式无效``。这不是新接口设计，不能在本阶段自行“优化”。
    """

    content_length = request.content_length
    if content_length is not None and content_length > MAX_ANALYSIS_REQUEST_BYTES:
        logger.warning(
            "文件分析请求被拒绝: 请求体过大 content_length=%s limit=%s",
            content_length,
            MAX_ANALYSIS_REQUEST_BYTES,
        )
        raise AnalysisRequestValidationError("请求体过大", status_code=413)
    try:
        if content_length is None:
            raw_body = request.stream.read(MAX_ANALYSIS_REQUEST_BYTES + 1)
            if len(raw_body) > MAX_ANALYSIS_REQUEST_BYTES:
                logger.warning(
                    "文件分析请求被拒绝: 无Content-Length请求体过大 body_bytes>%s",
                    MAX_ANALYSIS_REQUEST_BYTES,
                )
                raise AnalysisRequestValidationError("请求体过大", status_code=413)
            payload = json.loads(raw_body) if raw_body else {}
        else:
            payload = request.get_json(silent=True)
            if payload is None:
                payload = {}
    except AnalysisRequestValidationError:
        raise
    except json.JSONDecodeError:
        payload = {}
    except (RecursionError, UnicodeError, ValueError) as exc:
        logger.warning(
            "文件分析请求被拒绝: JSON结构无法安全解析 error_type=%s",
            type(exc).__name__,
        )
        raise AnalysisRequestValidationError("请求JSON格式无效") from exc
    return parse_analysis_payload(payload)


def parse_analysis_payload(payload: object) -> ParsedAnalysisRequest:
    """校验公开 payload，并保留与旧路由相同的字段、顺序和错误优先级。"""

    if not isinstance(payload, dict):
        logger.warning(
            "文件分析请求被拒绝: JSON顶层不是对象 type=%s",
            type(payload).__name__,
        )
        raise AnalysisRequestValidationError("请求JSON必须是对象")
    try:
        # 旧路由先证明对象可表示为严格 JSON，再进入业务字段校验。不能因冻结 DTO
        # 本身可接受某些 Python 对象，就放宽已存在的 NaN/Unicode 拒绝边界。
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        logger.warning(
            "文件分析请求被拒绝: JSON含非有限数值或非法Unicode error_type=%s",
            type(exc).__name__,
        )
        raise AnalysisRequestValidationError(
            "请求JSON包含非法数值或Unicode字符"
        ) from exc
    if payload.get("businessType") != ANALYSIS_BUSINESS_TYPE:
        logger.warning("文件分析请求被拒绝: businessType无效")
        raise AnalysisRequestValidationError("businessType必须为file")

    raw_params = payload.get("params")
    if not isinstance(raw_params, list) or not raw_params:
        logger.warning("文件分析请求被拒绝: params为空或格式无效")
        raise AnalysisRequestValidationError("params不能为空")
    if len(raw_params) > MAX_ANALYSIS_PARAMS_PER_REQUEST:
        logger.warning(
            "文件分析请求被拒绝: params数量过多 count=%d limit=%d",
            len(raw_params),
            MAX_ANALYSIS_PARAMS_PER_REQUEST,
        )
        raise AnalysisRequestValidationError(
            f"params数量不能超过{MAX_ANALYSIS_PARAMS_PER_REQUEST}"
        )
    for index, item in enumerate(raw_params):
        if not isinstance(item, dict):
            logger.warning("文件分析请求被拒绝: params项不是对象 index=%d", index)
            raise AnalysisRequestValidationError(f"params[{index}]必须是对象")

    seen_file_names: set[str] = set()
    for index, params in enumerate(raw_params):
        file_name = params.get("fileName")
        if not isinstance(file_name, str) or not file_name.strip():
            logger.warning("文件分析请求被拒绝: fileName为空 index=%d", index)
            raise AnalysisRequestValidationError("fileName不能为空")
        normalized_name = file_name.strip()
        if normalized_name in seen_file_names:
            logger.warning(
                "文件分析请求被拒绝: fileName重复 file_name=%s index=%d",
                normalized_name,
                index,
            )
            raise AnalysisRequestValidationError("fileName不能重复")
        seen_file_names.add(normalized_name)

        file_path = params.get("filePath")
        if not isinstance(file_path, str) or not file_path.strip():
            logger.warning("文件分析请求被拒绝: filePath为空 index=%d", index)
            raise AnalysisRequestValidationError("filePath不能为空")
        try:
            validate_analysis_architecture_ranges(params)
        except ArchitectureTreeValidationError as exc:
            logger.warning(
                "文件分析请求被拒绝: 领域树无效 index=%d error=%s",
                index,
                exc,
            )
            raise AnalysisRequestValidationError(f"params[{index}]: {exc}") from exc

    try:
        request_projection = FrozenJsonObject.from_mapping(
            payload,
            name="analysis_request",
        )
        frozen_params = request_projection.get("params")
        if not isinstance(frozen_params, FrozenJsonArray) or any(
            not isinstance(item, FrozenJsonObject) for item in frozen_params.values
        ):
            raise AnalysisContractError("analysis_request.params 冻结形状无效")
        params = tuple(frozen_params.values)
    except AnalysisContractError as exc:
        # 上面的严格 JSON 校验已覆盖正常入口；该分支仅为防止未来改动绕过冻结边界，
        # 且必须映射回当前公开错误，而非泄漏内部异常。
        logger.warning(
            "文件分析请求冻结失败: error_type=%s",
            type(exc).__name__,
        )
        raise AnalysisRequestValidationError("请求JSON包含非法数值或Unicode字符") from exc
    logger.info("文件分析请求已完成公开解析: param_count=%d", len(params))
    return ParsedAnalysisRequest(
        request_projection=request_projection,
        params=params,
    )


__all__ = (
    "AnalysisRequestValidationError",
    "ParsedAnalysisRequest",
    "parse_analysis_flask_request",
    "parse_analysis_payload",
)
