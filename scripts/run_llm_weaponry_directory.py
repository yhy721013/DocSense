#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import closing
import csv
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.modules.weaponry.domain import (  # noqa: E402
    FORCED_EMPTY_FIELD_NAMES,
    is_forced_empty_field_name,
)
from app.integrations.anythingllm import (  # noqa: E402
    AnythingLLMTransport,
    AnythingLLMWorkspaceClient,
)
from app.services.core.config import load_anythingllm_config  # noqa: E402


RUNTIME_DIR = ROOT / ".runtime"
TASK_DB = RUNTIME_DIR / "llm_tasks.sqlite3"
KB_DB = RUNTIME_DIR / "knowledge_base.sqlite3"
DEFAULT_OUTPUT_PREFIX = "qwen3-4b-new"

logger = logging.getLogger(__name__)

FIELD_NAMES = [
    "装备编号",
    "一级分类",
    "二级分类",
    "三级分类",
    "四级分类",
    "上一代装备",
    "下一代装备",
    "中文型号",
    "英文型号",
    "母港位置",
    "系列/族",
    "军种",
    "服役状态",
    "总装厂商",
    "海上移动识别码",
    "舷号",
    "开工时间",
    "下水时间",
    "服役时间",
    "造价",
    "基本情况",
    "性能特点",
    "标准排水量",
    "满载排水量",
    "长",
    "型宽",
    "吃水",
    "垂线间长",
    "水线宽",
    "飞行甲板长",
    "飞行甲板宽",
    "机库长",
    "机库宽",
    "机库高",
    "军官人数",
    "舰员",
    "机组人数",
    "飞机升降机",
    "舰载机持续出动架次率",
    "舰载机高强度出动架次率",
    "载油量",
    "油耗",
    "轴系与螺旋桨",
    "装置型号",
    "装置类型",
    "装置数量",
    "装置总功率",
    "最大速度",
    "巡航速度",
    "续航力",
    "最大加速度",
    "最大转向速度",
    "雷达散射截面积",
    "光学截面积",
    "红外特征值",
    "等效雷达高度",
    "声强度值",
    "雷达型号",
    "雷达种类",
    "雷达数量",
    "对抗措施型号",
    "对抗措施种类",
    "对抗措施数量",
    "武器型号",
    "武器种类",
    "武器数量",
    "搭载型号",
    "搭载型号种类",
    "搭载型号数量",
    "声纳型号",
    "声纳种类",
    "声纳数量",
    "航保装置型号",
    "航保装置种类",
    "航保装置数量",
]
EXTRACTABLE_FIELD_NAMES = tuple(
    name for name in FIELD_NAMES if not is_forced_empty_field_name(name)
)
FORCED_EMPTY_CONTRACT_INPUT_FIELDS = tuple(
    name for name in FIELD_NAMES if is_forced_empty_field_name(name)
)
FORCED_EMPTY_CONTRACT_CONTROL_FIELD = "舷号"
FORCED_EMPTY_CONTRACT_MIXED_TABLE_FIELD = "保留字段与单舰信息"
FORCED_EMPTY_CONTRACT_RESERVED_TABLE_FIELD = "保留字段空值合同"
FORCED_EMPTY_CONTRACT_MIXED_COLUMNS = (
    *FORCED_EMPTY_CONTRACT_INPUT_FIELDS,
    "单舰名称",
    "舷号",
)
STANDARD_EMPTY_DATA_SOURCE = {
    "content": "",
    "source": "",
    "time": "",
    "fileName": "",
    "rows": [],
    "translate": "",
}

FIELD_DESCRIPTIONS = {
    "装备编号": "提取装备编号、项目编号或资料中的唯一编号标识。",
    "一级分类": "提取装备所属一级分类名称。",
    "二级分类": "提取装备所属二级分类名称。",
    "三级分类": "提取装备所属三级分类名称。",
    "四级分类": "提取装备所属四级分类名称。",
    "上一代装备": "提取上一代或前代装备型号/名称。",
    "下一代装备": "提取下一代、后继或替代装备型号/名称。",
    "中文型号": "提取装备中文型号或中文名称。",
    "英文型号": "提取装备英文型号、英文名称或原文型号。",
    "母港位置": "提取母港、驻泊地或基地位置。",
    "系列/族": "提取所属系列、族系或 class/family 名称。",
    "军种": "提取所属军种或使用方军种。",
    "服役状态": "提取在役、退役、建造中、计划中等状态（包括不同舰艇型号不同状态对应的数量也要提取）。",
    "总装厂商": "提取总装厂、建造厂、主承包商或系统集成商。",
    "海上移动识别码": "提取 MMSI 或海上移动业务识别码。",
    "舷号": "提取舷号、船体编号或 hull number。",
    "开工时间": "提取开工、铺龙骨、建造等状态开始时间。",
    "下水时间": "提取下水、出坞等状态的时间。",
    "服役时间": "提取服役、入役、交付等状态的时间。",
    "造价": "提取造价、单价、合同金额或项目成本。",
    "基本情况": "概括装备定位、用途、研制背景和总体情况。",
    "性能特点": "概括主要性能、能力优势和设计特点。",
    "标准排水量": "提取标准排水量数值及单位。",
    "满载排水量": "提取满载排水量数值及单位。",
    "长": "提取全长、总长或舰长数值。",
    "型宽": "提取型宽、最大宽度或 beam 数值。",
    "吃水": "提取吃水、draught/draft 数值。",
    "垂线间长": "提取垂线间长或 length between perpendiculars。",
    "水线宽": "提取水线宽或 waterline beam。",
    "飞行甲板长": "提取飞行甲板长度。",
    "飞行甲板宽": "提取飞行甲板宽度。",
    "机库长": "提取机库长度。",
    "机库宽": "提取机库宽度。",
    "机库高": "提取机库高度。",
    "军官人数": "提取军官人数。",
    "舰员": "提取舰员、船员或水兵人数。",
    "机组人数": "提取航空机组、飞行人员或航空部门人员人数。",
    "飞机升降机": "提取飞机升降机数量、位置或尺寸。",
    "舰载机持续出动架次率": "提取持续作业条件下舰载机出动架次率。",
    "舰载机高强度出动架次率": "提取高强度作业条件下舰载机出动架次率。",
    "载油量": "提取燃油、航空燃油或油料载量。",
    "油耗": "提取油耗、燃料消耗率或航行消耗条件。",
    "轴系与螺旋桨": "提取轴数、轴系布置、螺旋桨数量或形式。",
    "装置型号": "提取动力或推进装置型号。",
    "装置类型": "提取动力或推进装置类型。",
    "装置数量": "提取动力或推进装置数量。",
    "装置总功率": "提取动力或推进装置总功率。",
    "最大速度": "提取最大速度或最高航速。",
    "巡航速度": "提取巡航速度或经济航速。",
    "续航力": "提取续航力、航程或 endurance/range。",
    "最大加速度": "提取最大加速度或加速性能。",
    "最大转向速度": "提取最大转向速度或转向性能。",
    "雷达散射截面积": "提取 RCS、雷达散射截面积或隐身指标。",
    "光学截面积": "提取光学截面积或光学特征指标。",
    "红外特征值": "提取红外特征值或红外辐射指标。",
    "等效雷达高度": "提取等效雷达高度或相关探测特征。",
    "声强度值": "提取声强度、噪声级或声学特征值。",
    "雷达型号": "提取雷达系统型号。",
    "雷达种类": "提取雷达种类、用途或频段类型。",
    "雷达数量": "提取雷达数量或配置数量。",
    "对抗措施型号": "提取电子战、诱饵或干扰对抗措施型号。",
    "对抗措施种类": "提取对抗措施种类或用途类型。",
    "对抗措施数量": "提取对抗措施数量或配置数量。",
    "武器型号": "提取武器系统或弹药型号。",
    "武器种类": "提取武器种类或用途类型。",
    "武器数量": "提取武器数量、发射单元数或载弹量。",
    "搭载型号": "提取搭载的舰载机、直升机、无人机、艇或任务载荷型号。",
    "搭载型号种类": "提取搭载型号对应的类别。",
    "搭载型号数量": "提取搭载型号对应的数量。",
    "声纳型号": "提取声纳系统型号。",
    "声纳种类": "提取声纳种类、安装方式或用途类型。",
    "声纳数量": "提取声纳数量或配置数量。",
    "航保装置型号": "提取航空保障、起降、拦阻、弹射或甲板保障装置型号。",
    "航保装置种类": "提取航保装置类别或用途类型。",
    "航保装置数量": "提取航保装置数量或配置数量。",
}


class QuietDirectoryHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def load_env() -> None:
    """加载本地环境文件，并保留调用方已显式传入的环境变量。

    该目录测试脚本的 ``--dry-run`` 被设计为只依赖 Python 标准库，因此这里不能
    在模块导入阶段强制要求 ``python-dotenv``。正常项目虚拟环境中仍优先使用
    python-dotenv；当脚本由精简的系统 Python 启动时，则退化为读取本项目所需的
    简单 ``KEY=VALUE`` 配置，且始终遵守 ``override=False`` 的覆盖规则。
    """

    global RUNTIME_DIR, TASK_DB, KB_DB
    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path = ROOT / ".env.example"
    _load_env_file(env_path)

    RUNTIME_DIR = Path(os.getenv("DOCSENSE_RUNTIME_DIR", str(ROOT / ".runtime"))).expanduser()
    if not RUNTIME_DIR.is_absolute():
        raise RuntimeError("DOCSENSE_RUNTIME_DIR必须配置为绝对路径")
    RUNTIME_DIR = RUNTIME_DIR.resolve()
    TASK_DB = Path(
        os.getenv("DOCSENSE_LLM_TASK_DB", str(RUNTIME_DIR / "llm_tasks.sqlite3"))
    ).expanduser().resolve()
    KB_DB = Path(
        os.getenv(
            "DOCSENSE_KNOWLEDGE_BASE_DB",
            os.getenv("KNOWLEDGE_BASE_DB_PATH", str(RUNTIME_DIR / "knowledge_base.sqlite3")),
        )
    ).expanduser().resolve()


def _load_env_file(env_path: Path) -> None:
    """按“调用方环境优先”规则加载环境文件。

    fallback 解析器只处理仓库现有配置文件采用的 ``KEY=VALUE`` 形式；复杂 dotenv
    语法仍交由 python-dotenv 处理。这样既不削弱正式虚拟环境的能力，也可保证
    不访问服务的 dry-run 在依赖精简的诊断环境中正常执行。
    """

    if not env_path.is_file():
        return

    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        logger.debug(
            "未安装 python-dotenv，目录测试脚本使用标准库加载环境文件: path=%s",
            env_path,
        )
        for line_no, raw_line in enumerate(
            env_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                logger.debug(
                    "忽略无法按 KEY=VALUE 解析的环境配置行: path=%s line_no=%d",
                    env_path,
                    line_no,
                )
                continue
            key, value = stripped.split("=", 1)
            normalized_key = key.strip()
            if not normalized_key or normalized_key in os.environ:
                continue
            normalized_value = value.strip()
            if (
                len(normalized_value) >= 2
                and normalized_value[0] == normalized_value[-1]
                and normalized_value[0] in {"'", '"'}
            ):
                normalized_value = normalized_value[1:-1]
            os.environ[normalized_key] = normalized_value
        return

    load_dotenv(env_path, override=False)


def default_base_url() -> str:
    host = os.getenv("APP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.getenv("APP_PORT", "5001").strip() or "5001"
    return f"http://{host}:{port}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run /llm/analysis -> /llm/weaponry for every matched file in a directory.",
    )
    parser.add_argument("file_dir", help="包含待测文件的目录。默认只扫描当前目录下的 PDF。")
    parser.add_argument("--base-url", default="", help="DocSense 服务地址，默认读取 .env 的 APP_HOST/APP_PORT。")
    parser.add_argument("--pattern", action="append", default=None, help="文件匹配模式，可重复；默认：*.pdf。")
    parser.add_argument("--recursive", action="store_true", help="递归扫描 file_dir。")
    parser.add_argument(
        "--output-dir",
        default="",
        help="输出目录；默认写入 DOCSENSE_RUNTIME_DIR/weaponry_directory_<timestamp>。",
    )
    parser.add_argument("--output-prefix", default=DEFAULT_OUTPUT_PREFIX, help="输出文件名前缀。")
    parser.add_argument("--architecture-base", type=int, default=0, help="临时 architectureId 起始值；默认按时间生成。")
    parser.add_argument("--template-classify-id", type=int, default=1772442376645740)
    parser.add_argument("--static-host", default="127.0.0.1", help="自动静态文件服务监听地址。")
    parser.add_argument("--static-port", type=int, default=0, help="自动静态文件服务端口；0 表示自动分配。")
    parser.add_argument("--static-base", default="", help="已存在的静态文件服务 URL；提供后不再自动启动文件服务。")
    parser.add_argument("--analysis-timeout", type=int, default=1800)
    parser.add_argument("--weaponry-timeout", type=int, default=5400)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument(
        "--verify-forced-empty-contract",
        action="store_true",
        help=(
            "改用最小保留字段合同探针，并严格验证顶层/TABLE 空值、普通字段对照、"
            "callback 与 interaction audit；默认关闭，原 75 个 INPUT 字段不变。"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="只生成计划和 manifest，不请求 DocSense。")
    return parser.parse_args()


def discover_files(file_dir: Path, patterns: list[str], recursive: bool) -> list[Path]:
    if not file_dir.exists() or not file_dir.is_dir():
        raise FileNotFoundError(f"目录不存在: {file_dir}")
    files: list[Path] = []
    for pattern in patterns:
        iterator = file_dir.rglob(pattern) if recursive else file_dir.glob(pattern)
        files.extend(path for path in iterator if path.is_file())
    unique = sorted({path.resolve() for path in files})
    if not unique:
        raise FileNotFoundError(f"未找到匹配文件: dir={file_dir}, patterns={patterns}, recursive={recursive}")
    return unique


def start_static_server(file_dir: Path, host: str, port: int) -> tuple[ThreadingHTTPServer, str]:
    def handler(*args: Any, **kwargs: Any) -> QuietDirectoryHandler:
        return QuietDirectoryHandler(*args, directory=str(file_dir), **kwargs)

    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_host, actual_port = server.server_address[:2]
    public_host = "127.0.0.1" if actual_host in {"0.0.0.0", "::"} else str(actual_host)
    return server, f"http://{public_host}:{actual_port}"


def relative_url(static_base: str, file_dir: Path, file_path: Path) -> str:
    relative = file_path.relative_to(file_dir).as_posix()
    encoded = "/".join(quote(part) for part in relative.split("/"))
    return f"{static_base.rstrip('/')}/{encoded}"


def generated_architecture_base() -> int:
    return int(datetime.now().strftime("%m%d%H%M%S")) * 1000


def architecture_id_used(architecture_id: int) -> bool:
    if not KB_DB.exists():
        return False
    with closing(sqlite3.connect(KB_DB)) as conn, conn:
        workspace = conn.execute(
            "select 1 from workspaces where architecture_id=? limit 1",
            (architecture_id,),
        ).fetchone()
        document = conn.execute(
            "select 1 from documents where architecture_id=? limit 1",
            (architecture_id,),
        ).fetchone()
    return bool(workspace or document)


def allocate_architecture_ids(files: list[Path], base: int) -> dict[Path, int]:
    ids: dict[Path, int] = {}
    candidate = base
    for file_path in files:
        while architecture_id_used(candidate):
            candidate += 1
        ids[file_path] = candidate
        candidate += 1
    return ids


def request_json(method: str, base_url: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 60) -> dict[str, Any]:
    # dry-run 只生成计划，不应因为系统 Python 未安装 HTTP 依赖而无法执行；真实请求
    # 路径再延迟导入 requests，缺失依赖时仍会在首次联网前明确失败。
    import requests

    response = requests.request(f"{method}", f"{base_url.rstrip('/')}{path}", json=payload, timeout=timeout)
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {response.status_code}: {body}")
    return body


def get_task_snapshot(
    business_type: str,
    business_key: str | int,
) -> dict[str, Any] | None:
    """从 runner 与服务约定的同一个 TASK_DB 读取任务真相。

    ``/llm/check-task`` 仍会在轮询时调用，用于触发 callback recovery；其 HTTP 200
    响应允许为空，不能再作为任务状态或结果来源。
    """

    if not TASK_DB.exists():
        return None
    try:
        with closing(sqlite3.connect(TASK_DB)) as conn, conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT business_type, business_key, status, progress, message,
                       result_payload, callback_status, callback_attempts,
                       last_callback_error, execution_id, created_at, updated_at
                FROM llm_tasks
                WHERE business_type=? AND business_key=?
                """,
                (business_type, str(business_key)),
            ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(f"无法读取 TASK_DB={TASK_DB}: {exc}") from exc
    if row is None:
        return None
    result_payload: dict[str, Any] | None = None
    raw_result = row["result_payload"]
    if raw_result:
        try:
            decoded = json.loads(raw_result)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"TASK_DB={TASK_DB} 中 {business_type}/{business_key} 的 result_payload 不是合法 JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(
                f"TASK_DB={TASK_DB} 中 {business_type}/{business_key} 的 result_payload 必须为对象"
            )
        result_payload = decoded
    return {
        "business_type": str(row["business_type"]),
        "business_key": str(row["business_key"]),
        "status": str(row["status"]),
        "progress": row["progress"],
        "message": str(row["message"] or ""),
        "result_payload": result_payload,
        "callback_status": str(row["callback_status"] or ""),
        "callback_attempts": int(row["callback_attempts"] or 0),
        "last_callback_error": str(row["last_callback_error"] or ""),
        "execution_id": str(row["execution_id"] or ""),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def task_snapshot_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in snapshot.items()
        if key != "result_payload"
    }


def get_task_payload(business_type: str, business_key: str) -> dict[str, Any] | None:
    snapshot = get_task_snapshot(business_type, business_key)
    if snapshot is None:
        return None
    result_payload = snapshot.get("result_payload")
    return result_payload if isinstance(result_payload, dict) else None


def wait_task(
    base_url: str,
    business_type: str,
    key_name: str,
    key_value: str | int,
    *,
    timeout_seconds: int,
    poll_interval: float,
    label: str,
    wait_for_callback: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    start = time.time()
    last_observation: tuple[str, Any, str] | None = None
    while True:
        payload = {"businessType": business_type, "params": [{key_name: key_value}]}
        check_task_response = request_json(
            "POST",
            base_url,
            "/llm/check-task",
            payload,
            timeout=30,
        )
        snapshot = get_task_snapshot(business_type, key_value)
        if snapshot is None:
            raise RuntimeError(
                f"{label}: /llm/check-task 已返回 HTTP 200，但 TASK_DB={TASK_DB} "
                f"的 llm_tasks 中不存在 {business_type}/{key_value}；"
                "DocSense 服务与 runner 很可能使用了不一致的 DOCSENSE_LLM_TASK_DB"
            )
        status = str(snapshot["status"])
        progress = snapshot["progress"]
        callback_status = str(snapshot["callback_status"])
        observation = (status, progress, callback_status)
        if observation != last_observation:
            logger.info(
                "%s status=%s progress=%s callback=%s",
                label,
                status,
                progress,
                callback_status,
            )
            last_observation = observation
        if status in {"2", "3"}:
            if (
                not wait_for_callback
                or callback_status not in {"pending", "sending"}
            ):
                return snapshot, check_task_response
        if time.time() - start > timeout_seconds:
            raise TimeoutError(
                f"{label} timed out after {timeout_seconds}s: "
                f"{task_snapshot_summary(snapshot)}"
            )
        time.sleep(poll_interval)


def build_analysis_payload(file_name: str, file_url: str, architecture_id: int) -> dict[str, Any]:
    node = {
        "id": architecture_id,
        "name": "水面装备",
        "parentId": None,
        "path": str(architecture_id),
        "pathName": "水面装备",
        "remark": f"自动化 weaponry 测试节点，仅用于 {file_name} 的水面舰艇装备资料抽取。",
    }
    return {
        "businessType": "file",
        "params": [
            {
                "fileName": file_name,
                "originalName": file_name,
                "originalFileName": file_name,
                "filePath": file_url,
                "country": [{"key": "02", "value": "美国"}],
                "channel": [{"key": "03", "value": "军情"}],
                "format": [{"key": "03", "value": "文档类"}],
                "maturity": [{"key": "03", "value": "定型成果"}],
                "architectureList": [node],
                "architectureStandardList": [],
            }
        ],
    }


def _weaponry_target_suffix(
    target_file_name: str,
    *,
    generic_file: bool = False,
) -> str:
    target_name_label = "文件名" if generic_file else " PDF 文件名"
    target_fact_label = "目标文件" if generic_file else "目标 PDF "
    return (
        f"本次抽取的唯一目标{target_name_label}是 {target_file_name}。"
        f"仅依据该{target_fact_label}的明确装备事实抽取；terms/ 术语文件、term_rule_ 开头文件只用于理解字段定义和抽取口径，"
        "不是目标装备资料，不得把术语规则、字段定义或示例作为 analyseData。"
        f"如果检索片段的来源文件不是 {target_file_name}，只能把它当作术语参考，不能从中抽取取值。"
        f"如果当前上下文只来自术语文件或不含{target_fact_label}的装备事实，请只回答\"未找到\"；"
        f"找不到{target_fact_label}明确依据时也请只回答\"未找到\"，不要猜测。"
    )


def _input_field_template(
    name: str,
    *,
    description: str,
    template_classify_id: int,
) -> dict[str, Any]:
    return {
        "templateClassifyId": template_classify_id,
        "fieldName": name,
        "fieldType": "INPUT",
        "fieldDescription": description,
        "analyseData": "",
        "analyseDataSource": [],
    }


def _table_column_template(name: str, *, description: str) -> dict[str, Any]:
    return {
        "fieldName": name,
        "fieldType": "INPUT",
        "fieldDescription": description,
        "analyseData": "",
        "analyseDataSource": [],
    }


def build_weaponry_payload(
    architecture_id: int,
    target_file_name: str,
    template_classify_id: int,
    *,
    verify_forced_empty_contract: bool = False,
) -> dict[str, Any]:
    suffix = _weaponry_target_suffix(
        target_file_name,
        generic_file=verify_forced_empty_contract,
    )
    if verify_forced_empty_contract:
        fields = [
            _input_field_template(
                name,
                description=f"{FIELD_DESCRIPTIONS[name]} {suffix}",
                template_classify_id=template_classify_id,
            )
            for name in FORCED_EMPTY_CONTRACT_INPUT_FIELDS
        ]
        fields.append(
            _input_field_template(
                FORCED_EMPTY_CONTRACT_CONTROL_FIELD,
                description=(
                    f"{FIELD_DESCRIPTIONS[FORCED_EMPTY_CONTRACT_CONTROL_FIELD]} {suffix}"
                ),
                template_classify_id=template_classify_id,
            )
        )
        mixed_columns = [
            _table_column_template(
                name,
                description=(
                    f"{FIELD_DESCRIPTIONS.get(name, '提取单舰正式名称。')} {suffix}"
                ),
            )
            for name in FORCED_EMPTY_CONTRACT_MIXED_COLUMNS
        ]
        reserved_columns = [
            _table_column_template(
                name,
                description=f"{FIELD_DESCRIPTIONS[name]} {suffix}",
            )
            for name in FORCED_EMPTY_CONTRACT_INPUT_FIELDS
        ]
        fields.extend(
            [
                {
                    "templateClassifyId": template_classify_id,
                    "fieldName": FORCED_EMPTY_CONTRACT_MIXED_TABLE_FIELD,
                    "fieldType": "TABLE",
                    "fieldDescription": (
                        "按单舰逐行抽取单舰名称和舷号；五个甲方保留列必须保持空值。 "
                        f"{suffix}"
                    ),
                    "tableFieldList": [mixed_columns],
                },
                {
                    "templateClassifyId": template_classify_id,
                    "fieldName": FORCED_EMPTY_CONTRACT_RESERVED_TABLE_FIELD,
                    "fieldType": "TABLE",
                    "fieldDescription": (
                        "仅用于验证五个甲方保留列的确定性空值合同，不得检索或调用模型。"
                    ),
                    "tableFieldList": [reserved_columns],
                },
            ]
        )
    else:
        fields = [
            _input_field_template(
                name,
                description=f"{FIELD_DESCRIPTIONS[name]} {suffix}",
                template_classify_id=template_classify_id,
            )
            for name in FIELD_NAMES
        ]
    return {
        "businessType": "weaponry",
        "params": {
            "architectureId": architecture_id,
            "weaponryTemplateFieldList": fields,
        },
    }


def kb_records_for(architecture_id: int) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(KB_DB)) as conn, conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select w.architecture_id, w.workspace_slug, d.file_name, d.original_name, d.doc_path, d.anything_doc_id
            from workspaces w
            left join documents d on d.architecture_id = w.architecture_id
            where w.architecture_id = ?
            order by d.file_name
            """,
            (architecture_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def extract_fields(result_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(result_payload, dict):
        return []
    data = result_payload.get("data")
    if not isinstance(data, dict):
        return []
    fields = data.get("weaponryTemplateFieldList")
    return fields if isinstance(fields, list) else []


def normalize_missing(value: str) -> str:
    text = (value or "").strip()
    if text in {"", "未找到", "未找到明确依据", "null", "None", "无", "\"\"", "''"}:
        return "未找到明确依据"
    return text


def source_info(field: dict[str, Any]) -> dict[str, Any]:
    sources = field.get("analyseDataSource")
    if not isinstance(sources, list) or not sources:
        return {"source_count": 0, "first_source": "", "first_content": "", "source_list": ""}
    source_names = []
    for source in sources:
        if isinstance(source, dict):
            source_names.append(str(source.get("source") or ""))
    first = sources[0] if isinstance(sources[0], dict) else {}
    return {
        "source_count": len(sources),
        "first_source": str(first.get("source") or ""),
        "first_content": str(first.get("content") or "").replace("\n", " ").strip()[:500],
        "source_list": ", ".join(name for name in source_names if name),
    }


def get_weaponry_interaction_audits(task_id: str) -> list[dict[str, Any]]:
    if not task_id:
        raise RuntimeError("weaponry task snapshot 缺少 execution_id，无法核对 interaction audit")
    if not TASK_DB.exists():
        raise RuntimeError(f"TASK_DB 不存在: {TASK_DB}")
    try:
        with closing(sqlite3.connect(TASK_DB)) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT task_id, business_key, call_id, operation, field_sequence,
                       document_sequence, item_sequence, attempt_no, state, outcome,
                       created_at, updated_at
                FROM weaponry_interaction_audits
                WHERE task_id=?
                ORDER BY field_sequence, operation, document_sequence,
                         item_sequence, attempt_no
                """,
                (task_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"无法从 TASK_DB={TASK_DB} 读取 weaponry_interaction_audits: {exc}"
        ) from exc
    return [dict(row) for row in rows]


def _empty_contract_errors(field: object, *, subject: str) -> list[str]:
    if not isinstance(field, dict):
        return [f"{subject}: missing"]
    errors: list[str] = []
    if field.get("analyseData") != "":
        errors.append(f"{subject}: analyseData_not_empty")
    if field.get("analyseDataSource") != [STANDARD_EMPTY_DATA_SOURCE]:
        errors.append(f"{subject}: analyseDataSource_not_standard_empty_placeholder")
    return errors


def _has_real_evidence_source(field: object) -> bool:
    if not isinstance(field, dict):
        return False
    sources = field.get("analyseDataSource")
    if not isinstance(sources, list):
        return False
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_name = str(source.get("source") or source.get("fileName") or "").strip()
        rows = source.get("rows")
        if (
            source_name
            and isinstance(rows, list)
            and any(str(row or "").strip() for row in rows)
        ):
            return True
    return False


def _validated_table_rows(
    field: object,
    *,
    subject: str,
    expected_columns: tuple[str, ...],
    expected_row_count: int | None = None,
) -> tuple[list[list[dict[str, Any]]], list[str]]:
    if not isinstance(field, dict):
        return [], [f"{subject}: missing"]
    raw_rows = field.get("tableFieldList")
    if not isinstance(raw_rows, list):
        return [], [f"{subject}: rows_missing"]
    errors: list[str] = []
    if expected_row_count is not None and len(raw_rows) != expected_row_count:
        errors.append(
            f"{subject}: expected_{expected_row_count}_rows_got_{len(raw_rows)}"
        )
    elif not raw_rows:
        errors.append(f"{subject}: rows_missing")
    rows: list[list[dict[str, Any]]] = []
    for row_index, raw_row in enumerate(raw_rows, start=1):
        if not isinstance(raw_row, list):
            errors.append(f"{subject}.row{row_index}: row_not_array")
            rows.append([])
            continue
        names = tuple(
            str(cell.get("fieldName") or "")
            if isinstance(cell, dict)
            else "<non-object>"
            for cell in raw_row
        )
        if names != expected_columns:
            errors.append(
                f"{subject}.row{row_index}: expected_columns_"
                f"{','.join(expected_columns)}_got_{','.join(names)}"
            )
        rows.append([cell for cell in raw_row if isinstance(cell, dict)])
    return rows, errors


def verify_forced_empty_contract(
    result_payload: dict[str, Any] | None,
    task_snapshot: dict[str, Any],
    interaction_rows: list[dict[str, Any]] | None,
    *,
    analysis_task_snapshot: dict[str, Any] | None = None,
    interaction_error: str = "",
) -> dict[str, Any]:
    fields = extract_fields(result_payload)
    fields_by_name = {
        str(field.get("fieldName") or ""): field
        for field in fields
        if isinstance(field, dict)
    }
    errors: list[str] = []

    top_level_errors: list[str] = []
    for name in FORCED_EMPTY_CONTRACT_INPUT_FIELDS:
        top_level_errors.extend(
            _empty_contract_errors(
                fields_by_name.get(name),
                subject=f"top_level.{name}",
            )
        )
    errors.extend(top_level_errors)

    control_field = fields_by_name.get(FORCED_EMPTY_CONTRACT_CONTROL_FIELD)
    control_value = (
        str(control_field.get("analyseData") or "").strip()
        if isinstance(control_field, dict)
        else ""
    )
    control_has_source = _has_real_evidence_source(control_field)
    control_errors: list[str] = []
    if not control_value:
        control_errors.append("control.舷号: analyseData_empty")
    if not control_has_source:
        control_errors.append("control.舷号: real_evidence_source_missing")
    errors.extend(control_errors)

    mixed_field = fields_by_name.get(FORCED_EMPTY_CONTRACT_MIXED_TABLE_FIELD)
    mixed_rows, mixed_errors = _validated_table_rows(
        mixed_field,
        subject="mixed_table",
        expected_columns=FORCED_EMPTY_CONTRACT_MIXED_COLUMNS,
    )
    for row_index, row in enumerate(mixed_rows, start=1):
        cells = {str(cell.get("fieldName") or ""): cell for cell in row}
        for name in FORCED_EMPTY_CONTRACT_INPUT_FIELDS:
            mixed_errors.extend(
                _empty_contract_errors(
                    cells.get(name),
                    subject=f"mixed_table.row{row_index}.{name}",
                )
            )
        for name in ("单舰名称", "舷号"):
            cell = cells.get(name)
            value = (
                str(cell.get("analyseData") or "").strip()
                if isinstance(cell, dict)
                else ""
            )
            if not value:
                mixed_errors.append(
                    f"mixed_table.row{row_index}.{name}: analyseData_empty"
                )
            if not _has_real_evidence_source(cell):
                mixed_errors.append(
                    f"mixed_table.row{row_index}.{name}: real_evidence_source_missing"
                )
    errors.extend(mixed_errors)

    reserved_table_field = fields_by_name.get(
        FORCED_EMPTY_CONTRACT_RESERVED_TABLE_FIELD
    )
    reserved_rows, reserved_table_errors = _validated_table_rows(
        reserved_table_field,
        subject="reserved_table",
        expected_columns=FORCED_EMPTY_CONTRACT_INPUT_FIELDS,
        expected_row_count=1,
    )
    for row_index, row in enumerate(reserved_rows, start=1):
        cells = {str(cell.get("fieldName") or ""): cell for cell in row}
        for name in FORCED_EMPTY_CONTRACT_INPUT_FIELDS:
            reserved_table_errors.extend(
                _empty_contract_errors(
                    cells.get(name),
                    subject=f"reserved_table.row{row_index}.{name}",
                )
            )
    errors.extend(reserved_table_errors)

    analysis_status = (
        str(analysis_task_snapshot.get("status") or "")
        if isinstance(analysis_task_snapshot, dict)
        else ""
    )
    analysis_callback_status = (
        str(analysis_task_snapshot.get("callback_status") or "")
        if isinstance(analysis_task_snapshot, dict)
        else ""
    )
    weaponry_status = str(task_snapshot.get("status") or "")
    weaponry_callback_status = str(task_snapshot.get("callback_status") or "")
    callback_errors: list[str] = []
    if analysis_status != "2":
        callback_errors.append(
            f"analysis: expected_status_2_got_{analysis_status or 'unavailable'}"
        )
    if analysis_callback_status != "success":
        callback_errors.append(
            "analysis_callback: "
            f"expected_success_got_{analysis_callback_status or 'unavailable'}"
        )
    if weaponry_status != "2":
        callback_errors.append(
            f"weaponry: expected_status_2_got_{weaponry_status or 'empty'}"
        )
    if weaponry_callback_status != "success":
        callback_errors.append(
            "weaponry_callback: "
            f"expected_success_got_{weaponry_callback_status or 'empty'}"
        )
    errors.extend(callback_errors)

    operations_by_sequence: dict[str, list[str]] = {}
    audit_errors: list[str] = []
    if interaction_error:
        audit_errors.append(f"interaction_audit: {interaction_error}")
    elif interaction_rows is None:
        audit_errors.append("interaction_audit: unavailable")
    else:
        for row in interaction_rows:
            sequence = str(row.get("field_sequence") or "")
            operation = str(row.get("operation") or "")
            if sequence and operation:
                operations_by_sequence.setdefault(sequence, []).append(operation)
        operations_by_sequence = {
            sequence: sorted(set(operations))
            for sequence, operations in operations_by_sequence.items()
        }
        for sequence in (1, 2, 3, 4, 5, 8):
            operations = operations_by_sequence.get(str(sequence), [])
            if operations:
                audit_errors.append(
                    f"interaction_audit.field_sequence_{sequence}: "
                    f"expected_zero_interactions_got_{','.join(operations)}"
                )
        required_operations = {"target_retrieval", "evidence_extraction"}
        for sequence in (6, 7):
            operations = set(operations_by_sequence.get(str(sequence), []))
            missing = sorted(required_operations - operations)
            if missing:
                audit_errors.append(
                    f"interaction_audit.field_sequence_{sequence}: "
                    f"missing_{','.join(missing)}"
                )
    errors.extend(audit_errors)

    return {
        "passed": not errors,
        "errors": errors,
        "callback": {
            "passed": not callback_errors,
            "analysis": {
                "status": analysis_status,
                "callback_status": analysis_callback_status,
                "attempts": int(
                    analysis_task_snapshot.get("callback_attempts") or 0
                )
                if isinstance(analysis_task_snapshot, dict)
                else 0,
                "last_error": str(
                    analysis_task_snapshot.get("last_callback_error") or ""
                )
                if isinstance(analysis_task_snapshot, dict)
                else "",
            },
            "weaponry": {
                "status": weaponry_status,
                "callback_status": weaponry_callback_status,
                "attempts": int(task_snapshot.get("callback_attempts") or 0),
                "last_error": str(task_snapshot.get("last_callback_error") or ""),
            },
            "errors": callback_errors,
        },
        "top_level_forced_empty": {
            "passed": not top_level_errors,
            "field_names": list(FORCED_EMPTY_CONTRACT_INPUT_FIELDS),
            "errors": top_level_errors,
        },
        "ordinary_control": {
            "passed": not control_errors,
            "field_name": FORCED_EMPTY_CONTRACT_CONTROL_FIELD,
            "value": control_value,
            "has_real_evidence_source": control_has_source,
            "errors": control_errors,
        },
        "mixed_table": {
            "passed": not mixed_errors,
            "field_name": FORCED_EMPTY_CONTRACT_MIXED_TABLE_FIELD,
            "row_count": len(mixed_rows),
            "errors": mixed_errors,
        },
        "reserved_only_table": {
            "passed": not reserved_table_errors,
            "field_name": FORCED_EMPTY_CONTRACT_RESERVED_TABLE_FIELD,
            "row_count": len(reserved_rows),
            "errors": reserved_table_errors,
        },
        "interaction_audit": {
            "passed": not audit_errors,
            "task_id": str(task_snapshot.get("execution_id") or ""),
            "row_count": len(interaction_rows or []),
            "operations_by_field_sequence": operations_by_sequence,
            "errors": audit_errors,
        },
    }


def summarize_input_field_statistics(
    fields_by_name: dict[str, dict[str, Any]],
    field_names: tuple[str, ...],
) -> dict[str, Any]:
    extractable = tuple(
        name for name in field_names if not is_forced_empty_field_name(name)
    )
    non_empty_count = 0
    forced_empty_violations: list[str] = []
    forced_empty_source_violations: list[str] = []
    missing_fields: list[str] = []
    for name in field_names:
        field = fields_by_name.get(name, {})
        value = str(field.get("analyseData") or "").strip()
        if is_forced_empty_field_name(name):
            if value:
                forced_empty_violations.append(name)
            if field.get("analyseDataSource") != [STANDARD_EMPTY_DATA_SOURCE]:
                forced_empty_source_violations.append(name)
        elif value:
            non_empty_count += 1
        else:
            missing_fields.append(name)
    return {
        "extractable_field_names": extractable,
        "extractable_field_count": len(extractable),
        "non_empty_count": non_empty_count,
        "missing_fields": missing_fields,
        "forced_empty_violations": forced_empty_violations,
        "forced_empty_source_violations": forced_empty_source_violations,
    }


def markdown_escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", "<br>")


def write_outputs(
    out_dir: Path,
    output_prefix: str,
    rows: list[dict[str, Any]],
    audit_rows: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> None:
    csv_path = out_dir / f"{output_prefix}.csv"
    md_path = out_dir / f"{output_prefix}.md"
    audit_csv_path = out_dir / f"{output_prefix}_source_audit.csv"
    audit_json_path = out_dir / f"{output_prefix}_source_audit.json"
    manifest_path = out_dir / f"{output_prefix}_manifest.json"

    input_field_names = manifest.get("input_field_names")
    if not isinstance(input_field_names, list) or not all(
        isinstance(name, str) and name for name in input_field_names
    ):
        input_field_names = list(FIELD_NAMES)
    headers = ["文件名", *input_field_names]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)

    md_lines = [
        "# 武器装备知识谱系抽取结果汇总",
        "",
        f"- 运行目录：`{out_dir}`",
        f"- 输出前缀：`{output_prefix}`",
        f"- 完成文件数：`{len(rows)}/{len(manifest.get('files', []))}`",
        "- weaponry 模式：固定 `file_aggregate_v1`，无需设置运行时模式变量。",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        md_lines.append("| " + " | ".join(markdown_escape(row.get(h, "")) for h in headers) + " |")
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    audit_headers = [
        "文件名",
        "architectureId",
        "字段",
        "抽取值",
        "来源数量",
        "首个来源文件",
        "首个来源内容",
        "来源文件列表",
    ]
    with audit_csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=audit_headers)
        writer.writeheader()
        writer.writerows(audit_rows)
    audit_json_path.write_text(json.dumps(audit_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_doc_sense_reachable(base_url: str) -> None:
    try:
        request_json("POST", base_url, "/llm/check-task", {"businessType": "file", "params": [{"fileName": "__probe__"}]}, timeout=10)
    except RuntimeError:
        # 业务不存在也说明服务可达；连接失败会走 requests 异常。
        return
    except requests.RequestException as exc:
        raise RuntimeError(f"DocSense 服务不可访问: {base_url}") from exc


def probe_anythingllm() -> None:
    config = load_anythingllm_config()
    with AnythingLLMTransport(
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=config.timeout,
    ) as transport:
        # 使用原子 Workspace Client 完成协议级探测；失败只暴露稳定异常类型，
        # 不再拼接可能包含供应商响应正文或凭据的底层 HTTP 文本。
        AnythingLLMWorkspaceClient(transport).list_workspaces(user_id=1)


def run_file(
    *,
    base_url: str,
    file_dir: Path,
    file_path: Path,
    static_base: str,
    architecture_id: int,
    out_dir: Path,
    template_classify_id: int,
    analysis_timeout: int,
    weaponry_timeout: int,
    poll_interval: float,
    verify_forced_empty_contract_flag: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    file_name = file_path.name
    file_out_dir = out_dir / file_path.stem
    file_out_dir.mkdir(parents=True, exist_ok=True)
    file_url = relative_url(static_base, file_dir, file_path)

    analysis_payload = build_analysis_payload(file_name, file_url, architecture_id)
    (file_out_dir / "analysis_request.json").write_text(
        json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("POST /llm/analysis %s", file_name)
    analysis_submit = request_json("POST", base_url, "/llm/analysis", analysis_payload, timeout=60)
    (file_out_dir / "analysis_submit_response.json").write_text(
        json.dumps(analysis_submit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    analysis_state, analysis_check_task_response = wait_task(
        base_url,
        "file",
        "fileName",
        file_name,
        timeout_seconds=analysis_timeout,
        poll_interval=poll_interval,
        label=f"analysis {file_name}",
        wait_for_callback=verify_forced_empty_contract_flag,
    )
    (file_out_dir / "analysis_check_task_response.json").write_text(
        json.dumps(
            {
                "http_response": analysis_check_task_response,
                "task_db": str(TASK_DB),
                "task": task_snapshot_summary(analysis_state),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    analysis_result = analysis_state.get("result_payload")
    (file_out_dir / "analysis_task_result.json").write_text(
        json.dumps(analysis_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if str(analysis_state.get("status")) != "2":
        raise RuntimeError(
            f"analysis failed for {file_name}: {task_snapshot_summary(analysis_state)}"
        )

    kb_records = kb_records_for(architecture_id)
    (file_out_dir / "knowledge_base_records.json").write_text(
        json.dumps(kb_records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    related_docs = [record for record in kb_records if record.get("file_name")]
    if len(related_docs) != 1 or related_docs[0].get("file_name") != file_name:
        raise RuntimeError(f"architectureId={architecture_id} isolation check failed: {kb_records}")
    logger.info("KB isolation OK workspace=%s", related_docs[0].get("workspace_slug"))

    weaponry_payload = build_weaponry_payload(
        architecture_id,
        file_name,
        template_classify_id,
        verify_forced_empty_contract=verify_forced_empty_contract_flag,
    )
    (file_out_dir / "weaponry_request.json").write_text(
        json.dumps(weaponry_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("POST /llm/weaponry arch=%s", architecture_id)
    weaponry_submit = request_json("POST", base_url, "/llm/weaponry", weaponry_payload, timeout=60)
    (file_out_dir / "weaponry_submit_response.json").write_text(
        json.dumps(weaponry_submit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    weaponry_state, weaponry_check_task_response = wait_task(
        base_url,
        "weaponry",
        "architectureId",
        architecture_id,
        timeout_seconds=weaponry_timeout,
        poll_interval=poll_interval,
        label=f"weaponry {file_name}",
        wait_for_callback=verify_forced_empty_contract_flag,
    )
    (file_out_dir / "weaponry_check_task_response.json").write_text(
        json.dumps(
            {
                "http_response": weaponry_check_task_response,
                "task_db": str(TASK_DB),
                "task": task_snapshot_summary(weaponry_state),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    weaponry_result = weaponry_state.get("result_payload")
    (file_out_dir / "weaponry_task_result.json").write_text(
        json.dumps(weaponry_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if str(weaponry_state.get("status")) != "2":
        raise RuntimeError(
            f"weaponry failed for {file_name}: {task_snapshot_summary(weaponry_state)}"
        )

    fields = extract_fields(weaponry_result)
    fields_by_name = {field.get("fieldName"): field for field in fields if isinstance(field, dict)}
    summary_field_names = (
        (*FORCED_EMPTY_CONTRACT_INPUT_FIELDS, FORCED_EMPTY_CONTRACT_CONTROL_FIELD)
        if verify_forced_empty_contract_flag
        else tuple(FIELD_NAMES)
    )
    field_statistics = summarize_input_field_statistics(
        fields_by_name,
        summary_field_names,
    )
    row: dict[str, Any] = {"文件名": file_name}
    audit_rows: list[dict[str, Any]] = []
    term_source_fields: list[str] = []
    for field_name in summary_field_names:
        field = fields_by_name.get(field_name, {})
        value = str(field.get("analyseData") or "").strip()
        row[field_name] = normalize_missing(value)
        info = source_info(field)
        if value and "term_rule_" in info["source_list"]:
            term_source_fields.append(field_name)
        audit_rows.append(
            {
                "文件名": file_name,
                "architectureId": architecture_id,
                "字段": field_name,
                "抽取值": normalize_missing(value),
                "来源数量": info["source_count"],
                "首个来源文件": info["first_source"],
                "首个来源内容": info["first_content"],
                "来源文件列表": info["source_list"],
            }
        )

    contract_verification: dict[str, Any] | None = None
    if verify_forced_empty_contract_flag:
        interaction_rows: list[dict[str, Any]] | None = None
        interaction_error = ""
        try:
            interaction_rows = get_weaponry_interaction_audits(
                str(weaponry_state.get("execution_id") or "")
            )
        except RuntimeError as exc:
            interaction_error = str(exc)
        (file_out_dir / "weaponry_interaction_audit.json").write_text(
            json.dumps(
                {
                    "task_db": str(TASK_DB),
                    "task_id": str(weaponry_state.get("execution_id") or ""),
                    "error": interaction_error,
                    "rows": interaction_rows or [],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        contract_verification = verify_forced_empty_contract(
            weaponry_result if isinstance(weaponry_result, dict) else None,
            weaponry_state,
            interaction_rows,
            analysis_task_snapshot=analysis_state,
            interaction_error=interaction_error,
        )
        (file_out_dir / "forced_empty_contract_verification.json").write_text(
            json.dumps(contract_verification, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    file_manifest = {
        "fileName": file_name,
        "sourcePath": str(file_path),
        "fileUrl": file_url,
        "architectureId": architecture_id,
        "task_db": str(TASK_DB),
        "analysis_state": task_snapshot_summary(analysis_state),
        "weaponry_state": task_snapshot_summary(weaponry_state),
        "callbacks": {
            "analysis": {
                "status": str(analysis_state.get("status") or ""),
                "callback_status": str(
                    analysis_state.get("callback_status") or ""
                ),
                "attempts": int(analysis_state.get("callback_attempts") or 0),
                "last_error": str(
                    analysis_state.get("last_callback_error") or ""
                ),
            },
            "weaponry": {
                "status": str(weaponry_state.get("status") or ""),
                "callback_status": str(
                    weaponry_state.get("callback_status") or ""
                ),
                "attempts": int(weaponry_state.get("callback_attempts") or 0),
                "last_error": str(
                    weaponry_state.get("last_callback_error") or ""
                ),
            },
        },
        "field_count": len(fields),
        "input_field_count": len(summary_field_names),
        "extractable_field_count": field_statistics["extractable_field_count"],
        "non_empty_count": field_statistics["non_empty_count"],
        "missing_fields": field_statistics["missing_fields"],
        "forced_empty_fields": list(FORCED_EMPTY_CONTRACT_INPUT_FIELDS),
        "forced_empty_violations": field_statistics["forced_empty_violations"],
        "forced_empty_source_violations": field_statistics[
            "forced_empty_source_violations"
        ],
        "term_source_fields": term_source_fields,
        "workspace_records": kb_records,
    }
    if contract_verification is not None:
        file_manifest["forced_empty_contract_verification"] = contract_verification
    return row, audit_rows, file_manifest


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s [%(name)s:%(lineno)d] %(message)s",
    )
    load_env()
    args = parse_args()

    file_dir = Path(args.file_dir).expanduser()
    if not file_dir.is_absolute():
        file_dir = (ROOT / file_dir).resolve()
    else:
        file_dir = file_dir.resolve()

    patterns = args.pattern or ["*.pdf"]
    files = discover_files(file_dir, patterns, args.recursive)
    architecture_base = args.architecture_base or generated_architecture_base()
    architecture_ids = allocate_architecture_ids(files, architecture_base)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir).expanduser() if args.output_dir else RUNTIME_DIR / f"weaponry_directory_{run_id}"
    if not out_dir.is_absolute():
        out_dir = (ROOT / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=False)

    base_url = args.base_url.strip() or default_base_url()
    template_field_count = (
        8 if args.verify_forced_empty_contract else len(FIELD_NAMES)
    )
    input_field_count = (
        len(FORCED_EMPTY_CONTRACT_INPUT_FIELDS) + 1
        if args.verify_forced_empty_contract
        else len(FIELD_NAMES)
    )
    extractable_field_count = (
        1 if args.verify_forced_empty_contract else len(EXTRACTABLE_FIELD_NAMES)
    )
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "base_url": base_url,
        "runtime_dir": str(RUNTIME_DIR),
        "task_db": str(TASK_DB),
        "knowledge_base_db": str(KB_DB),
        "file_dir": str(file_dir),
        "patterns": patterns,
        "recursive": args.recursive,
        "output_dir": str(out_dir),
        "output_prefix": args.output_prefix,
        "architecture_base": architecture_base,
        "field_count": template_field_count,
        "input_field_count": input_field_count,
        "input_field_names": (
            [
                *FORCED_EMPTY_CONTRACT_INPUT_FIELDS,
                FORCED_EMPTY_CONTRACT_CONTROL_FIELD,
            ]
            if args.verify_forced_empty_contract
            else list(FIELD_NAMES)
        ),
        "extractable_field_count": extractable_field_count,
        "forced_empty_fields": list(FORCED_EMPTY_CONTRACT_INPUT_FIELDS),
        "verify_forced_empty_contract": bool(args.verify_forced_empty_contract),
        "started_at": datetime.now().isoformat(),
        "dry_run": bool(args.dry_run),
        "files": [
            {
                "fileName": path.name,
                "sourcePath": str(path),
                "architectureId": architecture_ids[path],
            }
            for path in files
        ],
    }

    if args.dry_run:
        write_outputs(out_dir, args.output_prefix, [], [], manifest)
        logger.info("DRY_RUN OUT_DIR=%s files=%d", out_dir, len(files))
        return 0

    ensure_doc_sense_reachable(base_url)
    probe_anythingllm()

    static_server: ThreadingHTTPServer | None = None
    if args.static_base.strip():
        static_base = args.static_base.strip().rstrip("/")
    else:
        static_server, static_base = start_static_server(file_dir, args.static_host, args.static_port)
    manifest["static_base"] = static_base

    rows: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    write_outputs(out_dir, args.output_prefix, rows, audit_rows, manifest)

    try:
        for index, file_path in enumerate(files, 1):
            architecture_id = architecture_ids[file_path]
            logger.info(
                "START %d/%d %s arch=%s",
                index,
                len(files),
                file_path.name,
                architecture_id,
            )
            row, file_audit_rows, file_manifest = run_file(
                base_url=base_url,
                file_dir=file_dir,
                file_path=file_path,
                static_base=static_base,
                architecture_id=architecture_id,
                out_dir=out_dir,
                template_classify_id=args.template_classify_id,
                analysis_timeout=args.analysis_timeout,
                weaponry_timeout=args.weaponry_timeout,
                poll_interval=args.poll_interval,
                verify_forced_empty_contract_flag=args.verify_forced_empty_contract,
            )
            rows.append(row)
            audit_rows.extend(file_audit_rows)
            manifest["files"][index - 1].update(file_manifest)
            manifest["completed_rows"] = len(rows)
            manifest["updated_at"] = datetime.now().isoformat()
            write_outputs(out_dir, args.output_prefix, rows, audit_rows, manifest)
            verification = file_manifest.get("forced_empty_contract_verification")
            if (
                args.verify_forced_empty_contract
                and (
                    not isinstance(verification, dict)
                    or not verification.get("passed")
                )
            ):
                raise RuntimeError(
                    f"{file_path.name} 保留字段合同探针失败: "
                    f"{verification.get('errors', []) if isinstance(verification, dict) else 'missing verification'}"
                )
            logger.info(
                "DONE %s non_empty=%s/%d term_source_fields=%s",
                file_path.name,
                file_manifest["non_empty_count"],
                file_manifest["extractable_field_count"],
                file_manifest["term_source_fields"],
            )
    finally:
        if static_server is not None:
            static_server.shutdown()
            static_server.server_close()

    manifest["finished_at"] = datetime.now().isoformat()
    manifest["completed_rows"] = len(rows)
    write_outputs(out_dir, args.output_prefix, rows, audit_rows, manifest)
    logger.info("DONE OUT_DIR=%s completed_rows=%d", out_dir, len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
