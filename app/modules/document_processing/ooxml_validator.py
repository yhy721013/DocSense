"""可信 OOXML 校验器旧路径的兼容入口。"""

from __future__ import annotations

import sys

from .adapters.libreoffice.ooxml_validator import *  # noqa: F403
from .adapters.libreoffice.ooxml_validator import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
