"""Config document I/O without pipeline imports or backend schema validation.

These helpers check syntax and mapping roots only. Use the backend's validation
command separately before running a pipeline. Pass a resolved document to
``save_copy``: saving never rebases its paths to the destination directory.
"""

from __future__ import annotations

import hashlib
import json
import os  # Kept at module scope for test monkeypatching of os.replace.
from datetime import date, datetime
from pathlib import Path

import yaml

from app.services._io import atomic_write_text, dump_json_strict, read_bounded_bytes
from bcflow.config_loader import _resolve_paths

_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_FORMATS = {".yaml", ".yml", ".json"}


def list_example_configs(root: str | Path = "configs") -> list[str]:
    """List shipped YAML/JSON configuration files under ``root``."""
    return sorted(str(p) for p in Path(root).glob("*") if p.suffix.lower() in _FORMATS)


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
    raw = read_bounded_bytes(source, _MAX_DOCUMENT_BYTES, label="Config document")
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


def _json_date(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Unsupported config value: {type(value).__name__}")


def normalize_config_for_json(resolved: dict) -> tuple[dict, str]:
    """Normalize YAML dates to ISO text and return ``(normalized, digest)``."""
    if not isinstance(resolved, dict):
        raise ValueError("Config must be a mapping (dict).")
    canonical = dump_json_strict(
        resolved, sort_keys=True, separators=(",", ":"), default=_json_date
    )
    return json.loads(canonical), hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def config_digest(config: dict) -> str:
    """Hash canonical, sorted, compact JSON; reject non-finite numbers."""
    return normalize_config_for_json(config)[1]


def save_copy(
    config: dict,
    path: str | Path,
    *,
    overwrite: bool = False,
    source_path: str | Path | None = None,
    create_parents: bool = False,
) -> Path:
    """Atomically save an already-resolved mapping, never overwriting by default.

    JSON normalization keeps YAML and JSON copies equivalent. When
    ``source_path`` is provided, saving back onto that file is refused. When
    ``create_parents`` is false, the target's parent must already exist.
    """
    if not isinstance(config, dict):
        raise ValueError("Config must be a mapping (dict).")
    target = Path(path).expanduser().absolute()
    if source_path is not None and target.resolve() == Path(source_path).resolve():
        raise ValueError(
            "Choose a different destination; the source config is protected."
        )
    suffix = _format(target.suffix)
    if suffix == ".json":
        text = (
            dump_json_strict(config, indent=2, sort_keys=True, default=_json_date)
            + "\n"
        )
    else:
        normalized, _ = normalize_config_for_json(config)
        text = yaml.safe_dump(normalized, sort_keys=True, allow_unicode=True)
    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    return atomic_write_text(target, text, overwrite=overwrite)
