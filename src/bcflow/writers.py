"""Write bias-correction results to disk.

Uses an explicit registry (_WRITERS), same principle as METHOD_REGISTRY/
GRID_EXTRACTORS: adding a new format means adding one dict entry, not an
if/elif branch.

write_run() is the single entry point (for the CLI and, later, the
frontend). On-disk layout of one run:

    <output.path>/run_YYYYmmdd_HHMMSS/
        manifest.json    every variable: status ("ok"/"failed") + written paths
        qa.json          every QACheck of the whole run
        <var>/scen.nc    corrected result (+ scen.csv if requested)
        <var>/adj.nc     trained adjustment factors, always NetCDF, so they can
                         be re-applied to another sim without re-training
                         (xsdba allows calling adj.adjust() more than once)

qa.json is written once per run, not per variable: run_pipeline() collects
the checks of every variable into ONE shared list, so a per-variable copy
would repeat the other variables' checks in each folder.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import xarray as xr

from .config_schema import OutputConfig
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


def write_result(scen: xr.DataArray, adj, *, run_dir: Path, var_name: str, formats: list[str]) -> dict:
    """Write one variable's results under run_dir/<var_name>/. Returns the written paths."""
    var_dir = run_dir / var_name
    var_dir.mkdir(parents=True, exist_ok=True)

    scen_paths: dict[str, Path] = {}
    for fmt in formats:
        if fmt not in _WRITERS:
            raise ValueError(f"Unsupported format: {fmt!r}. Valid formats: {sorted(_WRITERS)}.")
        scen_paths[fmt] = _WRITERS[fmt](scen, var_dir / f"scen.{_EXTENSIONS[fmt]}", var_name)

    adj_path = _write_netcdf(adj.ds, var_dir / "adj.nc")
    return {"scen": scen_paths, "adj": adj_path}


def write_qa(run_dir: Path, qa: list[QACheck]) -> Path:
    """Write every QACheck of the run to run_dir/qa.json."""
    path = run_dir / "qa.json"
    path.write_text(json.dumps([c.as_dict() for c in qa], indent=2, default=str), encoding="utf-8")
    return path


def write_manifest(run_dir: Path, per_variable: dict[str, dict | None]) -> Path:
    """Write run_dir/manifest.json. A None entry means the variable failed,
    either in the pipeline or while writing; it is still listed, so a reader
    of the manifest can tell a failed variable from a missing one.

    Paths are stored relative to run_dir, in POSIX form ("tasmax/scen.nc"),
    so the run folder stays valid when zipped, shared or moved to another
    machine (absolute paths would point back to the original computer).
    """
    def rel(p: Path) -> str:
        return p.relative_to(run_dir).as_posix()

    manifest: dict[str, dict] = {}
    for name, written in per_variable.items():
        if written is None:
            manifest[name] = {"status": "failed"}
        else:
            manifest[name] = {
                "status": "ok",
                "scen": {fmt: rel(p) for fmt, p in written["scen"].items()},
                "adj": rel(written["adj"]),
            }
    path = run_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def _new_run_dir(base: Path) -> Path:
    """Create a fresh run_YYYYmmdd_HHMMSS folder; add a suffix (_2, _3, ...)
    if a run already used that second, so a previous run is never overwritten.
    """
    stamp = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    run_dir, n = base / stamp, 1
    while run_dir.exists():
        n += 1
        run_dir = base / f"{stamp}_{n}"
    run_dir.mkdir(parents=True)
    return run_dir


def write_run(out: dict, output_cfg: OutputConfig) -> Path:
    """Write a whole run (the dict returned by pipeline.run_pipeline()) to disk.

    A failure while writing one variable is isolated the same way
    run_pipeline() isolates computation failures: a 'fail' QACheck is
    APPENDED to out["qa"] (so the caller's report shows it), the variable is
    marked failed in the manifest, and the other variables are still written.

    Returns the path of manifest.json; its parent is the run directory.
    """
    run_dir = _new_run_dir(Path(output_cfg.path))

    per_variable: dict[str, dict | None] = {}
    for name, res in out["results"].items():
        if res is None:  # already failed in the pipeline; its QACheck is in out["qa"]
            per_variable[name] = None
            continue
        try:
            per_variable[name] = write_result(
                res["scen"], res["adj"], run_dir=run_dir, var_name=name, formats=output_cfg.formats
            )
        except Exception as exc:  # isolates write errors, per variable
            out["qa"].append(QACheck(
                name=f"writers:{name}:exception",
                status="fail",
                message=f"Writing variable '{name}' failed: {exc}",
            ))
            per_variable[name] = None

    write_qa(run_dir, out["qa"])  # after the loop, so write failures are included
    return write_manifest(run_dir, per_variable)
