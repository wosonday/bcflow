"""Regrid a model field onto the cells of a reference grid.

Used when the reference is gridded observations (e.g. OBS.Vietnam at 0.1
degree) instead of stations: the question is the same as extract.py's --
"what is the model value at each reference location?" -- answered for
every cell of the grid instead of picking one cell per station.

Every method is a fixed set of weights per target cell,

    target[t] = sum_s  w[t, s] * source[s]

so all of them share one sparse matrix product (`_apply`), only the
weights differ:

    nearest       the closest source cell (no smoothing)
    idw           inverse-distance weighting of the k closest cells,
                  w ~ 1/d^power (Shepard, 1968, Proc. 23rd ACM National
                  Conference, 517-524, doi:10.1145/800186.810616)
    bilinear      linear in latitude and longitude between the 4 cells around
    bicubic       cubic convolution on the 4x4 cells around (Keys, 1981,
                  IEEE Trans. ASSP 29(6), 1153-1160, doi:10.1109/TASSP.1981.1163711)
    conservative  first-order conservative remapping: area of overlap on the
                  sphere / area of the target cell, so area totals are kept
                  (Jones, 1999, Mon. Wea. Rev. 127(9), 2204-2210,
                  doi:10.1175/1520-0493(1999)127<2204:FASOCR>2.0.CO;2);
                  recommended for precipitation
    patch         a quadratic polynomial fitted by least squares to the 4x4
                  cells around, evaluated at the target -- the patch-recovery
                  idea (Zienkiewicz & Zhu, 1992, Int. J. Numer. Meth. Eng.
                  33(7), 1331-1364, doi:10.1002/nme.1620330702). ESMF's
                  'patch' follows the same idea with its own patch layout,
                  so values are close to, not identical with, ESMF's.

Missing source values: weights are renormalised over the valid cells,
and a target whose valid weights sum to less than half stays NaN.

Only rectilinear source grids (1-D lat and lon, as CMIP 'gr' grids) are
supported here.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any

import numpy as np
import xarray as xr
from scipy import sparse

from .extract import _match_lon
from .grouping import resolve_latlon_dims
from .qa import QACheck

METHODS = ("nearest", "idw", "bilinear", "bicubic", "conservative", "patch")

_HALO = 3  # extra source cells kept around the targets (bicubic/patch need 2)


@dataclass(frozen=True)
class TargetGrid:
    """Cells of a regular reference grid that are corrected, in row-major order.

    `lat`/`lon` are the grid's 1-D axes; cell k is (lat[iy[k]], lon[ix[k]]).
    """

    lat: np.ndarray
    lon: np.ndarray
    iy: np.ndarray
    ix: np.ndarray

    @property
    def size(self) -> int:
        return int(self.iy.size)

    @property
    def point_lat(self) -> np.ndarray:
        return self.lat[self.iy]

    @property
    def point_lon(self) -> np.ndarray:
        return self.lon[self.ix]

    @property
    def ids(self) -> list[str]:
        """One label per cell, used as the 'station' coordinate."""
        return [f"{a:.4f}_{b:.4f}" for a, b in zip(self.point_lat, self.point_lon)]

    def block(self, start: int, stop: int) -> TargetGrid:
        return TargetGrid(self.lat, self.lon, self.iy[start:stop], self.ix[start:stop])

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        """Cell edges along lat and lon (midpoints, half a step beyond the ends)."""
        return _edges(self.lat, clip=90.0), _edges(self.lon, clip=None)


def _edges(axis: np.ndarray, *, clip: float | None) -> np.ndarray:
    """Edges of cells centred on a monotonic 1-D axis (n centres -> n+1 edges)."""
    a = np.asarray(axis, float)
    if a.size == 1:
        out = np.array([a[0] - 0.5, a[0] + 0.5])
    else:
        mid = (a[:-1] + a[1:]) / 2
        out = np.concatenate(
            [[a[0] - (mid[0] - a[0])], mid, [a[-1] + (a[-1] - mid[-1])]]
        )
    return np.clip(out, -clip, clip) if clip is not None else out


class _Source:
    """The model grid, sorted, with longitude extended across the 0/360 seam."""

    def __init__(self, lat: np.ndarray, lon: np.ndarray):
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        self.nlat, self.nlon = lat.size, lon.size
        self.lat_order = np.argsort(lat, kind="stable")
        self.lat = lat[self.lat_order]
        lon_order = np.argsort(lon, kind="stable")
        sorted_lon = lon[lon_order]
        step = float(np.median(np.diff(sorted_lon))) if lon.size > 1 else 360.0
        self.is_global = sorted_lon[-1] - sorted_lon[0] + step >= 359.9
        if self.is_global:
            h = min(_HALO, lon.size)
            self.lon = np.concatenate(
                [sorted_lon[-h:] - 360, sorted_lon, sorted_lon[:h] + 360]
            )
            self.lon_map = np.concatenate([lon_order[-h:], lon_order, lon_order[:h]])
        else:
            self.lon, self.lon_map = sorted_lon, lon_order
        self.raw_lon = lon

    def target_lon(self, lon: np.ndarray) -> np.ndarray:
        return _match_lon(lon, self.raw_lon)

    def flat(self, i_sorted: np.ndarray, j_ext: np.ndarray) -> np.ndarray:
        """Index into the source's (lat, lon) field, flattened row-major."""
        return self.lat_order[i_sorted] * self.nlon + self.lon_map[j_ext]


def _bracket(axis: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Lower neighbour index and fraction (0..1) of x between axis[i] and axis[i+1]."""
    i = np.clip(np.searchsorted(axis, x, side="right") - 1, 0, axis.size - 2)
    frac = (x - axis[i]) / (axis[i + 1] - axis[i])
    return i, np.clip(frac, 0.0, 1.0)


def _stencil(src: _Source, lat: np.ndarray, lon: np.ndarray):
    """4x4 neighbourhood around each target: (i, j) of shape (n, 4) and fractions."""
    i0, fy = _bracket(src.lat, lat)
    j0, fx = _bracket(src.lon, lon)
    offsets = np.arange(-1, 3)
    i = np.clip(i0[:, None] + offsets, 0, src.lat.size - 1)
    j = np.clip(j0[:, None] + offsets, 0, src.lon.size - 1)
    return i, j, fy, fx


def _outer(i: np.ndarray, j: np.ndarray, wy: np.ndarray, wx: np.ndarray):
    """Combine per-axis indices/weights (n, a) and (n, b) into (n, a*b)."""
    n = i.shape[0]
    ii = np.repeat(i, j.shape[1], axis=1)
    jj = np.tile(j, (1, i.shape[1]))
    w = (wy[:, :, None] * wx[:, None, :]).reshape(n, -1)
    return ii, jj, w


def _nearest(src: _Source, lat, lon, params):
    i0, fy = _bracket(src.lat, lat)
    j0, fx = _bracket(src.lon, lon)
    i = np.where(fy <= 0.5, i0, i0 + 1)[:, None]
    j = np.where(fx <= 0.5, j0, j0 + 1)[:, None]
    return i, j, np.ones_like(i, dtype=float)


def _bilinear(src: _Source, lat, lon, params):
    i0, fy = _bracket(src.lat, lat)
    j0, fx = _bracket(src.lon, lon)
    i = np.stack([i0, i0 + 1], axis=1)
    j = np.stack([j0, j0 + 1], axis=1)
    wy = np.stack([1 - fy, fy], axis=1)
    wx = np.stack([1 - fx, fx], axis=1)
    return _outer(i, j, wy, wx)


def _keys(t: np.ndarray, a: float = -0.5) -> np.ndarray:
    """Keys (1981) cubic-convolution weights for the 4 points at -1, 0, 1, 2."""
    d = np.abs(np.stack([1 + t, t, 1 - t, 2 - t], axis=1))
    near = (a + 2) * d**3 - (a + 3) * d**2 + 1
    far = a * d**3 - 5 * a * d**2 + 8 * a * d - 4 * a
    return np.where(d <= 1, near, np.where(d < 2, far, 0.0))


def _bicubic(src: _Source, lat, lon, params):
    i, j, fy, fx = _stencil(src, lat, lon)
    return _outer(i, j, _keys(fy), _keys(fx))


def _distance(src: _Source, ii, jj, lat, lon) -> np.ndarray:
    """Chord distance on the unit sphere from each target to its candidate cells."""

    def xyz(a, b):
        a, b = np.radians(a), np.radians(b)
        return np.stack(
            [np.cos(a) * np.cos(b), np.cos(a) * np.sin(b), np.sin(a)], axis=-1
        )

    return np.linalg.norm(
        xyz(src.lat[ii], src.lon[jj]) - xyz(lat, lon)[:, None, :], axis=-1
    )


def _idw(src: _Source, lat, lon, params):
    k = int(params.get("neighbours", 4))
    power = float(params.get("power", 2.0))
    i, j, _, _ = _stencil(src, lat, lon)
    ii, jj, _ = _outer(i, j, np.ones_like(i, float), np.ones_like(j, float))
    d = _distance(src, ii, jj, lat, lon)
    order = np.argsort(d, axis=1, kind="stable")[:, : max(1, min(k, 16))]
    ii, jj, d = (np.take_along_axis(x, order, axis=1) for x in (ii, jj, d))
    with np.errstate(divide="ignore"):
        w = 1.0 / d**power
    exact = d[:, 0] < 1e-12  # target on a source centre: take that value
    w[exact] = 0.0
    w[exact, 0] = 1.0
    return ii, jj, w / w.sum(axis=1, keepdims=True)


def _patch(src: _Source, lat, lon, params):
    i, j, _, _ = _stencil(src, lat, lon)
    ii, jj, _ = _outer(i, j, np.ones_like(i, float), np.ones_like(j, float))
    y = src.lat[ii] - lat[:, None]
    x = (src.lon[jj] - lon[:, None]) * np.cos(np.radians(lat))[:, None]
    design = np.stack(
        [np.ones_like(x), x, y, x * x, x * y, y * y], axis=-1
    )  # (n, 16, 6)
    # value at the target = constant term of the least-squares fit = row 0 of pinv
    w = np.linalg.pinv(design)[:, 0, :]
    return ii, jj, w


def _overlaps(
    t_edges: np.ndarray, s_edges: np.ndarray, *, sine: bool
) -> list[list[tuple[int, float]]]:
    """For each target interval, (source index, overlap fraction of the target)."""
    f = (lambda v: np.sin(np.radians(v))) if sine else (lambda v: v)
    out = []
    for lo, hi in zip(t_edges[:-1], t_edges[1:]):
        lo, hi = min(lo, hi), max(lo, hi)
        size = f(hi) - f(lo)
        pairs = []
        for s in range(s_edges.size - 1):
            a, b = sorted((s_edges[s], s_edges[s + 1]))
            top, bottom = min(hi, b), max(lo, a)
            if top > bottom and size > 0:
                pairs.append((s, float((f(top) - f(bottom)) / size)))
        out.append(pairs)
    return out


def _conservative(src: _Source, lat, lon, params, target: TargetGrid | None = None):
    if target is None:
        raise ValueError("conservative regridding needs the target grid's cells")
    t_lat_edges, t_lon_edges = target.bounds()
    # In the model's convention, kept continuous if the grid crosses the 0/360 seam.
    t_lon_edges = np.degrees(np.unwrap(np.radians(src.target_lon(t_lon_edges))))
    s_lat_edges = _edges(src.lat, clip=90.0)
    s_lon_edges = _edges(src.lon, clip=None)
    rows = _overlaps(t_lat_edges, s_lat_edges, sine=True)
    cols = _overlaps(t_lon_edges, s_lon_edges, sine=False)
    width = max(len(rows[a]) * len(cols[b]) for a, b in zip(target.iy, target.ix)) or 1
    n = target.size
    ii = np.zeros((n, width), int)
    jj = np.zeros((n, width), int)
    w = np.zeros((n, width))
    for k, (a, b) in enumerate(zip(target.iy, target.ix)):
        pairs = [(si, sj, wa * wb) for si, wa in rows[a] for sj, wb in cols[b]]
        for m, (si, sj, wk) in enumerate(pairs):
            ii[k, m], jj[k, m], w[k, m] = si, sj, wk
    # A target cell only partly inside the model grid: average over the covered part.
    total = w.sum(axis=1, keepdims=True)
    return ii, jj, np.divide(w, total, out=np.zeros_like(w), where=total > 0)


_WEIGHTS = {
    "nearest": _nearest,
    "idw": _idw,
    "bilinear": _bilinear,
    "bicubic": _bicubic,
    "conservative": _conservative,
    "patch": _patch,
}


def regrid_weights(
    src_lat: np.ndarray,
    src_lon: np.ndarray,
    target: TargetGrid,
    method: str,
    params: dict[str, Any] | None = None,
) -> sparse.csr_matrix:
    """Sparse (n_targets x n_source_cells) weight matrix; source cells row-major (lat, lon)."""
    if method not in _WEIGHTS:
        raise ValueError(
            f"Unknown regrid method {method!r}. Valid methods: {list(METHODS)}."
        )
    src = _Source(src_lat, src_lon)
    lat = np.asarray(target.point_lat, float)
    lon = src.target_lon(np.asarray(target.point_lon, float))
    fn = _WEIGHTS[method]
    if method == "conservative":
        ii, jj, w = _conservative(src, lat, lon, params or {}, target)
    else:
        ii, jj, w = fn(src, lat, lon, params or {})
    rows = np.repeat(np.arange(target.size), ii.shape[1])
    cols = src.flat(ii.ravel(), jj.ravel())
    m = sparse.coo_matrix(
        (w.ravel(), (rows, cols)), shape=(target.size, src.nlat * src.nlon)
    )
    out = sparse.csr_matrix(m)
    out.eliminate_zeros()
    return out


def _apply(data: np.ndarray, weights: sparse.csr_matrix) -> np.ndarray:
    """(time, n_source) @ weights.T, renormalised over the valid source values."""
    valid = np.isfinite(data)
    if valid.all():
        return np.asarray(data @ weights.T)
    filled = np.where(valid, data, 0.0)
    num = np.asarray(filled @ weights.T)
    den = np.asarray(valid.astype(float) @ weights.T)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    return np.where(den >= 0.5, out, np.nan)


def regrid_to_points(
    da: xr.DataArray,
    target: TargetGrid,
    qa: list[QACheck],
    *,
    method: str,
    params: dict[str, Any] | None = None,
    lat_name: str = "auto",
    lon_name: str = "auto",
    source_label: str,
) -> xr.DataArray:
    """(time, station) model values on the target cells; 'station' holds the cell ids."""
    variable = source_label.split(":")[0]
    lat, lon = resolve_latlon_dims(
        da, lat_name, lon_name, qa, source_label=source_label
    )
    if da[lat].ndim != 1 or da[lon].ndim != 1:
        raise ValueError(
            f"{source_label}: regridding needs a rectilinear model grid (1-D lat and lon)."
        )
    lat_dim: Hashable = da[lat].dims[0]
    lon_dim: Hashable = da[lon].dims[0]
    weights = regrid_weights(da[lat].values, da[lon].values, target, method, params)

    # Read only the source rows/columns the weights use (a small box around the targets).
    used = np.unique(weights.indices)
    nlon = da.sizes[lon_dim]
    rows, cols = np.unique(used // nlon), np.unique(used % nlon)
    box = da.isel({lat_dim: rows, lon_dim: cols}).transpose("time", lat_dim, lon_dim)
    values = np.asarray(box.values, dtype=float).reshape(box.sizes["time"], -1)
    # source column index in the full grid -> column in the box
    full = (rows[:, None] * nlon + cols[None, :]).ravel()
    remap = np.full(da.sizes[lat_dim] * nlon, -1)
    remap[full] = np.arange(full.size)
    w_box = sparse.csr_matrix(
        (weights.data, remap[weights.indices], weights.indptr),
        shape=(target.size, full.size),
    )

    out = xr.DataArray(
        _apply(values, w_box),
        dims=("time", "station"),
        coords={
            "time": box["time"].values,
            "station": target.ids,
            "lat": ("station", target.point_lat),
            "lon": ("station", target.point_lon),
        },
        attrs=dict(da.attrs),
        name=da.name,
    )
    empty = int(out.isnull().all("time").sum())
    qa.append(
        QACheck(
            name=f"regrid:{source_label}:{method}",
            status="warn" if empty else "pass",
            message=(
                f"{source_label}: regridded to {target.size} cell(s) with '{method}' "
                f"from a {rows.size}x{cols.size} block of model cells"
                + (f"; {empty} cell(s) got no model data." if empty else ".")
            ),
            context={
                "variable": variable,
                "method": method,
                "n_cells": target.size,
                "empty": empty,
            },
        )
    )
    return out
