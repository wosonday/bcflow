"""Bounded readers for declared run artifacts; never open scientific datasets."""

from __future__ import annotations

import heapq
import json
import os
import stat
from pathlib import Path, PureWindowsPath

import pandas as pd

_MAX_JSON_BYTES = 8 * 1024 * 1024


def _read_json(path: Path):
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"JSON artifact is not a regular file: {path}")
        if info.st_size > _MAX_JSON_BYTES:
            raise ValueError(f"JSON artifact exceeds the 8 MiB size limit: {path}")
        with path.open("rb") as stream:
            raw = stream.read(_MAX_JSON_BYTES + 1)
        if len(raw) > _MAX_JSON_BYTES:
            raise ValueError(f"JSON artifact exceeds the 8 MiB size limit: {path}")
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"Cannot read JSON artifact {path}: {exc}") from exc


def load_manifest(path: str | Path) -> dict:
    """Load a manifest mapping with a required mapping of variable records.

    Artifact existence is checked only when read, so incomplete/failed runs can
    still be listed. No artifact path is trusted just because it is in JSON.
    """
    manifest = _read_json(Path(path))
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("variables"), dict
    ):
        raise ValueError("Manifest must be a mapping with a 'variables' mapping.")
    for key in ("created", "status"):
        if key in manifest and not isinstance(manifest[key], str):
            raise ValueError(f"Manifest {key!r} must be a string.")
    for name, record in manifest["variables"].items():
        if not isinstance(record, dict):
            raise ValueError(f"Manifest variable {name!r} must be a mapping.")
        if "status" in record and not isinstance(record["status"], str):
            raise ValueError(f"Manifest variable {name!r} status must be a string.")
    return manifest


def list_manifests(root: str | Path, limit: int = 30) -> list[dict]:
    """List newest runs at root/manifest.json or root/run_*/manifest.json only.

    Skip corrupt, missing, oversized or unreadable manifests and symlinked run
    directories. Scan one directory level, retaining only ``limit`` summaries.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer.")
    if limit == 0:
        return []
    base = Path(root).resolve()

    def candidates():
        yield base / "manifest.json"
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    if entry.name.startswith("run_"):
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                yield Path(entry.path) / "manifest.json"
                        except OSError:
                            continue
        except OSError:
            return

    def summaries():
        for path in candidates():
            try:
                if path.is_symlink():
                    continue
                manifest = load_manifest(path)
            except ValueError, OSError:
                continue
            yield {
                "path": str(path),
                "created": manifest.get("created", ""),
                "status": manifest.get("status", "unknown"),
                "variables": {
                    name: record.get("status", "unknown")
                    for name, record in manifest["variables"].items()
                },
            }

    return heapq.nlargest(
        limit, summaries(), key=lambda row: (row["created"], row["path"])
    )


def artifact_path(manifest_path: str | Path, relative_path: str) -> Path:
    """Resolve an existing regular artifact strictly inside the run directory.

    Reject traversal even when it would normalize back inside, Windows absolute
    paths on POSIX hosts, and symlinks whose resolved target escapes the run.
    """
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or "\x00" in relative_path
    ):
        raise ValueError("Artifact path must be a non-empty relative path.")
    windows = PureWindowsPath(relative_path)
    relative = Path(relative_path.replace("\\", "/"))
    if (
        relative.is_absolute()
        or windows.drive
        or windows.root
        or ".." in relative.parts
    ):
        raise ValueError(
            f"Artifact path must be relative without '..': {relative_path!r}"
        )
    try:
        base = Path(manifest_path).absolute().parent.resolve()
        resolved = (base / relative).resolve()
        if not resolved.is_relative_to(base):
            raise ValueError(
                f"Artifact path escapes the manifest directory: {relative_path!r}"
            )
        if not resolved.is_file():
            raise ValueError(f"Artifact is missing or not a file: {relative_path!r}")
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Cannot resolve artifact {relative_path!r}: {exc}") from exc
    return resolved


def read_qa(manifest_path: str | Path) -> list[dict]:
    """Read declared QA checks; absent/null QA is empty, broken declarations fail."""
    manifest = load_manifest(manifest_path)
    relative = manifest.get("qa")
    if relative is None:
        return []
    checks = _read_json(artifact_path(manifest_path, relative))
    if not isinstance(checks, list) or any(
        not isinstance(check, dict) for check in checks
    ):
        raise ValueError("QA artifact must be a list of mappings.")
    return checks


def read_evaluation(
    manifest_path: str | Path, variable: str, *, max_rows: int = 50000
) -> pd.DataFrame:
    """Read at most max_rows declared CSV rows, marking attrs['truncated'].

    Metrics require metric/raw/corrected and either station identifiers or grid
    lat/lon coordinates. Optional period/ideal/improved columns are preserved.
    Station identifiers stay strings (including leading zeroes and 'NA').
    """
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows <= 0:
        raise ValueError("max_rows must be a positive integer.")
    manifest = load_manifest(manifest_path)
    record = manifest["variables"].get(variable)
    if record is None:
        raise ValueError(f"Variable {variable!r} is not declared in the manifest.")
    relative = record.get("evaluation")
    if relative is None:
        raise ValueError(f"No evaluation artifact declared for variable {variable!r}.")
    path = artifact_path(manifest_path, relative)
    if path.suffix.lower() != ".csv":
        raise ValueError(f"Evaluation artifact must be a CSV file: {relative!r}")
    try:
        data = pd.read_csv(
            path,
            nrows=max_rows + 1,
            keep_default_na=False,
            dtype={"station": "string", "period": "string", "metric": "string"},
        )
    except (OSError, UnicodeError, ValueError, pd.errors.ParserError) as exc:
        raise ValueError(f"Cannot read evaluation CSV {path}: {exc}") from exc
    missing = {"metric", "raw", "corrected"} - set(data.columns)
    if missing:
        raise ValueError(
            f"Evaluation CSV is missing required columns: {', '.join(sorted(missing))}."
        )
    if "station" not in data and not {"lat", "lon"}.issubset(data.columns):
        raise ValueError(
            "Evaluation CSV requires 'station' or both 'lat' and 'lon' columns."
        )
    truncated = len(data) > max_rows
    data = data.iloc[:max_rows].copy()
    for column in ("raw", "corrected", "ideal", "lat", "lon"):
        if column in data:
            try:
                data[column] = pd.to_numeric(
                    data[column].replace(
                        {"": float("nan"), "NaN": float("nan"), "nan": float("nan")}
                    ),
                    errors="raise",
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Evaluation column {column!r} must be numeric: {exc}"
                ) from exc
    if "improved" in data:
        values = data["improved"].astype("string").str.lower()
        if not values.isin(["true", "false", ""]).all():
            raise ValueError(
                "Evaluation column 'improved' must contain true, false or empty values."
            )
        data["improved"] = values.map({"true": True, "false": False}).astype("boolean")
    data.attrs["truncated"] = truncated
    return data
