#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
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

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.core.config import load_anythingllm_config  # noqa: E402
from app.services.utils.anythingllm_client import AnythingLLMClient  # noqa: E402


TASK_DB = ROOT / ".runtime" / "llm_tasks.sqlite3"
KB_DB = ROOT / ".runtime" / "knowledge_base.sqlite3"
DEFAULT_OUTPUT_PREFIX = "qwen3-4b-new"

FIELD_NAMES = [
    "舰级名称",
    "资料信息",
    "装备类型",
    "服役状态",
    "舰艇清单",
    "建造单位",
    "建造节点",
    "排水量",
    "全长",
    "全宽",
    "吃水",
    "航空能力",
    "最高航速",
    "航程续航",
    "人员容量",
    "动力推进",
    "导弹垂发",
    "火炮反潜",
    "传感指控",
    "项目改装",
]

FIELD_DESCRIPTIONS = {
    "舰级名称": "提取该资料对应舰级、级别或型号名称；如只出现英文舰级名，保留原文。",
    "资料信息": "提取资料标题、发布日期、出版物、更新状态、版权机构等资料元信息。",
    "装备类型": "提取舰艇类别、用途类别或 Jane's 标注的装备类型。",
    "服役状态": "提取舰艇总数、在役、建造中、退役或其他状态描述。",
    "舰艇清单": "提取该舰级包含的舰名、舷号及状态清单。",
    "建造单位": "提取建造厂、承包商、系统集成商或相关建造单位。",
    "建造节点": "提取铺龙骨、下水、服役、交付、海试、合同等时间节点。",
    "排水量": "提取标准、满载或其他排水量指标。",
    "全长": "提取舰艇总长或 overall length。",
    "全宽": "提取舰宽、型宽或 beam。",
    "吃水": "提取吃水、draught/draft 等指标。",
    "航空能力": "提取舰载机、直升机、无人机、机库、飞行甲板、航空燃油或航空人员能力。",
    "最高航速": "提取最高航速、top speed 或 speed。",
    "航程续航": "提取航程、续航力、航速条件或 range/endurance。",
    "人员容量": "提取舰员、军官、航空人员、登陆部队、搭载人员等容量。",
    "动力推进": "提取主机、柴油机、燃气轮机、电推、锅炉、轴系、螺旋桨、推进功率等信息。",
    "导弹垂发": "提取导弹、垂直发射系统、VLS 单元、SAM/SLCM/ASROC/LMVLS/CPS 等信息。",
    "火炮反潜": "提取火炮、近防炮、机枪、鱼雷、反潜武器、反潜声呐或诱饵等信息。",
    "传感指控": "提取雷达、声呐、电子战、通信、作战管理、火控、指挥控制或数据链系统等信息。",
    "项目改装": "提取项目背景、采购计划、合同、批次、升级改装、现代化、未来计划或作战角色变化。",
}


class QuietDirectoryHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def load_env() -> None:
    load_dotenv(ROOT / ".env", override=False)
    if not (ROOT / ".env").exists():
        load_dotenv(ROOT / ".env.example", override=False)


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
    parser.add_argument("--output-dir", default="", help="输出目录；默认写入 .runtime/weaponry_directory_<timestamp>。")
    parser.add_argument("--output-prefix", default=DEFAULT_OUTPUT_PREFIX, help="输出文件名前缀。")
    parser.add_argument("--architecture-base", type=int, default=0, help="临时 architectureId 起始值；默认按时间生成。")
    parser.add_argument("--template-classify-id", type=int, default=1772442376645740)
    parser.add_argument("--static-host", default="127.0.0.1", help="自动静态文件服务监听地址。")
    parser.add_argument("--static-port", type=int, default=0, help="自动静态文件服务端口；0 表示自动分配。")
    parser.add_argument("--static-base", default="", help="已存在的静态文件服务 URL；提供后不再自动启动文件服务。")
    parser.add_argument("--analysis-timeout", type=int, default=1800)
    parser.add_argument("--weaponry-timeout", type=int, default=5400)
    parser.add_argument("--poll-interval", type=float, default=5.0)
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
    with sqlite3.connect(KB_DB) as conn:
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
    response = requests.request(f"{method}", f"{base_url.rstrip('/')}{path}", json=payload, timeout=timeout)
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {response.status_code}: {body}")
    return body


def get_task_payload(business_type: str, business_key: str) -> dict[str, Any] | None:
    with sqlite3.connect(TASK_DB) as conn:
        row = conn.execute(
            "select result_payload from llm_tasks where business_type=? and business_key=?",
            (business_type, str(business_key)),
        ).fetchone()
    if not row or not row[0]:
        return None
    return json.loads(row[0])


def wait_task(
    base_url: str,
    business_type: str,
    key_name: str,
    key_value: str | int,
    *,
    timeout_seconds: int,
    poll_interval: float,
    label: str,
) -> dict[str, Any]:
    start = time.time()
    last_progress: float | None = None
    while True:
        payload = {"businessType": business_type, "params": [{key_name: key_value}]}
        body = request_json("POST", base_url, "/llm/check-task", payload, timeout=30)
        data = body.get("data") or {}
        status = str(data.get("status", ""))
        progress = data.get("progress")
        if progress != last_progress:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {label} status={status} progress={progress}", flush=True)
            last_progress = progress
        if status in {"2", "3"}:
            return body
        if time.time() - start > timeout_seconds:
            raise TimeoutError(f"{label} timed out after {timeout_seconds}s: {body}")
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
                "enableFullTranslation": False,
                "country": [{"key": "02", "value": "美国"}],
                "channel": [{"key": "03", "value": "军情"}],
                "format": [{"key": "03", "value": "文档类"}],
                "maturity": [{"key": "03", "value": "定型成果"}],
                "architectureList": [node],
                "architectureStandardList": [],
            }
        ],
    }


def build_weaponry_payload(architecture_id: int, target_file_name: str, template_classify_id: int) -> dict[str, Any]:
    suffix = (
        f"本次抽取的唯一目标 PDF 文件名是 {target_file_name}。"
        "仅依据该目标 PDF 的明确装备事实抽取；terms/ 术语文件、term_rule_ 开头文件只用于理解字段定义和抽取口径，"
        "不是目标装备资料，不得把术语规则、字段定义或示例作为 analyseData。"
        f"如果检索片段的来源文件不是 {target_file_name}，只能把它当作术语参考，不能从中抽取取值。"
        "如果当前上下文只来自术语文件或不含目标 PDF 的装备事实，请只回答\"未找到\"；"
        "找不到目标 PDF 明确依据时也请只回答\"未找到\"，不要猜测。"
    )
    fields = []
    for name in FIELD_NAMES:
        fields.append(
            {
                "templateClassifyId": template_classify_id,
                "fieldName": name,
                "fieldType": "INPUT",
                "fieldDescription": f"{FIELD_DESCRIPTIONS[name]} {suffix}",
                "analyseData": "",
                "analyseDataSource": [],
            }
        )
    return {
        "businessType": "weaponry",
        "params": {
            "architectureId": architecture_id,
            "weaponryTemplateFieldList": fields,
        },
    }


def kb_records_for(architecture_id: int) -> list[dict[str, Any]]:
    with sqlite3.connect(KB_DB) as conn:
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

    headers = ["文件名", *FIELD_NAMES]
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
        "- weaponry 模式：建议后端以 `WEAPONRY_ANALYSE_MODE=2` 启动。",
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
    client = AnythingLLMClient(config)
    response = client.session.get(f"{config.base_url}/workspaces", headers=client._json_headers(1), timeout=config.timeout)
    if not response.ok:
        raise RuntimeError(f"AnythingLLM 不可用或 API Key 无效: {response.status_code} {response.text}")


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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] POST /llm/analysis {file_name}", flush=True)
    analysis_submit = request_json("POST", base_url, "/llm/analysis", analysis_payload, timeout=60)
    (file_out_dir / "analysis_submit_response.json").write_text(
        json.dumps(analysis_submit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    analysis_state = wait_task(
        base_url,
        "file",
        "fileName",
        file_name,
        timeout_seconds=analysis_timeout,
        poll_interval=poll_interval,
        label=f"analysis {file_name}",
    )
    (file_out_dir / "analysis_check_task_response.json").write_text(
        json.dumps(analysis_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    analysis_result = get_task_payload("file", file_name)
    (file_out_dir / "analysis_task_result.json").write_text(
        json.dumps(analysis_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if str(analysis_state.get("data", {}).get("status")) != "2":
        raise RuntimeError(f"analysis failed for {file_name}: {analysis_state}")

    kb_records = kb_records_for(architecture_id)
    (file_out_dir / "knowledge_base_records.json").write_text(
        json.dumps(kb_records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    related_docs = [record for record in kb_records if record.get("file_name")]
    if len(related_docs) != 1 or related_docs[0].get("file_name") != file_name:
        raise RuntimeError(f"architectureId={architecture_id} isolation check failed: {kb_records}")
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] KB isolation OK workspace={related_docs[0].get('workspace_slug')}",
        flush=True,
    )

    weaponry_payload = build_weaponry_payload(architecture_id, file_name, template_classify_id)
    (file_out_dir / "weaponry_request.json").write_text(
        json.dumps(weaponry_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[{datetime.now().strftime('%H:%M:%S')}] POST /llm/weaponry arch={architecture_id}", flush=True)
    weaponry_submit = request_json("POST", base_url, "/llm/weaponry", weaponry_payload, timeout=60)
    (file_out_dir / "weaponry_submit_response.json").write_text(
        json.dumps(weaponry_submit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    weaponry_state = wait_task(
        base_url,
        "weaponry",
        "architectureId",
        architecture_id,
        timeout_seconds=weaponry_timeout,
        poll_interval=poll_interval,
        label=f"weaponry {file_name}",
    )
    (file_out_dir / "weaponry_check_task_response.json").write_text(
        json.dumps(weaponry_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    weaponry_result = get_task_payload("weaponry", str(architecture_id))
    (file_out_dir / "weaponry_task_result.json").write_text(
        json.dumps(weaponry_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if str(weaponry_state.get("data", {}).get("status")) != "2":
        raise RuntimeError(f"weaponry failed for {file_name}: {weaponry_state}")

    fields = extract_fields(weaponry_result)
    fields_by_name = {field.get("fieldName"): field for field in fields if isinstance(field, dict)}
    row: dict[str, Any] = {"文件名": file_name}
    audit_rows: list[dict[str, Any]] = []
    non_empty = 0
    term_source_fields: list[str] = []
    for field_name in FIELD_NAMES:
        field = fields_by_name.get(field_name, {})
        value = str(field.get("analyseData") or "").strip()
        if value:
            non_empty += 1
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

    file_manifest = {
        "fileName": file_name,
        "sourcePath": str(file_path),
        "fileUrl": file_url,
        "architectureId": architecture_id,
        "analysis_state": analysis_state.get("data"),
        "weaponry_state": weaponry_state.get("data"),
        "field_count": len(FIELD_NAMES),
        "non_empty_count": non_empty,
        "missing_fields": [name for name in FIELD_NAMES if row[name] == "未找到明确依据"],
        "term_source_fields": term_source_fields,
        "workspace_records": kb_records,
    }
    return row, audit_rows, file_manifest


def main() -> int:
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
    out_dir = Path(args.output_dir).expanduser() if args.output_dir else ROOT / ".runtime" / f"weaponry_directory_{run_id}"
    if not out_dir.is_absolute():
        out_dir = (ROOT / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=False)

    base_url = args.base_url.strip() or default_base_url()
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "base_url": base_url,
        "file_dir": str(file_dir),
        "patterns": patterns,
        "recursive": args.recursive,
        "output_dir": str(out_dir),
        "output_prefix": args.output_prefix,
        "architecture_base": architecture_base,
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
        print(f"DRY_RUN OUT_DIR={out_dir} files={len(files)}", flush=True)
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
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] START {index}/{len(files)} "
                f"{file_path.name} arch={architecture_id}",
                flush=True,
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
            )
            rows.append(row)
            audit_rows.extend(file_audit_rows)
            manifest["files"][index - 1].update(file_manifest)
            manifest["completed_rows"] = len(rows)
            manifest["updated_at"] = datetime.now().isoformat()
            write_outputs(out_dir, args.output_prefix, rows, audit_rows, manifest)
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] DONE {file_path.name} "
                f"non_empty={file_manifest['non_empty_count']}/{len(FIELD_NAMES)} "
                f"term_source_fields={file_manifest['term_source_fields']}",
                flush=True,
            )
    finally:
        if static_server is not None:
            static_server.shutdown()
            static_server.server_close()

    manifest["finished_at"] = datetime.now().isoformat()
    manifest["completed_rows"] = len(rows)
    write_outputs(out_dir, args.output_prefix, rows, audit_rows, manifest)
    print(f"DONE OUT_DIR={out_dir} completed_rows={len(rows)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
