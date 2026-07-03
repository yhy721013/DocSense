"""AnythingLLM 的任务级 HTTP 传输层。

职责范围：

* 统一拼接并校验 AnythingLLM 请求地址；
* 统一注入认证、用户标识、内容类型和超时配置；
* 解析 JSON、multipart 与 SSE 三类通用 HTTP 交互；
* 将 ``requests`` 异常和非成功状态码转换为稳定的集成层异常；
* 在异常信息进入日志或上层代码前完成密钥脱敏和响应摘要截断；
* 确保普通响应、流式响应和底层会话均按确定的生命周期关闭。

本模块只处理 HTTP 协议，不理解工作区、文档、向量检索等供应商领域概念，也不会
把 ``requests.Response`` 暴露给上层。每个后台任务或 HTTP 流式请求应独占一个传输
对象，禁止把包含 ``requests.Session`` 的实例作为跨线程全局单例。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Iterator, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from requests import Response, Session

from app.integrations.anythingllm.errors import (
    AnythingLLMConnectionError,
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
    AnythingLLMTimeoutError,
    AnythingLLMTransportClosedError,
)


logger = logging.getLogger(__name__)


# 使用显式白名单约束当前传输层支持的 JSON 方法，避免调用方绕过公共接口发起任意请求。
_SUPPORTED_JSON_METHODS = frozenset({"GET", "POST", "DELETE"})
# URL 可能进入异常、日志和监控系统；这些查询参数的值必须在输出前统一替换。
_SENSITIVE_QUERY_KEYS = frozenset(
    {"api_key", "apikey", "authorization", "key", "token", "access_token"}
)


@dataclass(frozen=True)
class SSEEvent:
    """与业务事件类型无关的单个 Server-Sent Events 事件。

    属性:
        data: 一个事件内所有 ``data`` 行按换行符拼接后的完整数据。
        event: 可选事件名称；未提供 ``event`` 字段时为 ``None``。
        event_id: 可选事件标识。按照 SSE 规范，该值可以跨后续事件继承。
        retry: 服务端建议的毫秒级重连等待时间；格式无效时为 ``None``。

    该对象只描述 SSE 帧，不解释 ``data`` 中的 JSON，也不识别 AnythingLLM 的具体
    事件名称。领域事件转换属于后续原子客户端的职责。
    """

    data: str
    event: Optional[str] = None
    event_id: Optional[str] = None
    retry: Optional[int] = None


class AnythingLLMTransport:
    """管理单个任务所独占的 HTTP 会话及其资源生命周期。

    实例可以作为上下文管理器使用。无论任务正常完成还是抛出异常，退出 ``with``
    代码块时都会关闭底层会话。调用 ``close`` 后实例不可复用，后续请求会得到明确的
    ``AnythingLLMTransportClosedError``，而不是依赖 ``requests`` 的偶然行为。

    线程安全性:
        本类有可变的关闭状态，并持有 ``requests.Session``。它按设计不保证线程安全，
        必须由一个后台任务或一个流式 HTTP 请求独占。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: Optional[float],
        session: Optional[Session] = None,
        max_error_body_chars: int = 512,
    ) -> None:
        """创建并校验任务级传输对象。

        参数:
            base_url: AnythingLLM API 根地址，必须是无凭据、无查询参数、无片段的
                绝对 HTTP(S) URL。末尾斜杠会被规范化。
            api_key: Bearer 认证密钥。空值或只包含空白字符的值会被拒绝。
            timeout: 单次请求交给 ``requests`` 的超时时间，单位为秒；``None`` 表示
                不由客户端设置超时，其他值必须大于零。
            session: 可选的任务级会话。传入后其所有权转移给本对象，``close`` 时会
                一并关闭；未传入时创建新的 ``requests.Session``。
            max_error_body_chars: 异常响应摘要允许保留的最大字符数。下限为 64，避免
                过小配置导致诊断信息完全失去意义。

        异常:
            ValueError: 任一配置违反上述约束时抛出。配置会在创建默认会话前完成校验，
                因此构造失败不会遗留未关闭的网络资源。
        """
        normalized_base_url = str(base_url or "").strip().rstrip("/")
        if not normalized_base_url:
            raise ValueError("base_url 不能为空")
        parsed_base_url = urlsplit(normalized_base_url)
        if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
            raise ValueError("base_url 必须是绝对 HTTP(S) URL")
        if parsed_base_url.username or parsed_base_url.password:
            raise ValueError("base_url 不得包含用户名或密码")
        if parsed_base_url.query or parsed_base_url.fragment:
            raise ValueError("base_url 不得包含查询参数或片段")
        normalized_api_key = str(api_key or "").strip()
        if not normalized_api_key:
            raise ValueError("api_key 不能为空")
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout 必须大于 0 或设为 None")
        if max_error_body_chars < 64:
            raise ValueError("max_error_body_chars 不得小于 64")

        self._base_url = urlunsplit(
            (
                parsed_base_url.scheme,
                parsed_base_url.netloc,
                parsed_base_url.path.rstrip("/"),
                "",
                "",
            )
        )
        self._api_key = normalized_api_key
        self._timeout = timeout
        self._session = session if session is not None else Session()
        self._max_error_body_chars = max_error_body_chars
        self._closed = False

    def __enter__(self) -> "AnythingLLMTransport":
        """进入上下文并返回当前未关闭实例。

        异常:
            AnythingLLMTransportClosedError: 实例已经关闭时抛出，关闭操作不可逆。
        """
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        """退出上下文并关闭会话，不吞掉代码块中产生的异常。"""
        self.close()

    def close(self) -> None:
        """关闭传输对象持有的 HTTP 会话。

        该操作具备幂等性，重复调用不会重复关闭会话。关闭标志会在调用底层
        ``Session.close`` 前设置，确保即使自定义会话的关闭实现抛出异常，该传输对象
        也不会再次被用于发起请求。
        """
        if self._closed:
            return
        self._closed = True
        logger.debug("关闭 AnythingLLM 任务级 HTTP 会话")
        self._session.close()

    def get_json(
        self,
        path: str,
        *,
        user_id: Optional[int] = None,
        params: Optional[Mapping[str, Any]] = None,
        allow_empty: bool = False,
    ) -> Any:
        """发送 GET 请求并返回解析后的 JSON 数据。

        参数:
            path: 相对于 ``base_url`` 的请求路径，可带查询字符串，但不得是绝对 URL
                或包含 ``..`` 上级目录片段。
            user_id: 可选 AnythingLLM 用户标识；提供时写入专用请求头。
            params: 交给 ``requests`` 编码的附加查询参数。
            allow_empty: 是否允许成功响应没有正文。为 ``True`` 且正文为空时返回
                ``None``；否则空正文按协议错误处理。

        返回:
            JSON 解码后的 Python 对象，不会返回底层 ``Response``。

        异常:
            AnythingLLMTransportError: 请求超时、连接失败、HTTP 状态异常、JSON
                协议异常或传输对象已经关闭时抛出对应子类。
            ValueError: 请求路径不符合安全约束时抛出。
        """
        return self._request_json(
            "GET",
            path,
            user_id=user_id,
            params=params,
            allow_empty=allow_empty,
        )

    def post_json(
        self,
        path: str,
        payload: Any,
        *,
        user_id: Optional[int] = None,
        params: Optional[Mapping[str, Any]] = None,
        allow_empty: bool = False,
    ) -> Any:
        """发送 JSON 格式的 POST 请求并返回解析后的数据。

        参数:
            path: 相对于 ``base_url`` 的安全请求路径。
            payload: 交给 ``requests`` JSON 编码器的请求体；值为 ``None`` 时不发送
                JSON 请求体，但仍声明 JSON 内容类型。
            user_id: 可选 AnythingLLM 用户标识。
            params: 可选查询参数映射。
            allow_empty: 是否把成功但无正文的响应解释为 ``None``。

        返回:
            JSON 解码后的 Python 对象，响应对象始终在方法返回前关闭。

        异常:
            AnythingLLMTransportError: 发生传输、HTTP 或响应协议错误时抛出对应子类。
            ValueError: 请求路径不符合安全约束时抛出。
        """
        return self._request_json(
            "POST",
            path,
            payload=payload,
            user_id=user_id,
            params=params,
            allow_empty=allow_empty,
        )

    def delete_json(
        self,
        path: str,
        payload: Any = None,
        *,
        user_id: Optional[int] = None,
        params: Optional[Mapping[str, Any]] = None,
        allow_empty: bool = False,
    ) -> Any:
        """发送可携带 JSON 请求体的 DELETE 请求并返回解析结果。

        参数:
            path: 相对于 ``base_url`` 的安全请求路径。
            payload: 可选 JSON 请求体，兼容要求 DELETE 携带参数的上游接口。
            user_id: 可选 AnythingLLM 用户标识。
            params: 可选查询参数映射。
            allow_empty: 是否接受 204 等没有正文的成功响应。调用方必须显式开启，
                避免意外丢失本应存在的响应数据。

        返回:
            JSON 解码结果；允许空正文且响应为空时返回 ``None``。

        异常:
            AnythingLLMTransportError: 发生传输、HTTP 或响应协议错误时抛出对应子类。
            ValueError: 请求路径不符合安全约束时抛出。
        """
        return self._request_json(
            "DELETE",
            path,
            payload=payload,
            user_id=user_id,
            params=params,
            allow_empty=allow_empty,
        )

    def delete_status(
        self,
        path: str,
        payload: Any = None,
        *,
        user_id: Optional[int] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """发送只以 HTTP 状态码判断成功的 DELETE 请求。

        参数:
            path: 相对于 ``base_url`` 的安全请求路径。
            payload: 可选 JSON 请求体；为 ``None`` 时不发送请求体，也不声明 JSON
                内容类型。
            user_id: 可选 AnythingLLM 用户标识。
            params: 可选查询参数映射。

        返回:
            成功时固定返回 ``None``。响应正文无论为空、JSON 或纯文本都会被忽略，底层
            ``Response`` 始终在方法返回前关闭。

        异常:
            AnythingLLMTransportError: 请求超时、连接失败、非 2xx 状态或对象关闭时
                抛出对应子类。
            ValueError: 请求路径不符合安全约束时抛出。

        该方法只适用于上游契约明确声明“响应正文没有业务语义”的删除接口。例如部分
        AnythingLLM 版本删除成功后返回纯文本 ``OK``。它与 ``delete_json`` 分离，避免
        为兼容单个端点而削弱其他 JSON 接口的协议校验。
        """
        url = self._build_url(path)
        request_kwargs: dict[str, Any] = {
            "headers": self._headers(
                user_id=user_id,
                content_type="application/json" if payload is not None else None,
                accept="*/*",
            ),
            "params": params,
        }
        if payload is not None:
            request_kwargs["json"] = payload

        response = self._request_response("DELETE", url, **request_kwargs)
        response.close()

    def post_multipart(
        self,
        path: str,
        *,
        files: Mapping[str, Any],
        data: Optional[Mapping[str, Any]] = None,
        user_id: Optional[int] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """上传 multipart 数据并返回 JSON 解码结果。

        参数:
            path: 相对于 ``base_url`` 的安全请求路径。
            files: 符合 ``requests`` multipart 约定的文件字段映射。本方法只借用其中
                的文件对象，不负责关闭文件句柄。
            data: 可选普通表单字段。
            user_id: 可选 AnythingLLM 用户标识。
            params: 可选查询参数映射。

        返回:
            上游成功响应的 JSON 解码结果。

        注意:
            本方法刻意不设置 ``Content-Type``。multipart boundary 必须由
            ``requests`` 根据实际请求体生成，手动覆盖会导致服务端无法解析文件。
            无论 JSON 解码成功与否，HTTP 响应都会在退出本方法前关闭。

        异常:
            AnythingLLMTransportError: 发生传输、HTTP 或 JSON 协议错误时抛出。
            ValueError: 请求路径不符合安全约束时抛出。
        """
        url = self._build_url(path)
        response = self._request_response(
            "POST",
            url,
            headers=self._headers(user_id=user_id),
            files=files,
            data=data,
            params=params,
        )
        try:
            return self._decode_json_response(response, method="POST", url=url)
        finally:
            response.close()

    def stream_sse(
        self,
        path: str,
        payload: Any,
        *,
        user_id: Optional[int] = None,
        params: Optional[Mapping[str, Any]] = None,
        allow_json_lines: bool = False,
    ) -> Iterator[SSEEvent]:
        """发送 JSON 请求并逐个产出通用 SSE 事件。

        参数:
            path: 相对于 ``base_url`` 的安全请求路径。
            payload: 作为 POST 请求体发送的 JSON 可序列化对象。
            user_id: 可选 AnythingLLM 用户标识。
            params: 可选查询参数映射。
            allow_json_lines: 是否把没有 ``data:`` 前缀、但以 ``{`` 或 ``[`` 开头的行
                作为独立 data 事件产出。默认关闭，仅供明确存在 NDJSON 兼容需求的
                供应商适配层启用。

        产出:
            按服务端到达顺序生成 ``SSEEvent``。连续 ``data`` 行会以换行符合并；
            注释行被忽略；文件末尾没有空行时，最后一个完整事件仍会被产出。

        生命周期:
            这是生成器方法，HTTP 请求在首次迭代而不是调用本方法时发生。调用方应
            完整消费生成器，或在提前结束时调用生成器的 ``close``。无论正常结束、
            读取异常还是主动关闭，``finally`` 块都会释放流式响应。

        异常:
            AnythingLLMTimeoutError: 建立连接或读取响应流超时时抛出。
            AnythingLLMConnectionError: 建立连接或读取响应流发生网络错误时抛出。
            AnythingLLMHTTPError: 上游返回非 2xx 状态码时抛出。
            ValueError: 请求路径不符合安全约束时抛出。

        本方法只负责 SSE 协议分帧，不解析事件 ``data`` 的 JSON 内容，也不根据事件名
        触发 AnythingLLM 业务逻辑。``allow_json_lines`` 只识别 JSON 外形并保留原文本，
        实际 JSON 解码仍由上层完成。
        """
        url = self._build_url(path)
        response = self._request_response(
            "POST",
            url,
            headers=self._headers(
                user_id=user_id,
                content_type="application/json",
                accept="text/event-stream",
            ),
            json=payload,
            params=params,
            stream=True,
        )
        # 下列变量只缓存当前事件的字段；event_id 例外，它按照 SSE 规范跨事件保留。
        data_lines: list[str] = []
        event_name: Optional[str] = None
        event_id: Optional[str] = None
        retry: Optional[int] = None

        def build_event() -> Optional[SSEEvent]:
            """根据当前字段缓存构造事件；没有 data 字段时不产生空事件。"""
            if not data_lines:
                return None
            return SSEEvent(
                data="\n".join(data_lines),
                event=event_name,
                event_id=event_id,
                retry=retry,
            )

        try:
            response.encoding = "utf-8"
            for raw_line in response.iter_lines(decode_unicode=True, chunk_size=1):
                # 标准 requests 在 decode_unicode=True 时返回字符串；bytes 分支用于兼容
                # 未遵循该约定的自定义适配器，并以替换字符处理无法解码的字节。
                if isinstance(raw_line, bytes):
                    line = raw_line.decode("utf-8", errors="replace")
                else:
                    line = str(raw_line or "")

                # 空行标志一个 SSE 事件结束。只有包含 data 字段的事件才向上产出。
                if line == "":
                    event = build_event()
                    if event is not None:
                        yield event
                    data_lines = []
                    event_name = None
                    retry = None
                    # SSE 规范要求 id 跨事件继承，直至收到新的 id 字段。
                    continue

                if line.startswith(":"):
                    # 冒号开头的是心跳或注释，不属于业务事件字段。
                    continue

                if allow_json_lines and line.lstrip().startswith(("{", "[")):
                    # 部分流式接口使用 NDJSON 而不是标准 SSE。显式开启兼容时，每个原始
                    # JSON 行都作为独立事件产出，避免被误解为 SSE 字段名后静默丢弃。
                    logger.debug(
                        "按 NDJSON 兼容模式解析 AnythingLLM 流式响应: "
                        "url=%s line_chars=%d",
                        self._safe_url(url),
                        len(line),
                    )
                    pending_event = build_event()
                    if pending_event is not None:
                        yield pending_event
                    yield SSEEvent(
                        data=line.strip(),
                        event=event_name,
                        event_id=event_id,
                        retry=retry,
                    )
                    data_lines = []
                    event_name = None
                    retry = None
                    continue

                # 仅按首个冒号分隔，确保 data 内容中包含 URL、时间等冒号时不被破坏。
                field, separator, value = line.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if field == "data":
                    data_lines.append(value)
                elif field == "event":
                    event_name = value or None
                elif field == "id" and "\x00" not in value:
                    event_id = value or None
                elif field == "retry":
                    try:
                        retry = int(value)
                    except (TypeError, ValueError):
                        # 无效 retry 不能中断整个响应流；上层将其视为未提供重试建议。
                        retry = None

            # 服务端可能直接关闭连接而不发送尾部空行，此时仍需提交最后一个有效事件。
            event = build_event()
            if event is not None:
                yield event
        except requests.Timeout as exc:
            safe_url = self._safe_url(url)
            logger.warning(
                "AnythingLLM 流式响应读取超时: method=POST url=%s",
                safe_url,
            )
            raise AnythingLLMTimeoutError(
                f"AnythingLLM 流式响应超时：POST {safe_url}",
                method="POST",
                url=safe_url,
            ) from exc
        except requests.RequestException as exc:
            safe_url = self._safe_url(url)
            logger.warning(
                "AnythingLLM 流式响应读取中断: method=POST url=%s error_type=%s",
                safe_url,
                type(exc).__name__,
            )
            raise AnythingLLMConnectionError(
                f"AnythingLLM 流式响应中断：POST {safe_url}",
                method="POST",
                url=safe_url,
            ) from exc
        finally:
            response.close()

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        user_id: Optional[int] = None,
        params: Optional[Mapping[str, Any]] = None,
        allow_empty: bool = False,
    ) -> Any:
        """执行 JSON 请求公共流程并保证响应及时关闭。

        ``get_json``、``post_json`` 和 ``delete_json`` 通过该方法共享方法白名单、请求头、
        URL 校验、空正文策略与 JSON 解码逻辑。只有非 ``None`` 的 ``payload`` 才写入
        请求参数，即当前公共接口将 ``None`` 明确定义为“不发送请求体”。

        参数及异常语义与对应公开方法一致。``method`` 不在内部白名单时抛出
        ``ValueError``，防止新增调用方绕过经过审计的公开请求方法。
        """
        normalized_method = method.upper()
        if normalized_method not in _SUPPORTED_JSON_METHODS:
            raise ValueError(f"不支持的 JSON 请求方法：{normalized_method}")

        url = self._build_url(path)
        request_kwargs: dict[str, Any] = {
            "headers": self._headers(
                user_id=user_id,
                content_type="application/json",
            ),
            "params": params,
        }
        if payload is not None:
            request_kwargs["json"] = payload

        response = self._request_response(
            normalized_method,
            url,
            **request_kwargs,
        )
        try:
            if allow_empty and not self._response_text(response).strip():
                return None
            return self._decode_json_response(
                response,
                method=normalized_method,
                url=url,
            )
        finally:
            response.close()

    def _request_response(self, method: str, url: str, **kwargs: Any) -> Response:
        """发起底层 HTTP 请求并完成通用状态与异常归一化。

        参数:
            method: 已规范化的 HTTP 方法。
            url: 已通过 ``_build_url`` 构造的完整地址。
            **kwargs: 透传给 ``Session.request`` 的受控请求参数。

        返回:
            状态码为 2xx 的内部响应对象。该对象只允许在传输层内部使用，调用者必须
            通过 ``finally`` 关闭，不得继续向原子客户端或业务层传递。

        异常:
            AnythingLLMTimeoutError: ``requests`` 报告连接或读取超时时抛出。
            AnythingLLMConnectionError: 其他 ``requests`` 请求异常发生时抛出。
            AnythingLLMHTTPError: 响应状态码不在 200～299 范围时抛出。异常携带经过
                脱敏和截断的响应摘要，原始响应会在抛出前关闭。
            AnythingLLMTransportClosedError: 当前实例已经关闭时抛出。

        原始异常仅作为异常链保留用于诊断，对外异常正文不会拼接原始异常消息，避免
        网络库错误文本中的认证信息进入常规日志。
        """
        self._ensure_open(method=method, url=url)
        safe_url = self._safe_url(url)
        request_headers = kwargs.get("headers")
        has_user_context = bool(
            isinstance(request_headers, Mapping)
            and request_headers.get("X-AnythingLLM-User-Id") is not None
        )
        logger.debug(
            "发起 AnythingLLM HTTP 请求: method=%s url=%s stream=%s "
            "has_user_context=%s timeout_configured=%s",
            method,
            safe_url,
            bool(kwargs.get("stream")),
            has_user_context,
            self._timeout is not None,
        )
        try:
            response = self._session.request(
                method,
                url,
                timeout=self._timeout,
                **kwargs,
            )
        except requests.Timeout as exc:
            logger.warning(
                "AnythingLLM HTTP 请求超时: method=%s url=%s",
                method,
                safe_url,
            )
            raise AnythingLLMTimeoutError(
                f"AnythingLLM 请求超时：{method} {safe_url}",
                method=method,
                url=safe_url,
            ) from exc
        except requests.RequestException as exc:
            logger.warning(
                "AnythingLLM HTTP 连接失败: method=%s url=%s error_type=%s",
                method,
                safe_url,
                type(exc).__name__,
            )
            raise AnythingLLMConnectionError(
                f"AnythingLLM 请求失败：{method} {safe_url}",
                method=method,
                url=safe_url,
            ) from exc

        status_code = int(getattr(response, "status_code", 0) or 0)
        logger.debug(
            "收到 AnythingLLM HTTP 响应: method=%s url=%s status_code=%d",
            method,
            safe_url,
            status_code,
        )
        if not 200 <= status_code < 300:
            summary = self._safe_response_summary(response)
            logger.warning(
                "AnythingLLM HTTP 状态异常: method=%s url=%s status_code=%d "
                "response_summary_chars=%d",
                method,
                safe_url,
                status_code,
                len(summary),
            )
            # 错误响应不会再交给外层处理，因此必须在构造异常前就释放连接。
            response.close()
            message = f"AnythingLLM 返回 HTTP {status_code}：{method} {safe_url}"
            if summary:
                message = f"{message}；响应摘要={summary}"
            raise AnythingLLMHTTPError(
                message,
                method=method,
                url=safe_url,
                status_code=status_code,
                response_summary=summary,
            )
        return response

    def _decode_json_response(
        self,
        response: Response,
        *,
        method: str,
        url: str,
    ) -> Any:
        """解析成功响应中的 JSON，并把解码失败转换为协议异常。

        参数:
            response: 仅在传输层内部流转的成功响应。
            method: 用于异常诊断的 HTTP 方法。
            url: 用于异常诊断的完整请求地址。

        返回:
            ``requests.Response.json`` 返回的任意 Python 值。

        异常:
            AnythingLLMProtocolError: 响应正文不是合法 JSON 时抛出。异常中的 URL 和
                正文摘要均经过安全处理，便于上层分类处理而不暴露完整响应。

        本方法不负责关闭响应，资源生命周期由调用它的外层 ``finally`` 统一管理。
        """
        try:
            return response.json()
        except (TypeError, ValueError) as exc:
            summary = self._safe_response_summary(response)
            safe_url = self._safe_url(url)
            logger.warning(
                "AnythingLLM JSON 响应解析失败: method=%s url=%s status_code=%d "
                "response_summary_chars=%d",
                method,
                safe_url,
                int(getattr(response, "status_code", 0) or 0),
                len(summary),
            )
            message = f"AnythingLLM 返回无效 JSON：{method} {safe_url}"
            if summary:
                message = f"{message}；响应摘要={summary}"
            raise AnythingLLMProtocolError(
                message,
                method=method,
                url=safe_url,
                status_code=int(getattr(response, "status_code", 0) or 0),
                response_summary=summary,
            ) from exc

    def _headers(
        self,
        *,
        user_id: Optional[int] = None,
        content_type: Optional[str] = None,
        accept: str = "application/json",
    ) -> dict[str, str]:
        """构造单次请求使用的全新请求头字典。

        ``Authorization`` 始终使用构造阶段已校验的 API Key。``Content-Type`` 仅在调用方
        明确提供时写入，使 multipart 请求可以由 ``requests`` 自动生成 boundary。
        ``user_id`` 使用 ``is not None`` 判断，确保数值零不会被误判为未提供。

        每次调用都返回新字典，避免请求间共享可变对象而发生请求头串扰。
        """
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self._api_key}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        if user_id is not None:
            headers["X-AnythingLLM-User-Id"] = str(user_id)
        return headers

    def _build_url(self, path: str) -> str:
        """校验相对路径并与固定 API 根地址拼接。

        路径可以带查询字符串，也可以带一个便于调用方书写的前导斜杠；绝对 URL 和
        ``..`` 路径片段会被拒绝，防止调用方绕过配置的 AnythingLLM 主机或逃逸 API
        根路径。根地址已在构造阶段去除末尾斜杠，因此结果始终只有一个连接斜杠。

        异常:
            ValueError: 路径为空、为绝对 URL 或包含上级目录片段时抛出。
            AnythingLLMTransportClosedError: 传输对象已经关闭时抛出。
        """
        self._ensure_open()
        normalized_path = str(path or "").strip()
        if not normalized_path:
            raise ValueError("请求路径不能为空")
        parsed = urlsplit(normalized_path)
        if parsed.scheme or parsed.netloc:
            raise ValueError("请求路径必须相对于已配置的 AnythingLLM base_url")
        if any(segment == ".." for segment in parsed.path.split("/")):
            raise ValueError("请求路径不得包含上级目录片段")
        return f"{self._base_url}/{normalized_path.lstrip('/')}"

    def _ensure_open(self, *, method: str = "", url: str = "") -> None:
        """检查生命周期状态，并在关闭后阻止继续使用会话。

        ``method`` 和 ``url`` 只用于补充异常上下文。URL 在写入异常前仍会经过脱敏，
        避免关闭状态异常成为查询参数泄漏的旁路。
        """
        if not self._closed:
            return
        safe_url = self._safe_url(url) if url else ""
        raise AnythingLLMTransportClosedError(
            "AnythingLLM 传输对象已关闭",
            method=method,
            url=safe_url,
        )

    def _safe_response_summary(self, response: Response) -> str:
        """生成适合写入异常和日志的有界响应摘要。

        处理顺序固定为：安全读取正文、替换敏感值、压缩连续空白、限制最大长度。
        必须先脱敏再截断，否则密钥跨越截断边界时可能残留部分敏感内容。返回值为空
        表示响应没有可安全读取的文本，不代表上游一定没有二进制正文。
        """
        text = self._response_text(response)
        if not text:
            return ""
        redacted = self._redact(text)
        compact = re.sub(r"\s+", " ", redacted).strip()
        if len(compact) <= self._max_error_body_chars:
            return compact
        return compact[: self._max_error_body_chars].rstrip() + "...<truncated>"

    @staticmethod
    def _response_text(response: Response) -> str:
        """以防御方式读取文本正文，不让诊断辅助逻辑覆盖主异常。

        标准 ``requests.Response.text`` 通常不会抛出异常，但测试替身或自定义 HTTP
        适配器可能违反该约定。此处只接受字符串；读取失败或得到其他类型时返回空串。
        """
        try:
            value = response.text
        except Exception:  # 兼容行为异常的自定义 HTTP 适配器
            return ""
        return value if isinstance(value, str) else ""

    def _redact(self, value: str) -> str:
        """从任意诊断文本中替换已知认证信息。

        脱敏分三层执行：先精确替换当前实例的 API Key，再处理 Bearer 凭据，最后处理
        常见的 ``api_key``、``token`` 和 ``authorization`` 键值形式。该方法用于日志与
        异常安全，不应被误用为通用机密检测器或数据持久化前的内容清洗器。
        """
        redacted = value
        if self._api_key:
            redacted = redacted.replace(self._api_key, "<redacted>")
        redacted = re.sub(
            r"(?i)(bearer\s+)[^\s\"',;}]+",
            r"\1<redacted>",
            redacted,
        )
        redacted = re.sub(
            r'(?i)(["\']?(?:api[_-]?key|token|authorization)["\']?\s*[:=]\s*["\']?)[^\s\"\',;}]+',
            r"\1<redacted>",
            redacted,
        )
        return redacted

    def _safe_url(self, value: str) -> str:
        """移除 URL 片段并对敏感查询参数进行结构化重编码。

        使用 URL 解析器处理查询参数，而不是依赖整段正则表达式，以保留非敏感参数和
        重复参数的顺序。任何解析异常都会降级到通用文本脱敏，保证错误处理路径不会
        因诊断信息处理失败而掩盖真正的网络或协议异常。
        """
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
            safe_query = urlencode(
                [
                    (key, "<redacted>" if key.lower() in _SENSITIVE_QUERY_KEYS else item)
                    for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                ]
            )
            return urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, safe_query, "")
            )
        except Exception:  # 确保异常信息脱敏流程本身不会失败
            return self._redact(value)
