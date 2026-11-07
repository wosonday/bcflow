"""QA primitives used throughout the bias-correction pipeline.

Core idea (ported from swatprep/qa.py): instead of scattered raise/
warnings.warn() calls, every processing step appends a QACheck to one shared
list passed through the pipeline. The final result carries that list, so a
frontend can render it as a pass/warn/fail table -- no warning "disappears"
into stderr, which warnings.warn() cannot guarantee behind a user interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["pass", "warn", "fail"]


@dataclass
class QACheck:
    """Check passed through the pipeline."""

    name: str
    status: Status
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def is_ok(self) -> bool:
        """True unless the status is 'fail'."""
        return self.status in ("pass", "warn")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "context": self.context,
        }


def failures(checks: list[QACheck]) -> list[QACheck]:
    """Only the checks with status 'fail'."""
    return [c for c in checks if c.status == "fail"]


def warnings_only(checks: list[QACheck]) -> list[QACheck]:
    """Only the checks with status 'warn'."""
    return [c for c in checks if c.status == "warn"]


def all_pass(checks: list[QACheck]) -> bool:
    """True if no check has status 'fail'."""
    return not failures(checks)
