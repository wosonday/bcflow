"""Read the inputs of one variable: station series and model files.

Station data use the swat-preprocessing (swatprep) layout, so the same
folder serves both tools:

    weather_stations.csv        id,name,lat,lon,elev
                                ST001,StationA,15.5,105.2,120
    weather/ST001.csv           date,precip,tmax,tmin
                                2000-01-01,0.0,31.2,22.4

Headers are read without case or surrounding spaces, and LONG / LONGITUDE /
LATITUDE / ELEVATION are accepted (as swatprep/core/weather.py does).

Error convention, as in swatprep: a station whose file or column is missing
is skipped with a 'warn'; if no station is left, a 'fail' is recorded and
an error raised, because an empty array fails far less clearly inside xsdba.
"""

from __future__ import annotations

from collections.abc import Hashable
from pathlib import Path
from typing import NoReturn

import numpy as np
import pandas as pd
import xarray as xr
from xsdba.units import str2pint

from .config_schema import GridReferenceConfig, ModelConfig, StationsConfig, _span
from .grouping import resolve_latlon_dims
from .qa import QACheck
from .regrid import TargetGrid

_REQUIRED_META = ("id", "name", "lat", "lon")
_META_ALIASES = {
    "long": "lon",
    "longitude": "lon",
    "latitude": "lat",
    "elevation": "elev",
}
_TIME_NAMES = ("time", "t", "date", "valid_time")


def _column(name: str) -> str:
    key = str(name).strip().lower()
    return _META_ALIASES.get(key, key)


def load_station_metadata(stations: StationsConfig) -> pd.DataFrame:
    """The metadata table, indexed by station id (text), with name, lat, lon."""
    path = Path(stations.metadata)
    meta = pd.read_csv(path, dtype=str, encoding="utf-8-sig", skipinitialspace=True)
    meta.columns = [_column(c) for c in meta.columns]
    missing = [c for c in _REQUIRED_META if c not in meta.columns]
    if missing:
        raise ValueError(
            f"{path.name}: missing column(s) {missing} (found: {list(meta.columns)}); "
            "expected id,name,lat,lon[,elev]."
        )
    for col in ("id", "name"):
        meta[col] = meta[col].str.strip()
    for col in ("lat", "lon"):
        values = pd.Series(
            pd.to_numeric(meta[col].str.strip(), errors="coerce"),
            index=meta.index,
            dtype=float,
        )
        bad = meta.loc[values.isna(), "id"].tolist()
        if bad:
            raise ValueError(
                f"{path.name}: {col} is not a number for station(s) {bad[:5]}."
            )
        meta[col] = values
    duplicated = sorted(meta.loc[meta["id"].duplicated(), "id"].unique())
    if duplicated:
        raise ValueError(
            f"{path.name}: station id(s) listed more than once: {duplicated[:5]}."
        )
    return meta.set_index("id")


def station_file(stations: StationsConfig, station_id: str, name: str) -> Path:
    pattern = stations.file_pattern.replace("{id}", station_id).replace("{name}", name)
    return Path(stations.data_dir) / pattern


def load_station_variable(
    stations: StationsConfig, variable: str, units: str, qa: list[QACheck]
) -> xr.DataArray:
    """(time, station) observations of `variable`, with lat/lon per station."""
    meta = load_station_metadata(stations)
    series: dict[str, pd.Series] = {}
    no_file: list[str] = []
    no_column: list[str] = []

    for sid, row in meta.iterrows():
        path = station_file(stations, str(sid), str(row["name"]))
        if not path.is_file():
            no_file.append(str(sid))
            continue
        df = pd.read_csv(path, encoding="utf-8-sig", skipinitialspace=True)
        df.columns = [str(c).strip().lower() for c in df.columns]
        if "date" not in df.columns or variable not in df.columns:
            no_column.append(str(sid))
            continue
        values = np.asarray(pd.to_numeric(df[variable], errors="coerce"), dtype=float)
        index = pd.DatetimeIndex(pd.to_datetime(df["date"]), name="time")
        series[str(sid)] = (
            pd.Series(values, index=index).groupby(level=0).first().sort_index()
        )

    _warn_skipped(
        qa,
        variable,
        "missing_file",
        no_file,
        f"no file matching '{stations.file_pattern}'",
    )
    _warn_skipped(
        qa, variable, "missing_column", no_column, f"no 'date' or '{variable}' column"
    )

    if not series:
        qa.append(
            QACheck(
                name=f"stations:{variable}:none_loaded",
                status="fail",
                message=(
                    f"No station has '{variable}' data in {stations.data_dir} "
                    f"(file_pattern '{stations.file_pattern}')."
                ),
                context={"variable": variable},
            )
        )
        raise ValueError(
            f"No station data for '{variable}' -- see QACheck 'stations:{variable}:none_loaded'."
        )

    wide = pd.DataFrame(series)
    ids = [str(c) for c in wide.columns]
    data = wide.to_numpy(float)
    sentinel = [s for s, hit in zip(ids, (data == -99).any(axis=0)) if hit]
    if sentinel:
        qa.append(
            QACheck(
                name=f"stations:{variable}:minus_99",
                status="warn",
                message=(
                    f"{len(sentinel)} station(s) contain -99 in '{variable}', a common missing-value code; "
                    f"it is used as a real value. Leave the cell empty if it means missing: {sentinel[:10]}"
                ),
                context={"variable": variable, "stations": sentinel},
            )
        )
    qa.append(
        QACheck(
            name=f"stations:{variable}:loaded",
            status="pass",
            message=f"Loaded '{variable}' for {len(ids)} station(s).",
            context={"variable": variable, "n_stations": len(ids)},
        )
    )
    return xr.DataArray(
        data,
        dims=("time", "station"),
        coords={
            "time": wide.index,
            "station": ids,
            "name": ("station", meta.loc[ids, "name"].to_numpy(str)),
            "lat": ("station", meta.loc[ids, "lat"].to_numpy(float)),
            "lon": ("station", meta.loc[ids, "lon"].to_numpy(float)),
        },
        attrs={"units": units},
        name=variable,
    )


def _warn_skipped(
    qa: list[QACheck], variable: str, kind: str, ids: list[str], why: str
) -> None:
    if ids:
        qa.append(
            QACheck(
                name=f"stations:{variable}:{kind}",
                status="warn",
                message=f"{len(ids)} station(s) skipped for '{variable}' ({why}): {ids[:10]}",
                context={"variable": variable, "stations": ids},
            )
        )


def _open(path: Path) -> xr.Dataset:
    if path.suffix.lower() == ".zarr" or path.is_dir():
        return xr.open_zarr(path)
    return xr.open_dataset(path)


def _time_dim(da: xr.DataArray, time_name: str) -> str | None:
    available = {str(n).strip().lower(): str(n) for n in (*da.dims, *da.coords)}
    if time_name != "auto":
        return available.get(time_name.strip().lower())
    return next((available[c] for c in _TIME_NAMES if c in available), None)


def load_model_variable(
    model: ModelConfig, which: str, variable: str, qa: list[QACheck]
) -> xr.DataArray:
    """The model field of `variable` from the hist or sim file, time axis named 'time'."""
    path = Path(model.path(which, variable))  # type: ignore[arg-type]
    file_var = model.var_map[variable]  # type: ignore[index]
    label = f"{variable}:{which}"
    if not path.exists():
        qa.append(
            QACheck(
                name=f"model:{label}:missing_file",
                status="fail",
                message=f"Model {which} file for '{variable}' not found: {path}",
                context={"variable": variable, "path": str(path)},
            )
        )
        raise ValueError(f"Model {which} file not found: {path}")
    ds = _open(path)
    if file_var not in ds.data_vars:
        names = sorted(str(v) for v in ds.data_vars)
        qa.append(
            QACheck(
                name=f"model:{label}:missing_variable",
                status="fail",
                message=f"{path.name} has no variable '{file_var}' (model.var_map.{variable}). Found: {names}.",
                context={"variable": variable, "path": str(path), "available": names},
            )
        )
        raise ValueError(
            f"'{file_var}' not in {path.name} -- see QACheck 'model:{label}:missing_variable'."
        )

    da = ds[file_var]
    time = _time_dim(da, model.time_name)
    if time is None:
        qa.append(
            QACheck(
                name=f"model:{label}:time_not_found",
                status="fail",
                message=(
                    f"No time axis in '{file_var}' of {path.name} (time_name={model.time_name!r}); "
                    f"dims: {[str(d) for d in da.dims]}. Set model.time_name in the config."
                ),
                context={"variable": variable, "path": str(path)},
            )
        )
        raise ValueError(
            f"No time axis in {path.name} -- see QACheck 'model:{label}:time_not_found'."
        )
    if time != "time":
        da = da.rename({time: "time"})
    override = model.units.get(variable)  # type: ignore[call-overload]
    return da.assign_attrs(units=override) if override else da


# --- gridded reference ---------------------------------------------------------


def open_grid_reference(
    grid: GridReferenceConfig, variable: str, qa: list[QACheck]
) -> tuple[xr.DataArray, Hashable, Hashable]:
    """The reference field (time, lat, lon), opened lazily, cut to `grid.region`.

    Returns (data, lat dim, lon dim). Units come from `grid.units` when set,
    otherwise from the file, and must be a unit pint understands -- CDO
    often writes the placeholder "var_unit", which must be overridden.
    """
    path = Path(grid.path)
    label = f"{variable}:ref"
    file_var = grid.var_map[variable]  # type: ignore[index]
    if not path.exists():
        _fail(
            qa,
            f"grid:{label}:missing_file",
            f"Reference grid not found: {path}",
            variable,
        )
    ds = _open(path)
    if file_var not in ds.data_vars:
        names = sorted(str(v) for v in ds.data_vars)
        _fail(
            qa,
            f"grid:{label}:missing_variable",
            f"{path.name} has no variable '{file_var}' (grid.var_map.{variable}). Found: {names}.",
            variable,
        )
    da = ds[file_var]
    time = _time_dim(da, grid.time_name)
    if time is None:
        _fail(
            qa,
            f"grid:{label}:time_not_found",
            f"No time axis in '{file_var}' of {path.name}.",
            variable,
        )
    if time != "time":
        da = da.rename({time: "time"})

    units = grid.units.get(variable) or da.attrs.get("units", "")  # type: ignore[call-overload]
    try:
        str2pint(units)
    except Exception:  # noqa: BLE001 -- pint raises several error types for bad strings
        _fail(
            qa,
            f"grid:{label}:bad_units",
            f"'{file_var}' in {path.name} has units {units!r}, which are not a valid unit; "
            f"set grid.units.{variable} in the config (e.g. 'mm d-1' or 'degC').",
            variable,
        )
    da = da.assign_attrs(units=units)

    lat, lon = resolve_latlon_dims(
        da, grid.lat_name, grid.lon_name, qa, source_label=label
    )
    if da[lat].ndim != 1 or da[lon].ndim != 1:
        _fail(
            qa,
            f"grid:{label}:not_regular",
            "The reference grid must have 1-D lat and lon.",
            variable,
        )
    lat_dim, lon_dim = da[lat].dims[0], da[lon].dims[0]
    if grid.region is not None:
        la, lo = da[lat].values, da[lon].values
        keep_lat = np.flatnonzero(
            (la >= grid.region.lat[0]) & (la <= grid.region.lat[1])
        )
        keep_lon = np.flatnonzero(
            (lo >= grid.region.lon[0]) & (lo <= grid.region.lon[1])
        )
        if keep_lat.size == 0 or keep_lon.size == 0:
            _fail(
                qa,
                f"grid:{label}:empty_region",
                "grid.region contains no cell of the grid.",
                variable,
            )
        da = da.isel({lat_dim: keep_lat, lon_dim: keep_lon})
    return da.transpose("time", lat_dim, lon_dim), lat, lon


def scan_grid(
    da: xr.DataArray, period: tuple[str, str] | None, *, chunk: int = 365
) -> tuple[np.ndarray, np.ndarray, int]:
    """One pass over the reference, a year at a time, to save memory.

    Returns (cells with any data, valid days per cell in the calibration
    window, number of calendar days in that window). Cells without any
    data (sea, outside the country) are not corrected at all.
    """
    index = da.indexes["time"]
    days = pd.DatetimeIndex(
        index if isinstance(index, pd.DatetimeIndex) else index.to_datetimeindex()
    ).floor("D")
    start, end = (days[0], days[-1]) if period is None else _span(period, "calibration")
    window = pd.date_range(start, end, freq="D").floor("D")
    in_window = days.isin(window)
    has_data = np.zeros(da.shape[1:], dtype=bool)
    valid = np.zeros(da.shape[1:], dtype=int)
    for a in range(0, da.sizes["time"], chunk):
        finite = np.isfinite(np.asarray(da.isel(time=slice(a, a + chunk)).values))
        has_data |= finite.any(axis=0)
        valid += finite[in_window[a : a + chunk]].sum(axis=0)
    return has_data, valid, len(window)


def grid_target(
    da: xr.DataArray, lat: Hashable, lon: Hashable, has_data: np.ndarray
) -> TargetGrid:
    """The cells to correct, row by row."""
    iy, ix = np.nonzero(has_data)
    return TargetGrid(
        np.asarray(da[lat].values, float), np.asarray(da[lon].values, float), iy, ix
    )


def load_grid_points(
    da: xr.DataArray, target: TargetGrid, variable: str
) -> xr.DataArray:
    """(time, station) reference values of the target cells; 'station' holds the cell ids."""
    lat_dim = da.dims[1]  # rows; opened as (time, lat, lon)
    top, bottom = int(target.iy.min()), int(target.iy.max())
    band = np.asarray(da.isel({lat_dim: slice(top, bottom + 1)}).values, dtype=float)
    return xr.DataArray(
        band[:, target.iy - top, target.ix],
        dims=("time", "station"),
        coords={
            "time": da["time"].values,
            "station": target.ids,
            "lat": ("station", target.point_lat),
            "lon": ("station", target.point_lon),
        },
        attrs={"units": da.attrs.get("units", "")},
        name=variable,
    )


def _fail(qa: list[QACheck], name: str, message: str, variable: str) -> NoReturn:
    qa.append(
        QACheck(
            name=name, status="fail", message=message, context={"variable": variable}
        )
    )
    raise ValueError(f"{message} -- see QACheck '{name}'.")
