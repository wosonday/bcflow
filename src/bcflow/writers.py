"""Write bias-correction results to disk.

Uses an explicit registry (_WRITERS), same principle as METHOD_REGISTRY/
GRID_EXTRACTORS: adding a new format means adding one dict entry, not an
if/elif branch.

Writes BOTH of the following for each variable:
- scen   (the corrected result)        -> one or more formats, caller's choice
- adj.ds (the trained adjustment factors) -> always NetCDF, so it can be
  re-applied to a different sim later without re-training (xsdba allows
  calling adj.adjust() more than once on a trained object).
"""

from __future__ import annotations

import json
from pathlib import Path

import xarray as xr

from .qa import QACheck

_EXTENSIONS = {"netcdf": "nc", "csv": "csv"}


def _write_netcdf(obj: xr.DataArray | xr.Dataset, path: Path, *, var_name: str | None = None) -> Path:
    """NetCDF keeps attrs/units/multiple dimensions intact -- the "canonical" format.

    A DataArray with no name yet (the `scen` case) must be wrapped into a
    Dataset first, since NetCDF always needs a variable name to write.
    """
    data = obj.to_dataset(name=var_name) if isinstance(obj, xr.DataArray) else obj
    data.to_netcdf(path)
    return path


def _write_csv(da: xr.DataArray, path: Path) -> Path:
    """CSV is easy to open in Excel for non-coding team members -- only makes
    sense for station-point data (2 dimensions: time x station). Raster data
    (lat, lon) must use NetCDF instead.
    """
    if set(da.dims) != {"time", "station"}:
        raise ValueError(
            f"_write_csv only supports a 2-D DataArray (time, station); "
            f"got dims={da.dims!r}. Use format='netcdf' for raster data."
        )
    df = da.transpose("time", "station").to_pandas()
    df.to_csv(path, index_label="time")
    return path


_WRITERS = {
    "netcdf": lambda da, path, var_name: _write_netcdf(da, path, var_name=var_name),
    "csv": lambda da, path, var_name: _write_csv(da, path),
}


def write_result(
    scen: xr.DataArray,
    adj,
    *,
    run_dir: Path,
    var_name: str,
    formats: list[str],
    qa: list[QACheck],
) -> dict:
    """Write one variable's results under run_dir/<var_name>/. Returns the written paths."""
    var_dir = run_dir / var_name
    var_dir.mkdir(parents=True, exist_ok=True)

    scen_paths: dict[str, Path] = {}
    for fmt in formats:
        if fmt not in _WRITERS:
            raise ValueError(f"Unsupported format: {fmt!r}. Valid formats: {sorted(_WRITERS)}.")
        path = var_dir / f"scen.{_EXTENSIONS[fmt]}"
        scen_paths[fmt] = _WRITERS[fmt](scen, path, var_name)

    adj_path = _write_netcdf(adj.ds, var_dir / "adj.nc")

    qa_path = var_dir / "qa.json"
    qa_path.write_text(json.dumps([c.as_dict() for c in qa], indent=2, default=str), encoding="utf-8")

    return {"scen": scen_paths, "adj": adj_path, "qa": qa_path}


def write_manifest(run_dir: Path, per_variable: dict[str, dict]) -> Path:
    """Aggregate every variable's output into one manifest.json at run_dir's root."""
    manifest = {
        name: {
            "scen": {fmt: str(p) for fmt, p in result["scen"].items()},
            "adj": str(result["adj"]),
            "qa": str(result["qa"]),
        }
        for name, result in per_variable.items()
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path