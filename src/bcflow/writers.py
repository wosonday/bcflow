"""Write bias-correction results to disk.

Uses an explicit registry (_WRITERS), same principle as METHOD_REGISTRY/
GRID_EXTRACTORS: adding a new format means adding one dict entry, not an
if/elif branch.

write_run() is the single entry point. On-disk layout of one run:

    <output.path>/run_YYYYmmdd_HHMMSS/
        manifest.json        run metadata + every variable's status and files
        qa.json              every QACheck of the whole run
        config.json          the exact config used (paths already resolved)
        <var>/scen.nc        corrected result (+ scen.csv if requested)
        <var>/adj.nc         trained adjustment factors, always NetCDF, so they
                             can be re-applied to another sim without
                             re-training (xsdba allows adj.adjust() repeatedly)
        <var>/evaluation.csv before/after metrics per period and station (evaluate.py)
        <var>/hist_eval.nc   obs, raw hist and CORRECTED hist on the same dates,
                             over the calibration and validation periods, with a
                             'period' label per day (+ hist_eval.csv, long format:
                             time, station, period, obs, raw, corrected)

With a gridded reference, scen.nc and hist_eval.nc are (time, lat, lon) on the
reference grid (cells without data are NaN), written a year at a time so a
large grid never has to sit in memory as a full cube; their CSVs are long
format with lat/lon columns and only the corrected cells.

qa.json is written once per run, not per variable: run_pipeline() collects
the checks of every variable into ONE shared list, so a per-variable copy
would repeat the other variables' checks in each folder.

manifest.json (schema_version 1), all paths relative to the run folder:
    {"schema_version": 1, "bcflow_version": "0.1.0", "created": "<ISO 8601>",
     "qa": "qa.json", "config": "config.json" | null,
     "variables": {"<var>": {"status": "ok", "scen": {"netcdf": "<var>/scen.nc"},
                             "adj": "<var>/adj.nc", "evaluation": "<var>/evaluation.csv" | null,
                             "hist_eval": {"netcdf": "<var>/hist_eval.nc"} | null}
                   | {"status": "failed" | "cancelled" | "skipped"}},
     "status": "ok" | "failed" | "cancelled"}
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import netCDF4
import numpy as np
import pandas as pd
import xarray as xr
from xarray.coding.times import encode_cf_datetime

from ._version import bcflow_version
from .config_schema import OutputConfig
from .progress import RunControl
from .qa import QACheck
from .regrid import TargetGrid

MANIFEST_SCHEMA_VERSION = 1
_EXTENSIONS = {"netcdf": "nc", "csv": "csv"}


def _write_netcdf(
    obj: xr.DataArray | xr.Dataset, path: Path, *, var_name: str | None = None
) -> Path:
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


def _write_hist_eval(
    ds: xr.Dataset, var_dir: Path, formats: list[str]
) -> dict[str, Path]:
    """obs / raw / corrected hist, in the same formats as scen."""
    paths: dict[str, Path] = {}
    if "netcdf" in formats:
        paths["netcdf"] = _write_netcdf(ds, var_dir / "hist_eval.nc")
    if "csv" in formats:
        path = var_dir / "hist_eval.csv"
        df = ds.to_dataframe().reset_index()
        df[["time", "station", "period", "obs", "raw", "corrected"]].to_csv(
            path, index=False
        )
        paths["csv"] = path
    return paths


_CHUNK_DAYS = 365  # time steps written at once for gridded output


def _write_grid_netcdf(
    variables: dict[str, xr.DataArray], grid: TargetGrid, path: Path
) -> Path:
    """(time, station) arrays of grid cells -> one (time, lat, lon) NetCDF file."""
    first = next(iter(variables.values()))
    ids = grid.ids
    num, units, calendar = encode_cf_datetime(
        first["time"].values, "days since 1850-01-01", first["time"].dt.calendar
    )
    nt, nlat, nlon = first.sizes["time"], grid.lat.size, grid.lon.size
    with netCDF4.Dataset(path, "w") as nc:
        nc.createDimension("time", None)
        nc.createDimension("lat", nlat)
        nc.createDimension("lon", nlon)
        t = nc.createVariable("time", "f8", ("time",))
        t.setncatts({"units": units, "calendar": calendar, "standard_name": "time"})
        t[:] = num
        for name, axis, unit in (
            ("lat", grid.lat, "degrees_north"),
            ("lon", grid.lon, "degrees_east"),
        ):
            v = nc.createVariable(name, "f8", (name,))
            v.setncatts(
                {
                    "units": unit,
                    "standard_name": "latitude" if name == "lat" else "longitude",
                }
            )
            v[:] = axis
        if "period" in first.coords:
            p = nc.createVariable("period", str, ("time",))
            p[:] = np.asarray(first["period"].values, dtype=object)
        for name, da in variables.items():
            values = da.sel(station=ids).transpose("time", "station").values
            v = nc.createVariable(
                name,
                "f4",
                ("time", "lat", "lon"),
                zlib=True,
                complevel=4,
                fill_value=np.float32(np.nan),
                chunksizes=(min(_CHUNK_DAYS, max(nt, 1)), nlat, nlon),
            )
            if da.attrs.get("units"):
                v.units = da.attrs["units"]
            for a in range(0, nt, _CHUNK_DAYS):
                b = min(a + _CHUNK_DAYS, nt)
                cube = np.full((b - a, nlat, nlon), np.nan, dtype=np.float32)
                cube[:, grid.iy, grid.ix] = values[a:b]
                v[a:b] = cube
    return path


def _write_grid_csv(
    variables: dict[str, xr.DataArray], grid: TargetGrid, path: Path
) -> Path:
    """Long CSV: time, lat, lon[, period], one column per variable; corrected cells only."""
    first = next(iter(variables.values()))
    ids = grid.ids
    columns = {
        n: da.sel(station=ids).transpose("time", "station").values
        for n, da in variables.items()
    }
    times = pd.Index(first["time"].values).astype(str)
    period = first["period"].values if "period" in first.coords else None
    n_cells = len(ids)
    with path.open("w", encoding="utf-8", newline="") as f:
        for a in range(0, first.sizes["time"], _CHUNK_DAYS):
            b = min(a + _CHUNK_DAYS, first.sizes["time"])
            df = pd.DataFrame(
                {
                    "time": np.repeat(times[a:b], n_cells),
                    "lat": np.tile(grid.point_lat, b - a),
                    "lon": np.tile(grid.point_lon, b - a),
                }
            )
            if period is not None:
                df.insert(3, "period", np.repeat(period[a:b], n_cells))
            for n, values in columns.items():
                df[n] = values[a:b].ravel()
            df = df.dropna(subset=list(columns), how="all")
            df.to_csv(f, index=False, header=(a == 0))
    return path


def _write_grid(
    scen: xr.DataArray,
    hist_eval: xr.Dataset | None,
    grid: TargetGrid,
    var_dir: Path,
    var_name: str,
    formats: list[str],
) -> tuple[dict[str, Path], dict[str, Path] | None]:
    writers = {"netcdf": _write_grid_netcdf, "csv": _write_grid_csv}
    scen_paths, eval_paths = {}, {} if hist_eval is not None else None
    for fmt in formats:
        if fmt not in writers:
            raise ValueError(
                f"Unsupported format: {fmt!r}. Valid formats: {sorted(writers)}."
            )
        ext = _EXTENSIONS[fmt]
        scen_paths[fmt] = writers[fmt]({var_name: scen}, grid, var_dir / f"scen.{ext}")
        if hist_eval is not None and eval_paths is not None:
            data = {n: hist_eval[n] for n in ("obs", "raw", "corrected")}
            eval_paths[fmt] = writers[fmt](data, grid, var_dir / f"hist_eval.{ext}")
    return scen_paths, eval_paths


def write_result(
    scen: xr.DataArray,
    adj,
    *,
    run_dir: Path,
    var_name: str,
    formats: list[str],
    evaluation: pd.DataFrame | None = None,
    hist_eval: xr.Dataset | None = None,
    grid: TargetGrid | None = None,
) -> dict:
    """Write one variable's results under run_dir/<var_name>/. Returns the written paths."""
    var_dir = run_dir / var_name
    var_dir.mkdir(parents=True, exist_ok=True)

    if grid is not None:
        scen_paths, hist_eval_paths = _write_grid(
            scen, hist_eval, grid, var_dir, var_name, formats
        )
        adj_path = _write_netcdf(adj.ds, var_dir / "adj.nc")
        eval_path = None
        if evaluation is not None:
            eval_path = var_dir / "evaluation.csv"
            evaluation.to_csv(eval_path, index=False)
        return {
            "scen": scen_paths,
            "adj": adj_path,
            "evaluation": eval_path,
            "hist_eval": hist_eval_paths,
        }

    scen_paths: dict[str, Path] = {}
    for fmt in formats:
        if fmt not in _WRITERS:
            raise ValueError(
                f"Unsupported format: {fmt!r}. Valid formats: {sorted(_WRITERS)}."
            )
        scen_paths[fmt] = _WRITERS[fmt](
            scen, var_dir / f"scen.{_EXTENSIONS[fmt]}", var_name
        )

    adj_path = _write_netcdf(adj.ds, var_dir / "adj.nc")

    eval_path = None
    if evaluation is not None:
        eval_path = var_dir / "evaluation.csv"
        evaluation.to_csv(eval_path, index=False)

    hist_eval_paths = (
        _write_hist_eval(hist_eval, var_dir, formats) if hist_eval is not None else None
    )

    return {
        "scen": scen_paths,
        "adj": adj_path,
        "evaluation": eval_path,
        "hist_eval": hist_eval_paths,
    }


def write_qa(run_dir: Path, qa: list[QACheck]) -> Path:
    """Write every QACheck of the run to run_dir/qa.json."""
    path = run_dir / "qa.json"
    path.write_text(
        json.dumps([c.as_dict() for c in qa], indent=2, default=str), encoding="utf-8"
    )
    return path


def write_manifest(
    run_dir: Path,
    per_variable: dict[str, dict | None],
    *,
    config_path: Path | None = None,
    variable_statuses: dict[str, str] | None = None,
    cancelled: bool = False,
) -> Path:
    """Write run_dir/manifest.json (layout in the module docstring).

    A None entry means the variable failed, either in the pipeline or while
    writing; it is still listed, so a reader can tell a failed variable from
    a missing one. Paths are relative to run_dir, in POSIX form, so the run
    folder stays valid when zipped, shared or moved to another machine.
    """

    def rel(p: Path | None) -> str | None:
        return None if p is None else p.relative_to(run_dir).as_posix()

    variables: dict[str, dict] = {}
    for name, written in per_variable.items():
        if written is None:
            variables[name] = {"status": "failed"}
        else:
            variables[name] = {
                "status": "ok",
                "scen": {fmt: rel(p) for fmt, p in written["scen"].items()},
                "adj": rel(written["adj"]),
                "evaluation": rel(written.get("evaluation")),
                "hist_eval": (
                    {fmt: rel(p) for fmt, p in written["hist_eval"].items()}
                    if written.get("hist_eval")
                    else None
                ),
            }

    for name, status in (variable_statuses or {}).items():
        if name not in variables:
            variables[name] = {"status": status}

    manifest = {
        "status": "cancelled"
        if cancelled
        else (
            "failed"
            if any(v["status"] == "failed" for v in variables.values())
            else "ok"
        ),
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "bcflow_version": bcflow_version(),
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "qa": "qa.json",
        "config": rel(config_path),
        "variables": variables,
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


def write_run(
    out: dict,
    output_cfg: OutputConfig,
    *,
    config: dict | None = None,
    control: RunControl | None = None,
) -> Path:
    """Write a whole run (the dict returned by pipeline.run_pipeline()) to disk.

    `config`, if given, is saved as config.json so the run can be reproduced.

    A failure while writing one variable is isolated the same way
    run_pipeline() isolates computation failures: a 'fail' QACheck is
    APPENDED to out["qa"] (so the caller's report shows it), the variable is
    marked failed in the manifest, and the other variables are still written.

    Completed results are still saved when control is cancelled. Cancelled and
    skipped variables have status-only manifest entries, without output files.
    Returns the path of manifest.json; its parent is the run directory.
    """
    run_dir = _new_run_dir(Path(output_cfg.path))

    config_path = None
    if config is not None:
        config_path = run_dir / "config.json"
        config_path.write_text(
            json.dumps(config, indent=2, default=str), encoding="utf-8"
        )

    per_variable: dict[str, dict | None] = {}
    for name, res in out["results"].items():
        if res is None:  # already failed in the pipeline; its QACheck is in out["qa"]
            per_variable[name] = None
            continue
        if control is not None:
            # Cancellation stops computation, not preservation of completed results.
            control.emit({"event": "progress", "variable": name, "stage": "writing"})
        try:
            per_variable[name] = write_result(
                res["scen"],
                res["adj"],
                run_dir=run_dir,
                var_name=name,
                formats=[str(f) for f in output_cfg.formats],
                evaluation=res.get("evaluation"),
                hist_eval=res.get("hist_eval"),
                grid=res.get("grid"),
            )
        except Exception as exc:  # isolates write errors, per variable
            out["qa"].append(
                QACheck(
                    name=f"writers:{name}:exception",
                    status="fail",
                    message=f"Writing variable '{name}' failed: {exc}",
                    context={"variable": name},
                )
            )
            per_variable[name] = None

    write_qa(run_dir, out["qa"])  # after the loop, so write failures are included
    out["cancelled"] = out.get("cancelled", False) or (
        control is not None and control.cancelled
    )
    return write_manifest(
        run_dir,
        per_variable,
        config_path=config_path,
        variable_statuses=out.get("variable_statuses"),
        cancelled=out["cancelled"],
    )
