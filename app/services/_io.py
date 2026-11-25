"""Shared pure-Python file, JSON, and path helpers for frontend services."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_JSON_BYTES = 8 * 1024 * 1024


def read_bounded_bytes(path: str | Path, max_bytes: int, *, label: str) -> bytes:
    """Read up to ``max_bytes`` from a regular file, rejecting larger or special files."""
    target = Path(path)
    limit_mib = max_bytes // (1024 * 1024)
    try:
        info = target.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{label} is not a regular file: {target}")
        if info.st_size > max_bytes:
            raise ValueError(
                f"{label} exceeds the {limit_mib} MiB size limit: {target}"
            )
        with target.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except FileNotFoundError as exc:
        raise ValueError(f"{label} not found: {target}") from exc
    except OSError as exc:
        raise ValueError(f"Cannot read {label} {target}: {exc}") from exc
    if len(raw) > max_bytes:
        raise ValueError(f"{label} exceeds the {limit_mib} MiB size limit: {target}")
    return raw


def dump_json_strict(
    value: Any,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
    ensure_ascii: bool = True,
    default: Callable[[Any], Any] | None = None,
) -> str:
    """Serialize to strict JSON (rejecting NaN/Infinity and invalid types)."""
    try:
        return json.dumps(
            value,
            indent=indent,
            sort_keys=sort_keys,
            separators=separators,
            ensure_ascii=ensure_ascii,
            allow_nan=False,
            default=default,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"Value must contain valid finite JSON values: {exc}") from exc


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    overwrite: bool = False,
    encoding: str = "utf-8",
    newline: str = "\n",
    fsync: bool = True,
) -> Path:
    """Write ``text`` atomically via a same-directory temporary file."""
    target = Path(path).absolute()
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            newline=newline,
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            if fsync:
                os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def resolve_local_path(
    raw: str | Path, base: str | Path, *, resolve: bool = False
) -> Path:
    """Expand ``~`` and anchor relative paths to ``base``."""
    path = Path(raw).expanduser()
    combined = path if path.is_absolute() else Path(base) / path
    return combined.resolve() if resolve else combined.absolute()
