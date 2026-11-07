"""The operations a user interface can perform, returning JSON-ready data.

This module is the backend's interface for frontends. Every function
returns dicts/lists of str/int/float/bool/None -- never xarray objects --
so the result can be printed as JSON as-is:

- cli.py's --json mode prints them for the GPUI (Rust) desktop app, which
  runs `bcflow <command> --json` as a subprocess and parses stdout;
- the human-readable CLI formats the same data as text;
- a future HTTP API would be one more thin adapter over these functions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import xarray as xr
from pydantic import ValidationError

from ._version import bcflow_version
from .config_loader import load_config_file
from .config_schema import PipelineConfig, load_config
from .loaders import load_station_metadata, station_file
from .methods import METHOD_REGISTRY
from .pipeline import EventHandler, run_pipeline
from .variables import VARIABLES
from .writers import write_run

__all__ = [
    "bcflow_version",
    "config_json_schema",
    "inspect_path",
    "list_methods",
    "run_config_file",
    "validate_config_file",
]


# --- methods / schema -------------------------------------------------------

def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def list_methods() -> list[dict]:
    """Correction methods a config may name in `method:`."""
    return [
        {"name": name, "class": cls.__name__, "summary": _first_line(cls.__doc__)}
        for name, cls in METHOD_REGISTRY.items()
    ]


def config_json_schema() -> dict:
    """JSON Schema of config.yaml, e.g. to build or check a form in the UI."""
    return PipelineConfig.model_json_schema()


# --- validate ---------------------------------------------------------------

def _variable_of(loc: list) -> str | None:
    """The variable an error belongs to, when its location names one."""
    return next((str(x) for x in loc if x in VARIABLES), None)


def _pydantic_errors(exc: ValidationError) -> list[dict]:
    out = []
    for e in exc.errors():
        loc = list(e["loc"])
        out.append({"variable": _variable_of(loc), "loc": loc, "message": e["msg"]})
    return out


def _semantic_errors(cfg: PipelineConfig) -> list[dict]:
    """Checks the schema alone cannot do: registered methods, existing inputs."""
    errors: list[dict] = []
    for v in cfg.variables:
        method = cfg.spec(v).method
        if method not in METHOD_REGISTRY:
            errors.append({
                "variable": v,
                "loc": ["correction", v, "method"],
                "message": f"Method '{method}' is not registered. Valid methods: {sorted(METHOD_REGISTRY)}.",
            })

    st = cfg.stations
    missing = [key for key in ("metadata", "data_dir") if not Path(getattr(st, key)).exists()]
    for key in missing:
        errors.append({"variable": None, "loc": ["stations", key], "message": f"Path not found: {getattr(st, key)}"})
    if not missing:
        try:
            meta = load_station_metadata(st)
        except (OSError, ValueError) as exc:
            errors.append({"variable": None, "loc": ["stations", "metadata"], "message": str(exc)})
        else:
            if not any(station_file(st, str(sid), str(row["name"])).is_file() for sid, row in meta.iterrows()):
                errors.append({
                    "variable": None,
                    "loc": ["stations", "file_pattern"],
                    "message": f"No station file matches '{st.file_pattern}' in {st.data_dir}.",
                })

    seen: set[str] = set()
    for v in cfg.variables:
        for which in ("hist", "sim"):
            path = cfg.model.path(which, v)
            if path in seen:
                continue
            seen.add(path)
            if not Path(path).exists():
                errors.append({"variable": v, "loc": ["model", which], "message": f"Path not found: {path}"})
    return errors


def validate_config_file(path: str | Path) -> dict:
    """Check a config without running it: schema, method names, input paths.

    Every problem is reported, not just the first, so a form can highlight
    all of them at once. Returns
        {"valid": bool, "variables": [name, ...],
         "errors": [{"variable": name | None, "loc": [...], "message": str}]}
    where `loc` is the path to the offending field, e.g. ["model", "hist"].
    """
    try:
        raw = load_config_file(path)
    except ValueError as exc:
        return {"valid": False, "variables": [], "errors": [{"variable": None, "loc": [], "message": str(exc)}]}

    listed = raw.get("variables")
    names = [str(v) for v in listed] if isinstance(listed, list) else []
    try:
        cfg = load_config(raw)
    except ValidationError as exc:
        return {"valid": False, "variables": names, "errors": _pydantic_errors(exc)}

    errors = _semantic_errors(cfg)
    return {"valid": not errors, "variables": [str(v) for v in cfg.variables], "errors": errors}


# --- inspect ----------------------------------------------------------------

_RASTER_SUFFIXES = {".nc", ".nc4", ".netcdf", ".zarr"}


def _is_raster(p: Path) -> bool:
    if p.suffix.lower() in _RASTER_SUFFIXES:
        return True
    return p.is_dir() and any((p / marker).exists() for marker in (".zgroup", "zarr.json"))


def _time_info(t: xr.DataArray) -> dict | None:
    if t.size == 0:
        return None
    dim = t.dims[0]
    return {
        "start": t.isel({dim: 0}).dt.strftime("%Y-%m-%d").item(),
        "end": t.isel({dim: -1}).dt.strftime("%Y-%m-%d").item(),
        "n": int(t.size),
        "calendar": getattr(t.dt, "calendar", "standard"),
    }


def _inspect_raster(p: Path) -> dict:
    is_zarr = p.is_dir()
    with (xr.open_zarr(p) if is_zarr else xr.open_dataset(p)) as ds:
        return {
            "kind": "raster",
            "path": str(p),
            "format": "zarr" if is_zarr else "netcdf",
            "variables": [
                {
                    "name": str(name),
                    "dims": [str(d) for d in var.dims],
                    "shape": [int(s) for s in var.shape],
                    "units": var.attrs.get("units"),
                    "long_name": var.attrs.get("long_name"),
                }
                for name, var in ds.data_vars.items()
            ],
            "coords": {str(n): {"dims": [str(d) for d in c.dims], "size": int(c.size)} for n, c in ds.coords.items()},
            "time": _time_info(ds["time"]) if "time" in ds.coords else None,
        }


def _inspect_table(p: Path) -> dict:
    df = pd.read_csv(p)
    info: dict[str, Any] = {"kind": "table", "path": str(p), "columns": [str(c) for c in df.columns], "n_rows": int(len(df)), "time": None}
    date_col = next((c for c in df.columns if str(c).strip().lower() in ("date", "time")), None)
    if date_col is not None:
        t = pd.to_datetime(df[date_col], errors="coerce").dropna()
        if len(t):
            info["time"] = {"start": t.min().strftime("%Y-%m-%d"), "end": t.max().strftime("%Y-%m-%d"), "n": int(len(t)), "calendar": "standard"}
    return info


def _inspect_station_folder(p: Path) -> dict:
    files = sorted(p.glob("*.csv"))
    return {
        "kind": "station_folder",
        "path": str(p),
        "n_files": len(files),
        "stations": [f.stem for f in files],
        "sample": _inspect_table(files[0]) if files else None,
    }


def inspect_path(path: str | Path) -> dict:
    """Describe a data file or folder so a user can fill in the config.

    Raster (.nc / .zarr): variables with dims, units; coords; time range.
    Table (.csv): columns, row count, time range of a 'date' (or 'time') column.
    Folder of .csv (one file per station): file names + one sample file.
    Raises FileNotFoundError / ValueError for a missing or unsupported path.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {p}")
    if _is_raster(p):
        return _inspect_raster(p)
    if p.is_dir():
        return _inspect_station_folder(p)
    if p.suffix.lower() == ".csv":
        return _inspect_table(p)
    raise ValueError(f"Unsupported file type: {p.name!r}. Expected .nc, .zarr, .csv or a folder of .csv files.")


# --- run --------------------------------------------------------------------

_STATUSES = ("pass", "warn", "fail")


def _summary(out: dict | None, manifest: Path | None, *, error: str | None = None) -> dict:
    qa = out["qa"] if out else []
    counts = {s: sum(1 for c in qa if c.status == s) for s in _STATUSES}
    if manifest is not None:  # the manifest is authoritative: it includes write failures
        variables = {n: v["status"] for n, v in json.loads(manifest.read_text(encoding="utf-8"))["variables"].items()}
    elif out:
        variables = {n: ("ok" if r is not None else "failed") for n, r in out["results"].items()}
    else:
        variables = {}

    if error is not None:
        exit_code, status = 2, "error"
    else:
        exit_code = 0 if counts["fail"] == 0 else 1
        status = "ok" if exit_code == 0 else "failed"

    return {
        "status": status,
        "exit_code": exit_code,
        "error": error,
        "run_dir": str(manifest.parent) if manifest else None,
        "manifest": str(manifest) if manifest else None,
        "variables": variables,
        "counts": counts,
        "qa": [c.as_dict() for c in qa],
    }


def run_config_file(path: str | Path, on_event: EventHandler | None = None) -> dict:
    """Run every variable of a config file and write the outputs.

    Returns {"status": "ok" | "failed" | "error", "exit_code": 0 | 1 | 2,
             "error": str | None, "run_dir", "manifest", "variables": {name: "ok" | "failed"},
             "counts": {"pass", "warn", "fail"}, "qa": [check, ...]}.
    exit_code 2 / status "error" means the run could not start (invalid
    config, or its output folder could not be created); 1 means it ran with failures.
    """
    emit = on_event or (lambda event: None)
    try:
        raw = load_config_file(path)
        # The whole config is validated before any variable is computed;
        # pydantic's ValidationError is a ValueError.
        cfg = load_config(raw)
        out = run_pipeline(cfg, on_event=emit)
    except ValueError as exc:
        return _summary(None, None, error=str(exc))

    n_before = len(out["qa"])
    try:
        manifest = write_run(out, cfg.output, config=raw)
    except OSError as exc:
        return _summary(out, None, error=f"Cannot create the output folder: {exc}")

    for check in out["qa"][n_before:]:  # failures raised while writing
        emit({"event": "qa", "variable": check.context.get("variable"), "check": check.as_dict()})
    return _summary(out, manifest)
