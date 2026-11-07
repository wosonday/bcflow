"""Tests for extract.py: nearest cell on regular and curvilinear grids,
longitude conventions, and the neighbour search for empty cells."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from bcflow.extract import extract_nearest
from bcflow.qa import QACheck

TIME = pd.date_range("2000-01-01", periods=3)


def _stations(lat, lon) -> xr.DataArray:
    ids = [f"S{k}" for k in range(len(lat))]
    return xr.DataArray(
        np.zeros((len(TIME), len(ids))),
        dims=("time", "station"),
        coords={"time": TIME, "station": ids, "lat": ("station", lat), "lon": ("station", lon)},
    )


def _regular(values: np.ndarray, lat, lon) -> xr.DataArray:
    """(time, lat, lon) field, the same 2-D values on every day."""
    data = np.broadcast_to(values, (len(TIME), *values.shape)).copy()
    return xr.DataArray(data, dims=("time", "lat", "lon"), coords={"time": TIME, "lat": lat, "lon": lon})


def test_regular_grid_picks_the_closest_cell():
    da = _regular(np.arange(9.0).reshape(3, 3), [20.0, 21.0, 22.0], [105.0, 106.0, 107.0])
    qa: list[QACheck] = []

    out = extract_nearest(da, _stations([20.1, 21.9], [106.9, 105.2]), qa, source_label="tmax:hist")

    assert out.dims == ("time", "station")
    assert out.sel(station="S0").values.tolist() == [2.0] * 3  # lat idx 0, lon idx 2
    assert out.sel(station="S1").values.tolist() == [6.0] * 3  # lat idx 2, lon idx 0
    assert "lat" not in out.coords and "lon" not in out.coords
    assert qa[-1].name == "extract:tmax:hist:nearest" and qa[-1].context["n_cells"] == 2


def test_0_360_grid_with_negative_station_longitude():
    da = _regular(np.arange(4.0).reshape(1, 4), [0.0], [0.0, 90.0, 180.0, 270.0])
    out = extract_nearest(da, _stations([0.0], [-88.0]), [], source_label="tmax:hist")
    assert float(out.isel(time=0, station=0)) == 3.0  # -88 -> 272 -> lon 270


def test_station_outside_the_grid_warns():
    da = _regular(np.zeros((2, 2)), [10.0, 11.0], [100.0, 101.0])
    qa: list[QACheck] = []
    extract_nearest(da, _stations([30.0], [100.0]), qa, source_label="tmax:hist")
    assert any(c.name == "extract:tmax:hist:outside_grid" and c.status == "warn" for c in qa)


def test_empty_nearest_cell_uses_the_closest_neighbour():
    values = np.array([[1.0, 2.0, 3.0], [4.0, np.nan, 6.0], [7.0, 8.0, 9.0]])
    da = _regular(values, [20.0, 21.0, 22.0], [105.0, 106.0, 107.0])
    qa: list[QACheck] = []

    # nearest is the NaN centre; of its neighbours, (21, 107) is the closest
    out = extract_nearest(da, _stations([21.0], [106.4]), qa, source_label="precip:hist", search_radius=1)

    assert out.isel(station=0).values.tolist() == [6.0] * 3
    warn = next(c for c in qa if c.name == "extract:precip:hist:neighbor_cell")
    assert warn.context["cells"]["S0"] == {"lat": 21.0, "lon": 107.0}


def test_no_data_within_radius_stays_empty_and_warns():
    values = np.full((3, 3), np.nan)
    values[0, 0] = 1.0
    da = _regular(values, [20.0, 21.0, 22.0], [105.0, 106.0, 107.0])
    qa: list[QACheck] = []

    out = extract_nearest(da, _stations([22.0], [107.0]), qa, source_label="precip:hist", search_radius=1)

    assert bool(out.isnull().all())
    assert any(c.name == "extract:precip:hist:no_data" for c in qa)


def test_search_radius_zero_disables_the_search():
    values = np.array([[np.nan, 2.0]])
    da = _regular(values, [20.0], [105.0, 106.0])
    out = extract_nearest(da, _stations([20.0], [105.0]), [], source_label="precip:hist", search_radius=0)
    assert bool(out.isnull().all())


def test_declared_and_aliased_coordinate_names():
    da = _regular(np.arange(4.0).reshape(2, 2), [20.0, 21.0], [105.0, 106.0])
    da = da.rename({"lat": "Latitude", "lon": "LONGITUDE"})
    out = extract_nearest(da, _stations([21.0], [106.0]), [], source_label="tmax:hist")
    assert float(out.isel(time=0, station=0)) == 3.0

    named = da.rename({"Latitude": "yc", "LONGITUDE": "xc"})
    out = extract_nearest(
        named, _stations([21.0], [106.0]), [], lat_name="yc", lon_name="xc", source_label="tmax:hist"
    )
    assert float(out.isel(time=0, station=0)) == 3.0


def test_missing_latlon_fails_clearly():
    da = _regular(np.zeros((2, 2)), [20.0, 21.0], [105.0, 106.0]).rename({"lat": "a", "lon": "b"})
    qa: list[QACheck] = []
    with pytest.raises(ValueError, match="lat/lon"):
        extract_nearest(da, _stations([20.0], [105.0]), qa, source_label="tmax:hist")
    assert qa[-1].name == "grid:tmax:hist:latlon_not_found"


def test_curvilinear_grid_matches_brute_force_search():
    """2-D lat/lon NOT aligned with the native (y, x) dims, cross-checked
    against an independent brute-force search over the whole grid."""
    y, x = np.linspace(-10, 10, 21), np.linspace(-15, 15, 31)
    X, Y = np.meshgrid(x, y)
    theta = np.radians(20)
    true_lat = 10 + Y * np.cos(theta) - X * np.sin(theta) * 0.3
    true_lon = 105 + X * np.cos(theta) + Y * np.sin(theta) * 0.3
    values = np.arange(21 * 31, dtype=float).reshape(21, 31)
    da = xr.DataArray(
        np.broadcast_to(values, (len(TIME), 21, 31)).copy(),
        dims=("time", "y", "x"),
        coords={"time": TIME, "lat": (("y", "x"), true_lat), "lon": (("y", "x"), true_lon)},
    )
    slat = [true_lat[5, 10] + 0.001, true_lat[15, 20]]
    slon = [true_lon[5, 10] + 0.001, true_lon[15, 20]]

    out = extract_nearest(da, _stations(slat, slon), [], source_label="tmax:hist")

    assert float(out.isel(time=0, station=0)) == values[5, 10]
    assert float(out.isel(time=0, station=1)) == values[15, 20]
    d = (true_lat - slat[0]) ** 2 + (true_lon - slon[0]) ** 2
    assert np.unravel_index(np.argmin(d), d.shape) == (5, 10)
