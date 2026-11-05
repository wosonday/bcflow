"""Tests for extract.py: nearest-point extraction, both grid types.

extract_nearest_regular_latlon was previously only exercised indirectly
through test_pipeline_e2e.py's full runs; this file adds a direct unit test
for it. extract_nearest_rotated_pole is new here, verified against an
independent brute-force nearest-neighbor search over the whole grid, not
just by inspection.
"""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from bcflow.config_schema import RasterSourceConfig
from bcflow.extract import extract_nearest, extract_nearest_rotated_pole
from bcflow.qa import QACheck


def test_extract_nearest_regular_latlon_picks_the_closest_cell():
    lat = np.array([20.0, 21.0, 22.0])
    lon = np.array([105.0, 106.0, 107.0])
    da = xr.DataArray(
        np.arange(9).reshape(3, 3).astype(float),
        dims=("lat", "lon"),
        coords={"lat": lat, "lon": lon},
    )
    ref_points = xr.Dataset(coords={
        "station": ["A", "B"],
        "lat": ("station", [20.1, 21.9]),
        "lon": ("station", [106.9, 105.2]),
    })
    grid_cfg = RasterSourceConfig(format="netcdf", path="unused.nc", grid_type="regular_latlon")
    qa: list[QACheck] = []

    out = extract_nearest(da, ref_points, grid_cfg, qa, source_label="test")

    assert float(out.sel(station="A")) == 2.0  # lat idx 0, lon idx 2
    assert float(out.sel(station="B")) == 6.0  # lat idx 2, lon idx 0
    assert "lat" not in out.coords and "lon" not in out.coords
    assert not any(c.status == "fail" for c in qa)


def test_extract_nearest_rotated_pole_matches_brute_force_search():
    """Build a curvilinear grid (2-D lat/lon NOT aligned with rlat/rlon),
    pick stations near known cells, and cross-check against an independent
    brute-force nearest search over the whole grid.
    """
    rlat = np.linspace(-10, 10, 21)
    rlon = np.linspace(-15, 15, 31)
    RLON, RLAT = np.meshgrid(rlon, rlat)

    theta = np.radians(20)
    true_lat = 10 + RLAT * np.cos(theta) - RLON * np.sin(theta) * 0.3
    true_lon = 105 + RLON * np.cos(theta) + RLAT * np.sin(theta) * 0.3

    da = xr.DataArray(
        np.arange(21 * 31).reshape(21, 31).astype(float),
        dims=("rlat", "rlon"),
        coords={
            "lat": (("rlat", "rlon"), true_lat),
            "lon": (("rlat", "rlon"), true_lon),
        },
    )

    ref_points = xr.Dataset(coords={
        "station": ["A", "B"],
        "lat": ("station", [true_lat[5, 10] + 0.001, true_lat[15, 20]]),
        "lon": ("station", [true_lon[5, 10] + 0.001, true_lon[15, 20]]),
    })

    out = extract_nearest_rotated_pole(da, ref_points, lat_dim="lat", lon_dim="lon")

    assert float(out.sel(station="A")) == float(da.isel(rlat=5, rlon=10))
    assert float(out.sel(station="B")) == float(da.isel(rlat=15, rlon=20))
    assert "lat" not in out.coords and "lon" not in out.coords

    # Independent brute-force cross-check for station A -- not just inspection.
    d = np.sqrt((true_lat - ref_points["lat"].values[0]) ** 2 + (true_lon - ref_points["lon"].values[0]) ** 2)
    bf_idx = np.unravel_index(np.argmin(d), d.shape)
    assert bf_idx == (5, 10)


def test_extract_nearest_rotated_pole_rejects_1d_latlon():
    da = xr.DataArray(
        np.zeros((3, 3)),
        dims=("rlat", "rlon"),
        coords={"lat": ("rlat", [20.0, 21.0, 22.0]), "lon": ("rlon", [105.0, 106.0, 107.0])},
    )
    ref_points = xr.Dataset(coords={"station": ["A"], "lat": ("station", [20.0]), "lon": ("station", [105.0])})
    with pytest.raises(ValueError, match="needs a 2-D"):
        extract_nearest_rotated_pole(da, ref_points, lat_dim="lat", lon_dim="lon")
