"""Legacy Microsoft Office preprocessing domain types.

This module deliberately has no dependency on Flask, AnythingLLM, or the legacy
``app.services`` package.  The conversion slice can therefore be configured and
tested as an ordinary local boundary.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable


LEGACY_OFFICE_SAFE_ERROR_MESSAGE = "Legacy Office 文件本地转换失败"
logger = logging.getLogger(__name__)


class LegacyOfficeConversionError(RuntimeError):
    """Fail-closed conversion error with a stable, non-sensitive public text."""

    def __init__(
        self,
        code: str,
        *,
        diagnostic: str = "",
        safe_message: str = LEGACY_OFFICE_SAFE_ERROR_MESSAGE,
    ) -> None:
        super().__init__(safe_message)
        self.code = str(code)
        self.safe_message = str(safe_message)
        self.diagnostic = str(diagnostic)

    def __str__(self) -> str:
        return self.safe_message


@dataclass(frozen=True, slots=True)
class LegacyOfficeConfig:
    """Immutable configuration for the local LibreOffice boundary."""

    enabled: bool = False
    executable: str | None = None
    allowed_version_series: str = "26.2"
    timeout_seconds: float = 120.0
    max_concurrency: int = 1
    max_input_bytes: int = 512 * 1024 * 1024
    max_output_bytes: int = 1024 * 1024 * 1024
    jobs_root: Path = Path(".runtime/office_conversion/jobs")

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled 必须是 bool")

        executable = None
        if self.executable is not None:
            executable = str(self.executable).strip() or None
        object.__setattr__(self, "executable", executable)

        series_parts = str(self.allowed_version_series).strip().split(".")
        if (
            len(series_parts) < 2
            or any(not part.isdigit() for part in series_parts)
        ):
            raise ValueError("allowed_version_series 必须是数字版本系列，例如 26.2")
        object.__setattr__(
            self,
            "allowed_version_series",
            ".".join(str(int(part)) for part in series_parts),
        )

        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds 必须大于 0")
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency <= 0
        ):
            raise ValueError("max_concurrency 必须是大于 0 的整数")
        for name in ("max_input_bytes", "max_output_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是大于 0 的整数")

        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        object.__setattr__(self, "jobs_root", Path(self.jobs_root))

    @classmethod
    def disabled(
        cls,
        *,
        jobs_root: str | Path = Path(".runtime/office_conversion/jobs"),
    ) -> "LegacyOfficeConfig":
        """Return a safe default that never probes the host installation."""

        return cls(enabled=False, jobs_root=Path(jobs_root))


class _CleanupLease:
    """Mutable, idempotent cleanup state held by an immutable result."""

    def __init__(self, callback: Callable[[], None] | None = None) -> None:
        self._callback = callback
        self._closed = False
        self._lock = Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            callback = self._callback
            self._callback = None
        if callback is not None:
            try:
                callback()
            except Exception:
                # Cleanup is compensating maintenance.  It must never turn a
                # successfully published business result into a task failure,
                # and callback exceptions may contain private host paths.
                logger.warning(
                    "Legacy Office cleanup callback failed; startup sweep will retry"
                )


@dataclass(frozen=True, slots=True)
class LegacyOfficePreparationResult:
    """Prepared document identity and lifetime.

    The dataclass is immutable.  Its private lease only tracks whether cleanup
    has already run, which makes ``close`` and nested/finally cleanup idempotent.
    """

    original_path: Path
    prepared_path: Path
    source_suffix: str
    target_suffix: str
    libreoffice_version: str | None
    converted: bool
    _lease: _CleanupLease = field(
        default_factory=_CleanupLease,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "original_path", Path(self.original_path))
        object.__setattr__(self, "prepared_path", Path(self.prepared_path))
        object.__setattr__(self, "source_suffix", self.source_suffix.lower())
        object.__setattr__(self, "target_suffix", self.target_suffix.lower())

    def __enter__(self) -> "LegacyOfficePreparationResult":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        """Best-effort, idempotent cleanup of task-private artifacts."""

        self._lease.close()


__all__ = [
    "LEGACY_OFFICE_SAFE_ERROR_MESSAGE",
    "LegacyOfficeConfig",
    "LegacyOfficeConversionError",
    "LegacyOfficePreparationResult",
]
