#!/usr/bin/env python3
"""阶段 0 容量基线采集器。

该脚本只连接一个已经由操作者启动的 DocSense 实例，绝不会导入或启动 ``run.py``。
默认只允许访问本机回环地址，并默认拒绝需要 AnythingLLM/模型的重型场景，防止一次
普通基线检查误打开发或生产依赖。日志只记录场景名、状态码、耗时和错误类型，不记录
请求正文、响应正文、鉴权头或完整带查询参数 URL。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit


LOGGER = logging.getLogger("docsense.stage0_load_baseline")
SUPPORTED_SCENARIO_TYPES = frozenset({"http", "sse", "websocket"})
TERMINAL_SSE_EVENTS = frozenset({"aborted", "done", "error"})
_MAX_TIMEOUT_SECONDS = 3600.0
_MAX_SSE_EVENTS = 1_000_000
_MAX_SCENARIOS = 100
_FUTURE_WINDOW_MULTIPLIER = 2


@dataclass(frozen=True)
class SampleResult:
    """一次请求或连接的脱敏测量结果。"""

    succeeded: bool
    latency_ms: float
    status_code: int | None = None
    error_type: str | None = None
    ready_latency_ms: float | None = None
    hold_duration_ms: float | None = None


def _write_stdout(payload: str) -> None:
    """向标准输出写入机器可读结果，不把结果 JSON 混入运行日志。"""

    sys.stdout.write(payload)
    if not payload.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def _write_stderr(message: str) -> None:
    """向标准错误输出适合 CLI 调用方读取的单行失败原因。"""

    sys.stderr.write(message.rstrip("\n") + "\n")
    sys.stderr.flush()


def _require_number(
    scenario: Mapping[str, Any],
    field: str,
    *,
    minimum: float,
    maximum: float,
    default: float,
) -> float:
    """读取有限数值配置并统一执行上下界检查。"""

    value = scenario.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"场景 {scenario['name']} 的 {field} 必须是数字")
    numeric = float(value)
    if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
        raise ValueError(
            f"场景 {scenario['name']} 的 {field} 必须位于 "
            f"{minimum:g}..{maximum:g}"
        )
    return numeric


def _validate_expected_statuses(scenario: Mapping[str, Any]) -> None:
    """保证 HTTP 状态白名单是非空且不包含 bool 的标准状态码数组。"""

    statuses = scenario.get("expectedStatuses", [200])
    if (
        not isinstance(statuses, list)
        or not statuses
        or any(
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
            for status in statuses
        )
    ):
        raise ValueError(
            f"场景 {scenario['name']} 的 expectedStatuses "
            "必须是 100..599 的非空整数数组"
        )


def percentile(values: Sequence[float], quantile: float) -> float:
    """使用线性插值计算分位数，空集合返回 0，便于稳定生成 JSON 报告。"""

    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile 必须位于 0 到 1 之间")
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def assert_safe_base_url(base_url: str, *, allow_non_loopback: bool) -> None:
    """拒绝凭据 URL、非 HTTP(S) 协议和未明确授权的非回环目标。"""

    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base URL 必须是带主机名的 http/https 地址")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base URL 不得内嵌用户名或密码")
    if allow_non_loopback:
        return

    host = parsed.hostname.lower()
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError(
        "默认只允许 localhost/回环 IP；访问其他服务器必须显式传入 "
        "--allow-non-loopback"
    )


def build_endpoint_url(base_url: str, path: str, *, websocket: bool = False) -> str:
    """构造目标 URL，并阻止场景文件用绝对 URL 绕过统一安全检查。"""

    parsed_path = urlsplit(path)
    if parsed_path.scheme or parsed_path.netloc or not path.startswith("/"):
        raise ValueError("场景 path 必须是以 / 开头的站内路径")
    endpoint = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    if not websocket:
        return endpoint
    parsed = urlsplit(endpoint)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((ws_scheme, parsed.netloc, parsed.path, parsed.query, ""))


def load_workload(path: Path) -> dict[str, Any]:
    """读取并验证场景配置；配置错误必须在发出网络请求前失败。"""

    workload = json.loads(path.read_text(encoding="utf-8"))
    validate_workload(workload)
    return workload


def validate_workload(workload: Mapping[str, Any]) -> None:
    """验证阶段 0 场景的最小结构和危险参数上限。"""

    scenarios = workload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("workload.scenarios 必须是非空数组")
    if len(scenarios) > _MAX_SCENARIOS:
        raise ValueError(f"workload.scenarios 最多允许 {_MAX_SCENARIOS} 项")
    schema_version = workload.get("schemaVersion", 1)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise ValueError("workload.schemaVersion 当前只支持整数 1")

    seen_names: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise ValueError("每个 scenario 必须是对象")
        name = scenario.get("name")
        scenario_type = scenario.get("type")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("scenario.name 必须是非空字符串")
        if name in seen_names:
            raise ValueError(f"scenario.name 重复: {name}")
        seen_names.add(name)
        if scenario_type not in SUPPORTED_SCENARIO_TYPES:
            raise ValueError(f"不支持的场景类型: {scenario_type}")
        for flag in ("enabled", "templateOnly", "requiresHeavyServices"):
            if flag in scenario and not isinstance(scenario[flag], bool):
                raise ValueError(f"场景 {name} 的 {flag} 必须是 bool")
        if scenario.get("templateOnly", False) and scenario.get("enabled", False):
            raise ValueError(
                f"模板场景 {name} 不能直接启用；请复制后填入唯一测试数据并删除 "
                "templateOnly 标记"
            )
        if not isinstance(scenario.get("path"), str):
            raise ValueError(f"场景 {name} 缺少 path")
        build_endpoint_url("http://localhost", scenario["path"])

        concurrency = scenario.get("concurrency")
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or not 1 <= concurrency <= 500
        ):
            raise ValueError(f"场景 {name} 的 concurrency 必须位于 1..500")
        if scenario_type in {"http", "sse"}:
            method = scenario.get(
                "method",
                "POST" if scenario_type == "sse" else "GET",
            )
            if not isinstance(method, str) or method.upper() not in {
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "HEAD",
                "OPTIONS",
            }:
                raise ValueError(f"场景 {name} 的 method 不是受支持的 HTTP 方法")
            total_requests = scenario.get("totalRequests")
            if (
                isinstance(total_requests, bool)
                or not isinstance(total_requests, int)
                or not 1 <= total_requests <= 100000
            ):
                raise ValueError(f"场景 {name} 的 totalRequests 必须位于 1..100000")
            _require_number(
                scenario,
                "timeoutSeconds",
                minimum=0.1,
                maximum=_MAX_TIMEOUT_SECONDS,
                default=120.0 if scenario_type == "sse" else 10.0,
            )
            _validate_expected_statuses(scenario)
        if scenario_type == "sse":
            max_events = scenario.get("maxEvents", 10000)
            if (
                isinstance(max_events, bool)
                or not isinstance(max_events, int)
                or not 1 <= max_events <= _MAX_SSE_EVENTS
            ):
                raise ValueError(
                    f"场景 {name} 的 maxEvents 必须位于 1..{_MAX_SSE_EVENTS}"
                )
        if scenario_type == "websocket":
            if not isinstance(scenario.get("message"), dict):
                raise ValueError(f"WebSocket 场景 {name} 必须提供 message 对象")
            if "action" in scenario["message"]:
                raise ValueError(
                    f"WebSocket 场景 {name} 不得使用已下线目标契约中的 action"
                )
            business_type = scenario["message"].get("businessType")
            params = scenario["message"].get("params")
            if not isinstance(business_type, str) or not business_type.strip():
                raise ValueError(
                    f"WebSocket 场景 {name} 的 message.businessType 必须是非空字符串"
                )
            if (
                not isinstance(params, list)
                or not params
                or any(not isinstance(item, dict) for item in params)
            ):
                raise ValueError(
                    f"WebSocket 场景 {name} 的 message.params 必须是非空对象数组"
                )
            hold_seconds = _require_number(
                scenario,
                "holdSeconds",
                minimum=0.1,
                maximum=60.0,
                default=5.0,
            )
            _require_number(
                scenario,
                "receiveTimeoutSeconds",
                minimum=0.1,
                maximum=300.0,
                default=10.0,
            )
            probe_seconds = _require_number(
                scenario,
                "livenessProbeSeconds",
                minimum=0.1,
                maximum=30.0,
                default=1.0,
            )
            if probe_seconds > hold_seconds:
                raise ValueError(
                    f"场景 {name} 的 livenessProbeSeconds 不得大于 holdSeconds"
                )


def select_scenarios(
    workload: Mapping[str, Any],
    *,
    names: set[str] | None,
    allow_heavy_services: bool,
) -> list[dict[str, Any]]:
    """选择显式启用且满足重型服务门禁的场景。"""

    selected: list[dict[str, Any]] = []
    for scenario in workload["scenarios"]:
        if names and scenario["name"] not in names:
            continue
        if scenario.get("templateOnly", False):
            LOGGER.info("跳过仅供生成真实数据的模板场景: scenario=%s", scenario["name"])
            continue
        if not scenario.get("enabled", False):
            LOGGER.info("跳过未启用场景: scenario=%s", scenario["name"])
            continue
        if scenario.get("requiresHeavyServices", False) and not allow_heavy_services:
            LOGGER.warning(
                "跳过重型场景，需显式授权: scenario=%s flag=--allow-heavy-services",
                scenario["name"],
            )
            continue
        selected.append(dict(scenario))
    if names:
        known = {scenario["name"] for scenario in workload["scenarios"]}
        unknown = names - known
        if unknown:
            raise ValueError(f"未找到场景: {', '.join(sorted(unknown))}")
    return selected


def _http_sample(base_url: str, scenario: Mapping[str, Any]) -> SampleResult:
    """执行一次普通 HTTP 请求；每个线程独立调用，避免共享 Session 串扰。"""

    started = time.perf_counter()
    try:
        import requests

        response = requests.request(
            method=str(scenario.get("method", "GET")).upper(),
            url=build_endpoint_url(base_url, scenario["path"]),
            params=scenario.get("query"),
            json=scenario.get("json"),
            timeout=float(scenario.get("timeoutSeconds", 10)),
        )
        succeeded = int(response.status_code) in set(
            scenario.get("expectedStatuses", [200])
        )
        return SampleResult(
            succeeded=succeeded,
            latency_ms=(time.perf_counter() - started) * 1000,
            status_code=int(response.status_code),
        )
    except Exception as exc:  # 网络错误必须汇总，单样本失败不能中止整轮。
        return SampleResult(
            succeeded=False,
            latency_ms=(time.perf_counter() - started) * 1000,
            error_type=type(exc).__name__,
        )


def _sse_sample(base_url: str, scenario: Mapping[str, Any]) -> SampleResult:
    """读取一条 SSE 流到首个终态事件或事件上限，防止无限占用连接。"""

    started = time.perf_counter()
    response = None
    try:
        import requests

        response = requests.request(
            method=str(scenario.get("method", "POST")).upper(),
            url=build_endpoint_url(base_url, scenario["path"]),
            json=scenario.get("json"),
            timeout=float(scenario.get("timeoutSeconds", 120)),
            stream=True,
        )
        expected = set(scenario.get("expectedStatuses", [200]))
        if response.status_code not in expected:
            return SampleResult(
                succeeded=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                status_code=int(response.status_code),
            )

        event_count = 0
        max_events = int(scenario.get("maxEvents", 10000))
        terminal_seen = False
        for raw_line in response.iter_lines(decode_unicode=True):
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", errors="replace")
            if raw_line and raw_line.startswith("event: "):
                event_count += 1
                terminal_seen = raw_line[7:].strip() in TERMINAL_SSE_EVENTS
            if terminal_seen or event_count >= max_events:
                break
        return SampleResult(
            succeeded=terminal_seen,
            latency_ms=(time.perf_counter() - started) * 1000,
            status_code=int(response.status_code),
            error_type=None if terminal_seen else "TerminalEventMissing",
        )
    except Exception as exc:
        return SampleResult(
            succeeded=False,
            latency_ms=(time.perf_counter() - started) * 1000,
            error_type=type(exc).__name__,
        )
    finally:
        if response is not None:
            response.close()


def _decode_progress_message(
    message: str | bytes,
    *,
    request_message: Mapping[str, Any],
) -> dict[str, Any]:
    """校验容量探针收到的 Progress 消息，但不记录其中的业务正文。"""

    if isinstance(message, bytes):
        message = message.decode("utf-8")
    snapshot = json.loads(message)
    if not isinstance(snapshot, dict) or "error" in snapshot:
        raise ValueError("WebSocket 消息不是有效进度快照")
    expected_business_type = request_message.get("businessType")
    if snapshot.get("businessType") != expected_business_type:
        raise ValueError("进度快照的 businessType 与订阅不一致")
    data = snapshot.get("data")
    if not isinstance(data, dict):
        raise ValueError("进度快照的 data 必须是对象")
    progress = data.get("progress")
    if (
        isinstance(progress, bool)
        or not isinstance(progress, (int, float))
        or not math.isfinite(float(progress))
        or not 0.0 <= float(progress) <= 1.0
    ):
        raise ValueError("进度快照的 data.progress 必须位于 0..1")

    key_by_type = {
        "file": "fileName",
        "report": "reportId",
        "weaponry": "architectureId",
    }
    key_name = key_by_type.get(str(expected_business_type))
    if key_name is not None:
        requested_values = {
            item.get(key_name)
            for item in request_message.get("params", ())
            if isinstance(item, dict) and key_name in item
        }
        if data.get(key_name) not in requested_values:
            raise ValueError("进度快照的业务键不属于本次订阅")
    return snapshot


def _websocket_sample(base_url: str, scenario: Mapping[str, Any]) -> SampleResult:
    """建立 Progress 连接，并在保持窗口内主动检测断连和 Ping/Pong。"""

    started = time.perf_counter()
    ready_at: float | None = None
    client = None
    try:
        from simple_websocket import Client

        hold_seconds = float(scenario.get("holdSeconds", 5))
        probe_seconds = float(scenario.get("livenessProbeSeconds", 1))
        client = Client.connect(
            build_endpoint_url(base_url, scenario["path"], websocket=True),
            # simple-websocket 的后台线程会按该周期发送 Ping；未收到 Pong 时主动
            # 关闭连接。单纯 sleep 无法发现对端在首帧后立即断开的假阳性。
            ping_interval=probe_seconds if hold_seconds > 0 else None,
        )
        client.send(json.dumps(scenario["message"], ensure_ascii=False))
        first_message = client.receive(
            timeout=float(scenario.get("receiveTimeoutSeconds", 10))
        )
        if first_message is None:
            raise TimeoutError("未收到初始进度快照")
        _decode_progress_message(
            first_message,
            request_message=scenario["message"],
        )
        ready_at = time.perf_counter()

        # 在保持窗口内持续调用 receive。超时返回 None 只表示当前没有业务消息；
        # ``connected`` 变为 False 或 receive 抛出 ConnectionClosed 才表示连接失活。
        deadline = ready_at + hold_seconds
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            if not bool(getattr(client, "connected", False)):
                raise ConnectionError("WebSocket 在保持窗口内已断开")
            message = client.receive(timeout=min(probe_seconds, remaining))
            if message is not None:
                _decode_progress_message(
                    message,
                    request_message=scenario["message"],
                )
            if not bool(getattr(client, "connected", False)):
                raise ConnectionError("WebSocket 在保持窗口内已断开")

        if not bool(getattr(client, "connected", False)):
            raise ConnectionError("WebSocket 在保持窗口结束前已断开")
        completed_at = time.perf_counter()
        # 客户端 close 返回只证明本地关闭调用完成，不冒充服务端订阅表已经清理。
        client.close()
        client = None
        return SampleResult(
            succeeded=True,
            latency_ms=(completed_at - started) * 1000,
            status_code=101,
            ready_latency_ms=(ready_at - started) * 1000,
            hold_duration_ms=(completed_at - ready_at) * 1000,
        )
    except Exception as exc:
        failed_at = time.perf_counter()
        return SampleResult(
            succeeded=False,
            latency_ms=(failed_at - started) * 1000,
            error_type=type(exc).__name__,
            ready_latency_ms=(ready_at - started) * 1000
            if ready_at is not None
            else None,
            hold_duration_ms=(failed_at - ready_at) * 1000
            if ready_at is not None
            else None,
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                LOGGER.debug("关闭 WebSocket 连接失败", exc_info=True)


def summarize_results(
    scenario: Mapping[str, Any],
    results: Iterable[SampleResult],
    *,
    elapsed_seconds: float,
) -> dict[str, Any]:
    """生成不含业务正文的吞吐、成功率、分位延迟和错误分类。

    主 ``latencyMs`` 只统计成功样本，避免快速失败或超时失败把成功链路分位数
    混成一个无法解释的值。失败耗时、WebSocket 首帧耗时和实际保持时间分别输出。
    """

    samples = list(results)
    successful_latencies = [
        sample.latency_ms for sample in samples if sample.succeeded
    ]
    failed_latencies = [
        sample.latency_ms for sample in samples if not sample.succeeded
    ]
    ready_latencies = [
        sample.ready_latency_ms
        for sample in samples
        if sample.succeeded and sample.ready_latency_ms is not None
    ]
    hold_durations = [
        sample.hold_duration_ms
        for sample in samples
        if sample.succeeded and sample.hold_duration_ms is not None
    ]
    statuses: dict[str, int] = {}
    errors: dict[str, int] = {}
    for sample in samples:
        if sample.status_code is not None:
            key = str(sample.status_code)
            statuses[key] = statuses.get(key, 0) + 1
        if sample.error_type:
            errors[sample.error_type] = errors.get(sample.error_type, 0) + 1
    succeeded = sum(sample.succeeded for sample in samples)
    total = len(samples)

    def _statistics(values: Sequence[float]) -> dict[str, float | int]:
        return {
            "samples": len(values),
            "p50": round(percentile(values, 0.50), 3),
            "p95": round(percentile(values, 0.95), 3),
            "p99": round(percentile(values, 0.99), 3),
            "max": round(max(values), 3) if values else 0.0,
        }

    return {
        "scenario": scenario["name"],
        "type": scenario["type"],
        "concurrency": scenario["concurrency"],
        "samples": total,
        "succeeded": succeeded,
        "failed": total - succeeded,
        "successRate": round(succeeded / total, 6) if total else 0.0,
        "elapsedSeconds": round(elapsed_seconds, 3),
        "throughputPerSecond": round(total / elapsed_seconds, 3)
        if elapsed_seconds > 0
        else 0.0,
        "successfulThroughputPerSecond": round(succeeded / elapsed_seconds, 3)
        if elapsed_seconds > 0
        else 0.0,
        "latencyMs": _statistics(successful_latencies),
        "failedLatencyMs": _statistics(failed_latencies),
        "readyLatencyMs": _statistics(ready_latencies),
        "holdDurationMs": _statistics(hold_durations),
        "statusCounts": statuses,
        "errorTypeCounts": errors,
    }


def _run_bounded_samples(
    executor: ThreadPoolExecutor,
    *,
    sample_function: Callable[[str, Mapping[str, Any]], SampleResult],
    base_url: str,
    scenario: Mapping[str, Any],
    sample_count: int,
) -> list[SampleResult]:
    """以有限在途 Future 运行样本，避免大场景一次创建十万个对象。"""

    max_in_flight = max(
        1,
        int(scenario["concurrency"]) * _FUTURE_WINDOW_MULTIPLIER,
    )
    submitted = 0
    results: list[SampleResult] = []
    in_flight: set[Future[SampleResult]] = set()

    def _fill_window() -> None:
        nonlocal submitted
        while submitted < sample_count and len(in_flight) < max_in_flight:
            in_flight.add(executor.submit(sample_function, base_url, scenario))
            submitted += 1

    _fill_window()
    while in_flight:
        completed, pending = wait(in_flight, return_when=FIRST_COMPLETED)
        in_flight = set(pending)
        for future in completed:
            try:
                results.append(future.result())
            except Exception as exc:  # 最后一道样本隔离，不让单线程异常中止整轮。
                results.append(
                    SampleResult(
                        succeeded=False,
                        latency_ms=0.0,
                        error_type=type(exc).__name__,
                    )
                )
        _fill_window()
    return results


def run_scenario(base_url: str, scenario: Mapping[str, Any]) -> dict[str, Any]:
    """以受控线程池运行单个场景，并等待全部样本收敛。"""

    scenario_type = scenario["type"]
    sample_function = {
        "http": _http_sample,
        "sse": _sse_sample,
        "websocket": _websocket_sample,
    }[scenario_type]
    sample_count = (
        scenario["concurrency"]
        if scenario_type == "websocket"
        else scenario["totalRequests"]
    )
    LOGGER.info(
        "开始容量场景: scenario=%s type=%s concurrency=%d samples=%d",
        scenario["name"],
        scenario_type,
        scenario["concurrency"],
        sample_count,
    )
    started = time.perf_counter()
    with ThreadPoolExecutor(
        max_workers=scenario["concurrency"],
        thread_name_prefix=f"stage0-{scenario_type}",
    ) as executor:
        results = _run_bounded_samples(
            executor,
            sample_function=sample_function,
            base_url=base_url,
            scenario=scenario,
            sample_count=sample_count,
        )
    elapsed = time.perf_counter() - started
    summary = summarize_results(scenario, results, elapsed_seconds=elapsed)
    LOGGER.info(
        "容量场景结束: scenario=%s success_rate=%.4f p95_ms=%.3f",
        scenario["name"],
        summary["successRate"],
        summary["latencyMs"]["p95"],
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DocSense 阶段 0 容量基线采集器")
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument(
        "--workload",
        type=Path,
        default=Path(__file__).with_name("stage0_workloads.example.json"),
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        help="只运行指定场景；可重复传入",
    )
    parser.add_argument("--allow-non-loopback", action="store_true")
    parser.add_argument("--allow-heavy-services", action="store_true")
    parser.add_argument(
        "--parallel-scenarios",
        action="store_true",
        help="并行运行所选场景，用于同时验证50条长连接和50并发短请求",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验配置和门禁，不发出网络请求",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args(argv)
    try:
        assert_safe_base_url(
            args.base_url,
            allow_non_loopback=args.allow_non_loopback,
        )
        workload = load_workload(args.workload)
        selected = select_scenarios(
            workload,
            names=set(args.scenarios) if args.scenarios else None,
            allow_heavy_services=args.allow_heavy_services,
        )
        if not selected:
            raise ValueError("没有满足 enabled 和安全门禁的可运行场景")
        if args.dry_run:
            _write_stdout(
                json.dumps(
                    {
                        "mode": "dry-run",
                        "baseUrlHost": urlsplit(args.base_url).hostname,
                        "scenarios": [scenario["name"] for scenario in selected],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.parallel_scenarios:
            # 场景级线程池只负责同时启动各自的受控样本池；每个场景仍使用自己的
            # concurrency 上限，避免把“混合负载”误实现成一个无界全局线程池。
            with ThreadPoolExecutor(
                max_workers=len(selected),
                thread_name_prefix="stage0-scenario",
            ) as executor:
                futures = [
                    executor.submit(run_scenario, args.base_url, item)
                    for item in selected
                ]
                summaries = [future.result() for future in futures]
        else:
            summaries = [run_scenario(args.base_url, item) for item in selected]
        _write_stdout(
            json.dumps({"results": summaries}, ensure_ascii=False, indent=2)
        )
        return 0 if all(item["failed"] == 0 for item in summaries) else 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("容量基线配置或执行前置检查失败: error_type=%s", type(exc).__name__)
        _write_stderr(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
