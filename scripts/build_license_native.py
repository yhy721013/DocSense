from __future__ import annotations

import argparse
import os
import sys

if os.environ.get("SETUPTOOLS_USE_DISTUTILS") != "stdlib":
    os.environ["SETUPTOOLS_USE_DISTUTILS"] = "stdlib"
    os.execv(sys.executable, [sys.executable, *sys.argv])

import shutil
import subprocess
from distutils.core import Distribution, Extension
from pathlib import Path

from Cython.Build import cythonize
from Cython.Compiler import Options


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docsense_license.py"
BUILD_ROOT = ROOT / ".runtime" / "native-license-build"
OUTPUT_ROOT = ROOT / ".runtime" / "native-license"


def _path(value: Path | None, fallback: Path) -> Path:
    candidate = value or fallback
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--keep-build", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    build_root = _path(args.build_dir, BUILD_ROOT)
    output_root = _path(args.output_dir, OUTPUT_ROOT)
    Options.docstrings = False
    Options.emit_code_comments = False
    shutil.rmtree(build_root, ignore_errors=True)
    shutil.rmtree(output_root, ignore_errors=True)
    build_root.mkdir(parents=True)
    output_root.mkdir(parents=True)
    try:
        extensions = cythonize(
            [Extension("docsense_license", [str(SOURCE)])],
            build_dir=str(build_root / "cython"),
            compiler_directives={
                "binding": False,
                "language_level": 3,
            },
            annotate=False,
            quiet=True,
        )
        distribution = Distribution(
            {
                "name": "docsense-license-native",
                "ext_modules": extensions,
            }
        )
        command = distribution.get_command_obj("build_ext")
        command.build_lib = str(output_root)
        command.build_temp = str(build_root / "temp")
        distribution.run_command("build_ext")
        candidates = list(output_root.glob("docsense_license*.so"))
        candidates.extend(output_root.glob("docsense_license*.pyd"))
        if len(candidates) != 1:
            raise RuntimeError("未生成唯一的原生授权模块")
        target = candidates[0]
        if sys.platform == "darwin" and shutil.which("strip"):
            subprocess.run(["strip", "-x", str(target)], check=True)
        if sys.platform.startswith("linux") and shutil.which("strip"):
            subprocess.run(["strip", "--strip-unneeded", str(target)], check=True)
        sys.stdout.write(str(target) + "\n")
        return 0
    finally:
        if not args.keep_build:
            shutil.rmtree(build_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
