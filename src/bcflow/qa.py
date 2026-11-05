"""QA primitives dùng xuyên suốt pipeline bias-correction.

Ý tưởng cốt lõi (port từ thiết kế của swatprep/qa.py): thay vì raise/
warnings.warn() rải rác, mỗi bước xử lý append một QACheck vào một list
dùng chung, truyền xuyên suốt pipeline. Kết quả cuối cùng mang theo list
QACheck này để frontend render thành bảng pass/warn/fail — không có cảnh
báo nào "biến mất" vào stderr, điều mà warnings.warn() không đảm bảo được
trong một backend phục vụ web UI.
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
        """return `True` if not fail."""
        return self.status in ("pass", "warn")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "context": self.context,
        }


def failures(checks: list[QACheck]) -> list[QACheck]:
    """Chỉ trả về các check có status 'fail'."""
    return [c for c in checks if c.status == "fail"]


def warnings_only(checks: list[QACheck]) -> list[QACheck]:
    """Chỉ trả về các check có status 'warn'."""
    return [c for c in checks if c.status == "warn"]


def all_pass(checks: list[QACheck]) -> bool:
    """True nếu không có check nào 'fail'."""
    return not failures(checks)
