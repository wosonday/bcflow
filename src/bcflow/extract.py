"""Extract nearest data points, by grid_type."""

from __future__ import annotations

import numpy as np
import xarray as xr
from scipy.spatial import cKDTree

from .grouping import resolve_latlon_dims
from .qa import QACheck


def extract_nearest_regular_latlon(
    da: xr.DataArray, ref_points: xr.DataArray, *, lat_dim: str, lon_dim: str
) -> xr.DataArray:
    """Trích giá trị tại ô lưới gần nhất cho từng trạm trong ref_points.

    Vectorized theo chiều 'station': ref_points['lat']/['lon'] cùng mang
    chiều 'station', nên kết quả trả về cũng có chiều 'station' (khớp 1-1
    với ref) thay vì (lat, lon) như raster gốc.
    """
    out = da.sel({lat_dim: ref_points["lat"], lon_dim: ref_points["lon"]}, method="nearest")
    # Bỏ toạ độ lat/lon của Ô LƯỚI (khác giá trị thật của trạm trong ref) để
    # tránh xung đột toạ độ khi xsdba gộp ref/hist/sim theo chiều 'station'.
    return out.drop_vars([lat_dim, lon_dim], errors="ignore")


def _latlon_to_unit_sphere(lat_deg: np.ndarray, lon_deg: np.ndarray) -> np.ndarray:
    """Convert (lat, lon) in degrees to 3-D Cartesian points on the unit
    sphere. Needed because degrees are not isotropic -- a plain Euclidean
    distance on raw (lat, lon) would pick the wrong "nearest" cell at high
    latitudes, where a degree of longitude covers much less ground than near
    the equator.
    """
    lat_rad, lon_rad = np.radians(lat_deg), np.radians(lon_deg)
    return np.column_stack([
        np.cos(lat_rad) * np.cos(lon_rad),
        np.cos(lat_rad) * np.sin(lon_rad),
        np.sin(lat_rad),
    ])


def extract_nearest_rotated_pole(
    da: xr.DataArray, ref_points: xr.DataArray, *, lat_dim: str, lon_dim: str
) -> xr.DataArray:
    """Nearest-neighbor extraction on a rotated-pole (curvilinear) grid.

    `lat_dim`/`lon_dim` here name the 2-D auxiliary coordinates carrying the
    TRUE geographic lat/lon (shape matches the native dims, e.g.
    (rlat, rlon)) -- not 1-D dimension names as in the regular_latlon case.
    Works for any curvilinear grid carrying 2-D lat/lon aux coords, not only
    a strict CF rotated-pole grid, since it searches the true coordinates
    directly instead of parsing pole-rotation metadata.
    """
    lat2d, lon2d = da[lat_dim], da[lon_dim]
    if lat2d.ndim != 2:
        raise ValueError(
            f"extract_nearest_rotated_pole needs a 2-D '{lat_dim}' coordinate "
            f"(curvilinear grid); got {lat2d.ndim}-D. For a regular 1-D "
            "lat/lon grid, use grid_type='regular_latlon'."
        )
    native_dims = lat2d.dims  # vd. ("rlat", "rlon")

    tree = cKDTree(_latlon_to_unit_sphere(lat2d.values.ravel(), lon2d.values.ravel()))
    station_xyz = _latlon_to_unit_sphere(ref_points["lat"].values, ref_points["lon"].values)
    _, flat_idx = tree.query(station_xyz)

    idx0, idx1 = np.unravel_index(flat_idx, lat2d.shape)
    out = da.isel({
        native_dims[0]: xr.DataArray(idx0, dims="station"),
        native_dims[1]: xr.DataArray(idx1, dims="station"),
    })
    out = out.assign_coords(station=ref_points["station"])
    return out.drop_vars([lat_dim, lon_dim], errors="ignore")


GRID_EXTRACTORS = {
    "regular_latlon": extract_nearest_regular_latlon,
    "rotated_pole": extract_nearest_rotated_pole,
}


def extract_nearest(
    da: xr.DataArray, ref_points: xr.DataArray, grid_cfg, qa: list[QACheck], *, source_label: str = ""
) -> xr.DataArray:
    grid_type = grid_cfg.grid_type
    if grid_type not in GRID_EXTRACTORS:
        raise ValueError(
            f"Invalid grid_type: {grid_type!r}. Valid grid types: {sorted(GRID_EXTRACTORS)}."
        )
    lat_dim, lon_dim = resolve_latlon_dims(da, grid_cfg, qa, source_label=source_label)
    extractor = GRID_EXTRACTORS[grid_type]
    return extractor(da, ref_points, lat_dim=lat_dim, lon_dim=lon_dim)
