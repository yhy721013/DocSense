"""Ports exposed by the Legacy Office preprocessing slice."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .domain import LegacyOfficePreparationResult


@runtime_checkable
class LegacyOfficePreparer(Protocol):
    """Application-facing local document preparation port."""

    def preflight(self) -> str | None:
        """Validate the configured runtime and return its stable version."""

    def sweep_stale_jobs(self) -> int:
        """Best-effort cleanup of owned job directories from an earlier run."""

    def prepare(
        self,
        source_path: str | Path,
        *,
        job_id: str,
    ) -> LegacyOfficePreparationResult:
        """Prepare one path and return a context-managed result."""


__all__ = ["LegacyOfficePreparer"]
