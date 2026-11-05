"""config_loader.py -- read config.yaml from disk, resolve relative paths.

Split from config_schema.py: schema.py only answers "is this dict shaped
like a valid PipelineConfig" (pure, no I/O); this module answers "find the
file, read it, fix up its paths, then hand off to the caller".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_PATH_KEYS = {"path", "metadata_path"}


def _resolve_paths(node: Any, base: Path) -> Any:
    """Walk a nested dict/list structure, turning every path-valued field
    into an absolute path, resolved against `base` (not the cwd).
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _PATH_KEYS and isinstance(value, str):
                out[key] = str((base / value).resolve())
            else:
                out[key] = _resolve_paths(value, base)
        return out
    if isinstance(node, list):
        return [_resolve_paths(item, base) for item in node]
    return node


def load_config_file(path: str | Path) -> dict:
    """Read config.yaml and resolve every relative path against the file's
    own directory (not the cwd).

    Deliberately does NOT call config_schema.load_config() here: that would
    validate the WHOLE PipelineConfig at once, and pydantic raises on the
    first invalid VariableConfig for the ENTIRE model -- which would undo
    pipeline.run_pipeline()'s per-variable error isolation (one variable's
    config mistake, e.g. a missing adapt_freq_thresh, would stop every other
    variable from running too). Full-document validation stays available
    separately via config_schema.load_config(), for contexts that want
    fail-fast behaviour (e.g. a future --validate CLI flag, or tests).
    """
    cfg_path = Path(path)
    if not cfg_path.is_file():
        raise ValueError(f"Config file not found: {cfg_path}")

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{cfg_path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{cfg_path} must be a mapping (key: value) at the top level.")

    return _resolve_paths(raw, cfg_path.parent.resolve())
