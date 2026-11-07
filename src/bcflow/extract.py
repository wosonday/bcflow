"""Take, for each station, the model grid cell nearest to it.

Two grid layouts are recognised automatically from the latitude coordinate:

- regular: 1-D lat and lon (most GCMs, e.g. CNRM-ESM2-1 on 128 x 256);
- curvilinear: 2-D lat and lon (rotated-pole RCMs, ocean grids, ...),
  searched with a KD-tree on the unit sphere, because degrees of longitude
  shrink towards the poles and a plain (lat, lon) distance would pick the
  wrong cell at high latitude.

If the nearest cell has no data at all (a land/sea mask, a gap in the
grid), the cells within `search_radius` steps around it are tried from the
nearest to the farthest, as swat-preprocessing does for its NetCDF fill
source (swatprep/core/fill_sources.py).
"""

from __future__ import annotations

from collections.abc import Hashable

import numpy as np
import xarray as xr
from scipy.spatial import KDTree

from .grouping import resolve_latlon_dims
from .qa import QACheck


def _match_lon(lons: np.ndarray, grid_lon: np.ndarray) -> np.ndarray:
    """Station longitudes in the grid's convention (0..360 or -180..180).

    Changed only when needed, so 107.35 stays exactly 107.35.
    """
    lons = np.asarray(lons, float)
    if np.nanmax(grid_lon) > 180:
        return np.where(lons < 0, lons + 360.0, lons)
    return np.where(lons > 180, lons - 360.0, lons)


def _unit_sphere(lat_deg: np.ndarray, lon_deg: np.ndarray) -> np.ndarray:
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    return np.column_stack(
        [np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)]
    )


class _Regular:
    """1-D lat and lon: cells are (i, j) = (lat index, lon index)."""

    def __init__(self, da: xr.DataArray, lat: Hashable, lon: Hashable):
        self.dims = (da[lat].dims[0], da[lon].dims[0])
        self.glat = np.asarray(da[lat].values, float)
        self.glon = np.asarray(da[lon].values, float)

    def nearest(self, slat: np.ndarray, slon: np.ndarray):
        """Nearest (i, j) per station, and True where the station lies outside the grid."""
        slon = _match_lon(slon, self.glon)
        # Rounded so a station exactly between two cells always takes the lower index.
        i = np.round(np.abs(self.glat[:, None] - slat[None, :]), 9).argmin(axis=0)
        j = np.round(np.abs(self.glon[:, None] - slon[None, :]), 9).argmin(axis=0)
        outside = (np.abs(self.glat[i] - slat) > _step(self.glat)) | (
            np.abs(self.glon[j] - slon) > _step(self.glon)
        )
        return i, j, outside

    def candidates(self, slat: float, slon: float, i: int, j: int, radius: int):
        """Cells within +-radius of (i, j), nearest to the station first."""
        slon = float(_match_lon(np.array([slon]), self.glon)[0])
        ii = np.arange(max(i - radius, 0), min(i + radius, self.glat.size - 1) + 1)
        jj = np.arange(max(j - radius, 0), min(j + radius, self.glon.size - 1) + 1)
        dlat = (self.glat[ii] - slat)[:, None]
        dlon = (self.glon[jj] - slon)[None, :] * np.cos(np.radians(slat))
        order = np.argsort(np.round(dlat**2 + dlon**2, 12).ravel(), kind="stable")
        return [(int(ii[k // jj.size]), int(jj[k % jj.size])) for k in order]

    def coords(self, i: int, j: int) -> tuple[float, float]:
        return float(self.glat[i]), float(self.glon[j])


class _Curvilinear:
    """2-D lat and lon: cells are (i, j) along the coordinate's two native dims."""

    def __init__(self, da: xr.DataArray, lat: Hashable, lon: Hashable):
        self.dims = da[lat].dims
        self.glat = np.asarray(da[lat].values, float)
        self.glon = np.asarray(da[lon].values, float)
        self.xyz = _unit_sphere(self.glat.ravel(), self.glon.ravel())
        self.tree = KDTree(self.xyz, leafsize=16)

    def nearest(self, slat: np.ndarray, slon: np.ndarray):
        _, flat = self.tree.query(_unit_sphere(slat, slon))
        i, j = np.unravel_index(flat, self.glat.shape)
        return np.asarray(i), np.asarray(j), np.zeros(len(slat), dtype=bool)

    def candidates(self, slat: float, slon: float, i: int, j: int, radius: int):
        n0, n1 = self.glat.shape
        cells = [
            (a, b)
            for a in range(max(i - radius, 0), min(i + radius, n0 - 1) + 1)
            for b in range(max(j - radius, 0), min(j + radius, n1 - 1) + 1)
        ]
        here = _unit_sphere(np.array([slat]), np.array([slon]))[0]
        dist = [np.linalg.norm(self.xyz[a * n1 + b] - here) for a, b in cells]
        return [cells[k] for k in np.argsort(np.round(dist, 12), kind="stable")]

    def coords(self, i: int, j: int) -> tuple[float, float]:
        return float(self.glat[i, j]), float(self.glon[i, j])


def _step(grid: np.ndarray) -> float:
    return float(np.median(np.abs(np.diff(grid)))) if grid.size > 1 else np.inf


def _cell(da: xr.DataArray, dims, i: int, j: int) -> xr.DataArray:
    return da.isel({dims[0]: i, dims[1]: j})


def extract_nearest(
    da: xr.DataArray,
    ref: xr.DataArray,
    qa: list[QACheck],
    *,
    lat_name: str = "auto",
    lon_name: str = "auto",
    search_radius: int = 1,
    source_label: str,
) -> xr.DataArray:
    """(time, station) series of the cell nearest to each station of `ref`.

    `ref` carries 'lat' and 'lon' coords along 'station'. The returned
    array has ref's 'station' coordinate and no grid coordinates (they
    would clash with the stations' own lat/lon when xsdba aligns them).
    """
    variable = source_label.split(":")[0]
    lat, lon = resolve_latlon_dims(
        da, lat_name, lon_name, qa, source_label=source_label
    )
    grid = _Regular(da, lat, lon) if da[lat].ndim == 1 else _Curvilinear(da, lat, lon)

    stations = [str(s) for s in ref["station"].values]
    slat = np.asarray(ref["lat"].values, float)
    slon = np.asarray(ref["lon"].values, float)
    i, j, outside = grid.nearest(slat, slon)

    out = da.isel(
        {
            grid.dims[0]: xr.DataArray(i, dims="station"),
            grid.dims[1]: xr.DataArray(j, dims="station"),
        }
    )
    out = out.drop_vars([c for c in out.coords if c != "time"], errors="ignore")
    out = (
        out.assign_coords(station=ref["station"].values)
        .transpose("time", "station")
        .load()
    )

    empty = out.isnull().all("time").values
    moved: dict[str, dict] = {}
    no_data: list[str] = []
    for k in np.flatnonzero(empty):
        found = None
        if search_radius > 0:
            for a, b in grid.candidates(
                slat[k], slon[k], int(i[k]), int(j[k]), search_radius
            ):
                series = _cell(da, grid.dims, a, b).load()
                if bool(series.notnull().any()):
                    found = (a, b, series)
                    break
        if found is None:
            no_data.append(stations[k])
            continue
        a, b, series = found
        out[:, k] = series.values
        i[k], j[k] = a, b
        cell_lat, cell_lon = grid.coords(a, b)
        moved[stations[k]] = {"lat": cell_lat, "lon": cell_lon}

    cells = {(int(a), int(b)) for a, b in zip(i, j)}
    qa.append(
        QACheck(
            name=f"extract:{source_label}:nearest",
            status="pass",
            message=(
                f"{source_label}: {len(stations)} station(s) read from {len(cells)} grid cell(s) "
                f"({'regular' if isinstance(grid, _Regular) else 'curvilinear'} grid, lat='{lat}', lon='{lon}')."
            ),
            context={
                "variable": variable,
                "n_stations": len(stations),
                "n_cells": len(cells),
            },
        )
    )
    if outside.any():
        names = [s for s, o in zip(stations, outside) if o]
        qa.append(
            QACheck(
                name=f"extract:{source_label}:outside_grid",
                status="warn",
                message=(
                    f"{source_label}: {len(names)} station(s) lie more than one grid step outside "
                    f"the model grid; the edge cell is used: {names[:10]}"
                ),
                context={"variable": variable, "stations": names},
            )
        )
    if moved:
        qa.append(
            QACheck(
                name=f"extract:{source_label}:neighbor_cell",
                status="warn",
                message=(
                    f"{source_label}: the nearest cell has no data for {len(moved)} station(s); "
                    f"a neighbouring cell within {search_radius} step(s) is used: {sorted(moved)[:10]}"
                ),
                context={"variable": variable, "cells": moved},
            )
        )
    if no_data:
        qa.append(
            QACheck(
                name=f"extract:{source_label}:no_data",
                status="warn",
                message=(
                    f"{source_label}: no model data within {search_radius} cell(s) of "
                    f"{len(no_data)} station(s); they stay empty: {no_data[:10]}"
                ),
                context={"variable": variable, "stations": no_data},
            )
        )
    return out
