
from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def workspace_tempdir() -> Iterator[str]:
    root = Path(".runtime/test-temp")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True) as tmp:
        yield tmp
