#!/usr/bin/env python3
"""Run real LibreOffice smoke conversions for the macOS offline bundle."""

from __future__ import annotations

import argparse
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path


OLE2_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
logger = logging.getLogger(__name__)
UNSTABLE_VERSION_PATTERN = re.compile(
    r"(?:^|[^A-Za-z])"
    r"(?:alpha|beta|rc|nightly|development)"
    r"(?:[0-9._-]*)"
    r"(?:[^A-Za-z]|$)",
    re.IGNORECASE,
)
CASES = (
    (
        "word-sample.doc",
        "docx",
        "docx:Office Open XML Text",
        "word/document.xml",
    ),
    (
        "powerpoint-2002-apache-poi.ppt",
        "pptx",
        "pptx:Impress MS PowerPoint 2007 XML",
        "ppt/presentation.xml",
    ),
    (
        "excel-sample.xls",
        "xlsx",
        "xlsx:Calc Office Open XML",
        "xl/workbook.xml",
    ),
)


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float = 5.0,
) -> None:
    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass

    # 不能只等待组长进程：组长可能先退出，而同一进程组内的 soffice.bin
    # 仍然存活。宽限期内同时观察整个 PGID，之后对仍存在的进程组发 SIGKILL。
    deadline = time.monotonic() + max(grace_seconds, 0.0)
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group_id):
            break
        try:
            process.wait(timeout=min(0.1, max(deadline - time.monotonic(), 0.0)))
        except subprocess.TimeoutExpired:
            pass
        if _process_group_exists(process_group_id):
            time.sleep(min(0.05, max(deadline - time.monotonic(), 0.0)))

    if _process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("LibreOffice 进程组终止后组长进程仍未退出") from exc

    kill_deadline = time.monotonic() + 5.0
    while (
        _process_group_exists(process_group_id)
        and time.monotonic() < kill_deadline
    ):
        time.sleep(0.05)
    if _process_group_exists(process_group_id):
        raise RuntimeError("LibreOffice 进程组在 SIGKILL 后仍有残留")


def _run(
    command: list[str],
    *,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        raise RuntimeError("LibreOffice smoke 转换超时，已终止进程组") from exc
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _find_profile_processes(profile_uri: str) -> list[int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    matches: list[int] = []
    for line in result.stdout.splitlines():
        if profile_uri not in line:
            continue
        pid_text, _, _ = line.strip().partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid != os.getpid():
            matches.append(pid)
    return matches


def _terminate_residual_processes(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    time.sleep(1)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue


def _validate_version_output(version_output: str, expected_prefix: str) -> str:
    if re.search(r"LibreOfficeDev", version_output, re.IGNORECASE):
        raise RuntimeError("拒绝 LibreOfficeDev 开发版")
    if UNSTABLE_VERSION_PATTERN.search(version_output):
        raise RuntimeError("拒绝 LibreOffice 预发布或开发版本")
    pattern = rf"\bLibreOffice\s+{re.escape(expected_prefix)}(?:\.\d+)*\b"
    if re.search(pattern, version_output) is None:
        raise RuntimeError(
            f"版本不符合锁定的稳定 {expected_prefix}.x：{version_output[:160]}"
        )
    return version_output


def _verify_version(executable: Path, expected_prefix: str) -> str:
    result = _run([str(executable), "--version"], timeout_seconds=20)
    version_output = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        raise RuntimeError("LibreOffice --version 执行失败")
    return _validate_version_output(version_output, expected_prefix)


def _verify_ole2(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= len(OLE2_MAGIC):
        raise RuntimeError(f"样本不存在或为空：{path.name}")
    with path.open("rb") as handle:
        if handle.read(len(OLE2_MAGIC)) != OLE2_MAGIC:
            raise RuntimeError(f"样本不是 Office OLE2 文件：{path.name}")


def _verify_ooxml(path: Path, required_member: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"未生成有效 OOXML：{path.name}")
    try:
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise RuntimeError(f"OOXML ZIP 成员损坏：{bad_member}")
            if required_member not in archive.namelist():
                raise RuntimeError(f"OOXML 缺少结构成员：{required_member}")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"输出不是完整 OOXML ZIP：{path.name}") from exc


def run_smoke(
    *,
    executable: Path,
    samples_dir: Path,
    expected_version_prefix: str,
    timeout_seconds: int,
) -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("真实 smoke 只允许在 macOS Apple Silicon 运行")
    if not executable.is_absolute() or not executable.is_file():
        raise RuntimeError("--executable 必须是现有绝对文件路径")
    if timeout_seconds <= 0:
        raise RuntimeError("--timeout-seconds 必须为正整数")

    version = _verify_version(executable, expected_version_prefix)
    logger.info("版本门禁通过：%s", version)
    with tempfile.TemporaryDirectory(prefix="docsense-lo-smoke-") as temporary:
        temporary_root = Path(temporary)
        for index, (filename, target_suffix, filter_name, required_member) in enumerate(
            CASES,
            start=1,
        ):
            source = samples_dir / filename
            _verify_ole2(source)
            case_root = temporary_root / f"case-{index}"
            input_dir = case_root / "input"
            output_dir = case_root / "output"
            profile_dir = case_root / "profile"
            input_dir.mkdir(parents=True)
            output_dir.mkdir()
            profile_dir.mkdir()
            fixed_input = input_dir / f"input{source.suffix.lower()}"
            shutil.copyfile(source, fixed_input)
            profile_uri = profile_dir.resolve().as_uri()
            command = [
                str(executable),
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--norestore",
                f"-env:UserInstallation={profile_uri}",
                "--convert-to",
                filter_name,
                "--outdir",
                str(output_dir),
                str(fixed_input),
            ]
            result = _run(command, timeout_seconds=timeout_seconds)
            if result.returncode != 0:
                raise RuntimeError(
                    f"{filename} 转换失败，退出码 {result.returncode}"
                )
            outputs = list(output_dir.iterdir())
            expected_output = output_dir / f"input.{target_suffix}"
            if outputs != [expected_output]:
                names = ", ".join(sorted(path.name for path in outputs)) or "无"
                raise RuntimeError(f"{filename} 产物不唯一或名称不符：{names}")
            _verify_ooxml(expected_output, required_member)
            residual_pids = _find_profile_processes(profile_uri)
            if residual_pids:
                _terminate_residual_processes(residual_pids)
                raise RuntimeError(
                    f"{filename} 转换后发现残留 LibreOffice 进程，已清理：{residual_pids}"
                )
            logger.info(
                "转换结构验证通过：%s -> %s",
                filename,
                expected_output.name,
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--samples-dir", type=Path, required=True)
    parser.add_argument("--expected-version-prefix", default="26.2.5")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args(argv)
    try:
        run_smoke(
            executable=args.executable.resolve(),
            samples_dir=args.samples_dir.resolve(),
            expected_version_prefix=args.expected_version_prefix,
            timeout_seconds=args.timeout_seconds,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
