"""Config document I/O without pipeline imports or backend schema validation.

These helpers check syntax and mapping roots only. Use the backend's validation
command separately before running a pipeline. Pass a resolved document to
``save_copy``: saving never rebases its paths to the destination directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import yaml

from bcflow.config_loader import _resolve_paths

_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_FORMATS = {".yaml", ".yml", ".json"}


def _format(format: str) -> str:
    suffix = format.lower()
    if suffix not in _FORMATS:
        raise ValueError(
            f"Unsupported config format {format!r}; use .yaml, .yml, or .json."
        )
    return suffix


def load_document(path: str | Path) -> dict:
    """Read a UTF-8 mapping document (at most 2 MiB), hashing its raw bytes."""
    source = Path(path).resolve()
    suffix = _format(source.suffix)
    try:
        if not source.is_file():
            raise ValueError(f"Config file not found: {source}")
        with source.open("rb") as stream:
            raw = stream.read(_MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"Cannot read config {source}: {exc}") from exc
    if len(raw) > _MAX_DOCUMENT_BYTES:
        raise ValueError(f"Config document exceeds the 2 MiB size limit: {source}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Config document is not valid UTF-8: {source}") from exc
    resolve_document(text, suffix, source.parent)
    return {
        "path": str(source),
        "text": text,
        "format": suffix,
        "base_dir": str(source.parent),
        "digest": hashlib.sha256(raw).hexdigest(),
    }


def resolve_document(text: str, format: str, base_dir: str | Path) -> dict:
    """Parse a mapping and resolve paths exactly as the backend loader does."""
    suffix = _format(format)
    try:
        raw = json.loads(text) if suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError, RecursionError) as exc:
        kind = "JSON" if suffix == ".json" else "YAML"
        raise ValueError(f"Config document is not valid {kind}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            "Config document must be a mapping (key: value) at the top level."
        )
    try:
        return _resolve_paths(raw, Path(base_dir).resolve())
    except (RecursionError, OSError) as exc:
        raise ValueError(f"Cannot resolve config paths: {exc}") from exc


def _canonical_json(config: dict) -> str:
    if not isinstance(config, dict):
        raise ValueError("Config must be a mapping (dict).")
    try:
        return json.dumps(
            config, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(
            f"Config must contain valid finite JSON values: {exc}"
        ) from exc


def config_digest(config: dict) -> str:
    """Hash canonical, sorted, compact JSON; reject non-finite numbers."""
    return hashlib.sha256(_canonical_json(config).encode("utf-8")).hexdigest()


def save_copy(config: dict, path: str | Path, *, overwrite: bool = False) -> Path:
    """Atomically save an already-resolved mapping, never overwriting by default.

    JSON normalization keeps YAML and JSON copies equivalent. The target's parent
    must exist. A same-directory temporary file is published with an exclusive
    hard link (no-clobber), or atomic replacement when explicitly requested.
    """
    target = Path(path).absolute()
    suffix = _format(target.suffix)
    normalized = json.loads(_canonical_json(config))
    text = (
        json.dumps(normalized, indent=2, allow_nan=False) + "\n"
        if suffix == ".json"
        else yaml.safe_dump(normalized, sort_keys=True, allow_unicode=True)
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target
