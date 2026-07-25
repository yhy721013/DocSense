"""报告模块使用的不可变领域对象。

这里的对象只接收已经由入站 Adapter 完成公开协议解析后的内部值。它们不读取 Flask
request、不持有数据库连接，也不保存 AnythingLLM 的 workspace/thread/docpath 等供应商
字段。不可变快照可以防止 HTTP 请求返回后，后台任务继续引用并修改原始 ``dict/list``。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ReportDomainValidationError


REPORT_INPUT_SCHEMA_VERSION = 1
REPORT_ID_MAX_DECIMAL_DIGITS = 128
REPORT_ID_ABSOLUTE_UPPER_BOUND = 10**REPORT_ID_MAX_DECIMAL_DIGITS
REPORT_STATUS_SUCCEEDED = "1"
REPORT_STATUS_FAILED = "2"
REPORT_SUCCESS_MESSAGE = "生成成功"
REPORT_FAILURE_MESSAGE = "生成失败"
REPORT_TERMINAL_STATUSES = frozenset(
    {REPORT_STATUS_SUCCEEDED, REPORT_STATUS_FAILED}
)


def _required_text(value: object, *, name: str, strip: bool = True) -> str:
    """校验必填内部文本，拒绝把任意对象静默转换为字符串。"""

    if not isinstance(value, str):
        raise ReportDomainValidationError(f"{name} 必须是 str")
    normalized = value.strip() if strip else value
    if not normalized.strip():
        raise ReportDomainValidationError(f"{name} 不能为空")
    return normalized


def _optional_text(value: object, *, name: str) -> str:
    """校验允许为空的内部文本，并原样保留空白等业务内容。"""

    if not isinstance(value, str):
        raise ReportDomainValidationError(f"{name} 必须是 str")
    return value


def _source_url_tuple(value: object) -> tuple[str, ...]:
    """复制并冻结有序源文件 URL，同时保留重复项和原始顺序。

    公开接口已经确认数组元素必须为非空字符串；这里再次校验内部不变量，防止其他
    Application/Worker 入口绕过 Web Adapter 后把脏值写入持久化输入快照。
    """

    # 只接受有确定顺序和重复语义的容器。set 会丢失顺序，dict 会把键误当 URL；若在这里
    # 宽松接收，持久化后的输入快照将无法证明与受理时的业务输入一致。
    if not isinstance(value, (list, tuple)):
        raise ReportDomainValidationError("source_urls 必须是有序 URL 序列")
    source_urls = tuple(value)
    if not source_urls:
        raise ReportDomainValidationError("source_urls 不能为空")
    for index, source_url in enumerate(source_urls):
        if not isinstance(source_url, str) or not source_url.strip():
            raise ReportDomainValidationError(
                f"source_urls[{index}] 必须是非空 str"
            )
    return source_urls


@dataclass(frozen=True)
class ReportId:
    """报告公开数值与内部规范化业务键的成对表示。

    ``public_value`` 继续用于既有 JSON number 输出；``business_key`` 用于数据库、队列、
    Progress 和幂等判断。二者必须表示同一个 Python 整数，不引入 32/64 位机器整数范围；
    但绝对值必须小于 ``10**128``，保证存储与外部资源标识具有确定上界。
    """

    public_value: int
    business_key: str

    def __post_init__(self) -> None:
        if isinstance(self.public_value, bool) or not isinstance(
            self.public_value,
            int,
        ):
            raise ReportDomainValidationError("ReportId.public_value 必须是 int")
        if not (
            -REPORT_ID_ABSOLUTE_UPPER_BOUND
            < self.public_value
            < REPORT_ID_ABSOLUTE_UPPER_BOUND
        ):
            raise ReportDomainValidationError(
                "ReportId.public_value 不能超过128位十进制数字"
            )
        expected_key = str(self.public_value)
        if not isinstance(self.business_key, str) or self.business_key != expected_key:
            raise ReportDomainValidationError(
                "ReportId.business_key 必须是 public_value 的规范十进制文本"
            )

    @classmethod
    def from_public_value(cls, value: int) -> "ReportId":
        """从已经完成 Web 校验的公开整数构造领域标识。"""

        if isinstance(value, bool) or not isinstance(value, int):
            raise ReportDomainValidationError("report_id 必须是 int")
        # 必须先比较数值再转换为十进制文本。否则绕过 Web Adapter 的内部调用方传入超大
        # Python int 时，可能先触发解释器的整数转字符串安全上限，而不是稳定领域错误。
        if not (
            -REPORT_ID_ABSOLUTE_UPPER_BOUND
            < value
            < REPORT_ID_ABSOLUTE_UPPER_BOUND
        ):
            raise ReportDomainValidationError(
                "ReportId.public_value 不能超过128位十进制数字"
            )
        return cls(public_value=value, business_key=str(value))


@dataclass(frozen=True)
class ReportSubmission:
    """Web Adapter 交给提交用例的不可变报告命令。"""

    report_id: ReportId
    source_urls: tuple[str, ...]
    template_outline_url: str
    template_desc: str
    requirement: str
    trace_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.report_id, ReportId):
            raise ReportDomainValidationError("report_id 必须是 ReportId")
        object.__setattr__(self, "source_urls", _source_url_tuple(self.source_urls))
        object.__setattr__(
            self,
            "template_outline_url",
            _required_text(
                self.template_outline_url,
                name="template_outline_url",
            ),
        )
        object.__setattr__(
            self,
            "template_desc",
            _optional_text(self.template_desc, name="template_desc"),
        )
        object.__setattr__(
            self,
            "requirement",
            _optional_text(self.requirement, name="requirement"),
        )
        object.__setattr__(
            self,
            "trace_id",
            _required_text(self.trace_id, name="trace_id"),
        )


@dataclass(frozen=True)
class ReportInputSnapshot:
    """可靠受理后按 task ID 恢复执行所需的完整不可变输入。"""

    schema_version: int
    task_id: str
    report_id: ReportId
    source_urls: tuple[str, ...]
    template_outline_url: str
    template_desc: str
    requirement: str
    accepted_at: str
    trace_id: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version <= 0
        ):
            raise ReportDomainValidationError("schema_version 必须是正整数")
        object.__setattr__(
            self,
            "task_id",
            _required_text(self.task_id, name="task_id"),
        )
        if not isinstance(self.report_id, ReportId):
            raise ReportDomainValidationError("report_id 必须是 ReportId")
        object.__setattr__(self, "source_urls", _source_url_tuple(self.source_urls))
        object.__setattr__(
            self,
            "template_outline_url",
            _required_text(
                self.template_outline_url,
                name="template_outline_url",
            ),
        )
        object.__setattr__(
            self,
            "template_desc",
            _optional_text(self.template_desc, name="template_desc"),
        )
        object.__setattr__(
            self,
            "requirement",
            _optional_text(self.requirement, name="requirement"),
        )
        object.__setattr__(
            self,
            "accepted_at",
            _required_text(self.accepted_at, name="accepted_at"),
        )
        object.__setattr__(
            self,
            "trace_id",
            _required_text(self.trace_id, name="trace_id"),
        )

    @classmethod
    def from_submission(
        cls,
        submission: ReportSubmission,
        *,
        task_id: str,
        accepted_at: str,
        schema_version: int = REPORT_INPUT_SCHEMA_VERSION,
    ) -> "ReportInputSnapshot":
        """把已校验命令冻结为可持久化快照，不引用调用方的可变容器。"""

        if not isinstance(submission, ReportSubmission):
            raise ReportDomainValidationError("submission 必须是 ReportSubmission")
        return cls(
            schema_version=schema_version,
            task_id=task_id,
            report_id=submission.report_id,
            source_urls=tuple(submission.source_urls),
            template_outline_url=submission.template_outline_url,
            template_desc=submission.template_desc,
            requirement=submission.requirement,
            accepted_at=accepted_at,
            trace_id=submission.trace_id,
        )


@dataclass(frozen=True)
class ReportResult:
    """RAG 原始文本及其稳定 HTML 表示。"""

    report_id: ReportId
    raw_content: str
    html_details: str

    def __post_init__(self) -> None:
        if not isinstance(self.report_id, ReportId):
            raise ReportDomainValidationError("report_id 必须是 ReportId")
        object.__setattr__(
            self,
            "raw_content",
            _optional_text(self.raw_content, name="raw_content"),
        )
        object.__setattr__(
            self,
            "html_details",
            _required_text(
                self.html_details,
                name="html_details",
                strip=False,
            ),
        )

    @property
    def empty_rag_result(self) -> bool:
        """标记已确认需要按成功处理的空 RAG 内容，供应用层记录日志/指标。"""

        return not self.raw_content.strip()


@dataclass(frozen=True)
class ReportCallbackPayload:
    """报告成功或失败回调的强类型内部表示。"""

    report_id: ReportId
    status: str
    details: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.report_id, ReportId):
            raise ReportDomainValidationError("report_id 必须是 ReportId")
        # 先校验类型再做集合成员判断，确保 list/dict 等错误内部值也统一收敛为稳定领域
        # 异常，而不是泄漏 Python 的 ``TypeError: unhashable type``。
        if (
            not isinstance(self.status, str)
            or self.status not in REPORT_TERMINAL_STATUSES
        ):
            raise ReportDomainValidationError("status 只能是 1 或 2")
        object.__setattr__(
            self,
            "details",
            _optional_text(self.details, name="details"),
        )
        expected_message = (
            REPORT_SUCCESS_MESSAGE
            if self.status == REPORT_STATUS_SUCCEEDED
            else REPORT_FAILURE_MESSAGE
        )
        if self.message != expected_message:
            raise ReportDomainValidationError(
                "message 必须与报告终态保持一致"
            )

    def to_public_dict(self) -> dict[str, object]:
        """每次返回新的公开载荷，防止调用方修改领域对象内部状态。"""

        return {
            "businessType": "report",
            "data": {
                "reportId": self.report_id.public_value,
                "status": self.status,
                "details": self.details,
            },
            "msg": self.message,
        }


__all__ = [
    "REPORT_FAILURE_MESSAGE",
    "REPORT_ID_ABSOLUTE_UPPER_BOUND",
    "REPORT_ID_MAX_DECIMAL_DIGITS",
    "REPORT_INPUT_SCHEMA_VERSION",
    "REPORT_STATUS_FAILED",
    "REPORT_STATUS_SUCCEEDED",
    "REPORT_SUCCESS_MESSAGE",
    "REPORT_TERMINAL_STATUSES",
    "ReportCallbackPayload",
    "ReportId",
    "ReportInputSnapshot",
    "ReportResult",
    "ReportSubmission",
]
