#!/usr/bin/env python3
"""Fetch the pinned Apache POI PowerPoint smoke sample outside Git."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from fetch_assets import (
    LOCK_PATH,
    _download_verified,
    configure_cli_logging,
    load_lock,
)


DEFAULT_SAMPLES_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "dist"
    / "legacy-office"
    / "samples"
)
logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    configure_cli_logging()
    parser = argparse.ArgumentParser(
        description="下载固定 Apache POI 提交中的 PPT smoke 样本并校验 SHA-256"
    )
    parser.add_argument(
        "--samples-dir",
        type=Path,
        default=DEFAULT_SAMPLES_DIR,
        help="Git 外样本目录；默认 dist/legacy-office/samples",
    )
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    args = parser.parse_args(argv)

    try:
        lock = load_lock(args.lock)
        name = "powerpoint-2002-apache-poi.ppt"
        sample = lock["smokeSamples"][name]
        _download_verified(
            url=sample["url"],
            destination=args.samples_dir.resolve() / name,
            expected_sha256=sample["sha256"],
        )
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
