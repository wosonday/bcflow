"""Shared pure-Python file, JSON, and path helpers for frontend services."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bcflow.results import MAX_JSON_BYTES, read_bounded_bytes, resolve_local_path


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
