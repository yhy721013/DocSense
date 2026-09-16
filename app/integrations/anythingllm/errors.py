"""AnythingLLM HTTP 传输层对外暴露的稳定异常类型。

该模块隔离 ``requests`` 的异常体系，使原子客户端和上层服务只依赖项目定义的错误
契约。异常对象只允许保存已经脱敏的 URL 与响应摘要；不得把原始响应对象、请求头、
API Key 或未裁剪的响应正文挂载到异常实例。

调用方应优先按异常类型或稳定的 ``code`` 字段分类处理，不应解析面向运维人员的异常
消息文本。
"""

from __future__ import annotations

from typing import Optional


class AnythingLLMTransportError(RuntimeError):
    """所有 AnythingLLM 传输失败的稳定基类。

    属性:
        code: 供程序分类使用的稳定错误码。各子类覆盖该类属性。
        method: 发生错误的 HTTP 方法；构造前失败等场景可能为空。
        url: 已移除片段并脱敏敏感查询参数的请求地址。
        status_code: 上游 HTTP 状态码。请求未收到响应时为 ``None``。
        response_summary: 已脱敏、压缩空白并限制长度的响应正文摘要。

    这些字段用于结构化日志、重试决策和错误映射。异常消息可以随诊断需求优化，不属于
    机器可解析的稳定接口。
    """

    code: str = "transport_error"

    def __init__(
        self,
        message: str,
        *,
        method: str = "",
        url: str = "",
        status_code: Optional[int] = None,
        response_summary: str = "",
    ) -> None:
        """初始化一条已经完成安全处理的传输异常。

        参数:
            message: 面向日志和开发人员的安全错误描述。
            method: 相关 HTTP 方法。
            url: 已脱敏的请求 URL。
            status_code: 可选上游状态码。
            response_summary: 可选安全响应摘要。

        本类不会再次执行脱敏，因为它不知道 API Key。所有实例必须由掌握认证配置的
        传输对象构造，禁止调用方直接传入未经清洗的上游数据。
        """
        super().__init__(message)
        self.method = method
        self.url = url
        self.status_code = status_code
        self.response_summary = response_summary


class AnythingLLMTimeoutError(AnythingLLMTransportError):
    """请求建立连接或读取响应超过配置的超时时间。

    该类型通常可以进入受控重试策略，但是否重试仍取决于上层操作是否幂等。传输层只
    负责分类，不会自动重放可能产生副作用的请求。
    """

    code = "timeout"


class AnythingLLMConnectionError(AnythingLLMTransportError):
    """建立请求或读取流式响应期间发生非超时网络异常。

    常见来源包括 DNS 失败、连接被拒绝、连接重置和流式响应中断。由于无法仅凭该异常
    判断上游是否已经执行请求，上层重试写操作前必须结合幂等键或补偿机制。
    """

    code = "connection_error"


class AnythingLLMHTTPError(AnythingLLMTransportError):
    """AnythingLLM 返回 2xx 范围之外的 HTTP 状态码。

    ``status_code`` 必定包含传输层读取到的整数状态码；``response_summary`` 仅用于安全
    诊断，不能被当作稳定的 AnythingLLM 响应协议进行解析。
    """

    code = "http_error"


class AnythingLLMProtocolError(AnythingLLMTransportError):
    """HTTP 请求成功，但响应内容不符合当前传输协议约定。

    阶段 1 主要用于标识应返回 JSON 却无法解码的响应。具体字段缺失、字段类型错误等
    供应商接口语义，应由后续原子客户端转换为更细粒度的协议异常。
    """

    code = "protocol_error"


class AnythingLLMCleanupUncertainError(AnythingLLMProtocolError):
    """破坏性清理请求的最终状态无法确认。

    调用方必须把该错误写入生命周期审计并保留恢复凭据；禁止把网络超时、成员漂移或
    上游版本能力不明确解释成清理成功。
    """

    code = "cleanup_uncertain"


class AnythingLLMUploadRejectedError(AnythingLLMProtocolError):
    """上传已经产生供应商副作用，但返回集合因安全策略被拒绝。

    ``cleanup_attempted``/``cleanup_confirmed`` 只表达客户端已掌握的确定事实。
    ``folder_cleanup_token`` 是供受控恢复流程重试的内部 opaque token，不得写入甲方协议
    或普通日志。
    """

    code = "upload_rejected"

    def __init__(
        self,
        message: str,
        *,
        cleanup_attempted: bool = False,
        cleanup_confirmed: bool = False,
        folder_cleanup_token: str = "",
    ) -> None:
        super().__init__(message)
        self.cleanup_attempted = bool(cleanup_attempted)
        self.cleanup_confirmed = bool(cleanup_confirmed)
        self.folder_cleanup_token = str(folder_cleanup_token or "")


class AnythingLLMTransportClosedError(AnythingLLMTransportError):
    """任务级传输对象关闭后仍被用于发起请求。

    该异常表示调用方违反资源生命周期约束，通常属于编程错误，不应通过重试恢复。
    正确做法是为新的后台任务或流式请求创建新的传输对象。
    """

    code = "transport_closed"
