"""Tests for regrid.py: each method checked against fields with a known answer."""

from __future__ import annotations

from typing import get_args

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from bcflow.config_schema import RegridMethod
from bcflow.qa import QACheck
from bcflow.regrid import METHODS, TargetGrid, regrid_to_points

TIME = pd.date_range("2000-01-01", periods=2)
# a coarse global model grid, 0..360, and a fine regional target in -180..180
SRC_LAT = np.linspace(-88.6, 88.6, 128)
SRC_LON = np.arange(0, 360, 1.40625)


def _source(field: np.ndarray, lat=SRC_LAT, lon=SRC_LON) -> xr.DataArray:
    return xr.DataArray(
        np.broadcast_to(field, (TIME.size, *field.shape)).copy(),
        dims=("time", "lat", "lon"),
        coords={"time": TIME, "lat": lat, "lon": lon},
        attrs={"units": "mm d-1"},
    )


def _target(lat: np.ndarray, lon: np.ndarray) -> TargetGrid:
    iy, ix = np.meshgrid(np.arange(lat.size), np.arange(lon.size), indexing="ij")
    return TargetGrid(lat, lon, iy.ravel(), ix.ravel())


FINE = _target(np.arange(8.05, 24, 0.1), np.arange(100.05, 112, 0.1))


def test_method_names_match_the_schema():
    assert set(get_args(RegridMethod)) == set(METHODS)


@pytest.mark.parametrize("method", METHODS)
def test_constant_field_is_kept_by_every_method(method):
    out = regrid_to_points(
        _source(np.full((128, 256), 5.0)),
        FINE,
        [],
        method=method,
        source_label="pr:hist",
    )
    np.testing.assert_allclose(out.values, 5.0, atol=1e-9)
    assert out.dims == ("time", "station") and out.sizes["station"] == FINE.size


@pytest.mark.parametrize("method", ["bilinear", "bicubic", "patch"])
def test_linear_field_is_exact_for_interpolating_methods(method):
    la, lo = np.meshgrid(SRC_LAT, SRC_LON, indexing="ij")
    out = regrid_to_points(
        _source(2 * la + 0.5 * lo), FINE, [], method=method, source_label="pr:hist"
    )
    truth = 2 * FINE.point_lat + 0.5 * FINE.point_lon
    np.testing.assert_allclose(out.isel(time=0).values, truth, atol=1e-8)


def test_nearest_takes_the_closest_source_value():
    la, lo = np.meshgrid(SRC_LAT, SRC_LON, indexing="ij")
    got_lat = regrid_to_points(
        _source(la), FINE, [], method="nearest", source_label="pr:hist"
    )
    got_lon = regrid_to_points(
        _source(lo), FINE, [], method="nearest", source_label="pr:hist"
    )
    # brute force: the closest source centre along each axis
    want_lat = SRC_LAT[
        np.abs(SRC_LAT[None, :] - FINE.point_lat[:, None]).argmin(axis=1)
    ]
    want_lon = SRC_LON[
        np.abs(SRC_LON[None, :] - FINE.point_lon[:, None]).argmin(axis=1)
    ]
    np.testing.assert_allclose(got_lat.isel(time=0).values, want_lat)
    np.testing.assert_allclose(got_lon.isel(time=0).values, want_lon)


def test_idw_lies_between_its_neighbours_and_hits_exact_points():
    la, lo = np.meshgrid(SRC_LAT, SRC_LON, indexing="ij")
    field = np.sin(np.radians(la)) * 10 + lo / 100
    on_centre = _target(SRC_LAT[60:62], SRC_LON[70:72])
    out = regrid_to_points(
        _source(field), on_centre, [], method="idw", source_label="pr:hist"
    )
    np.testing.assert_allclose(
        out.isel(time=0).values, field[60:62, 70:72].ravel(), atol=1e-9
    )


def test_conservative_keeps_the_area_mean_from_fine_to_coarse():
    fine_lat, fine_lon = np.arange(10.05, 20, 0.1), np.arange(100.05, 110, 0.1)
    rain = np.random.default_rng(0).gamma(1.0, 5.0, (fine_lat.size, fine_lon.size))
    coarse = _target(np.arange(10.5, 20, 1.0), np.arange(100.5, 110, 1.0))

    out = regrid_to_points(
        _source(rain, fine_lat, fine_lon),
        coarse,
        [],
        method="conservative",
        source_label="pr:hist",
    )

    area_fine = np.cos(np.radians(fine_lat))[:, None] * np.ones(fine_lon.size)
    area_coarse = np.cos(np.radians(coarse.point_lat))
    mean_fine = (rain * area_fine).sum() / area_fine.sum()
    mean_coarse = (out.isel(time=0).values * area_coarse).sum() / area_coarse.sum()
    assert mean_coarse == pytest.approx(mean_fine, rel=1e-3)


def test_missing_source_cells_are_skipped_not_spread():
    field = np.full((128, 256), 3.0)
    field[70:74, 70:80] = np.nan  # a hole in the model (e.g. a land-sea mask)
    target = _target(np.array([SRC_LAT[71] + 0.3]), np.array([SRC_LON[75] + 0.3]))
    qa: list[QACheck] = []

    out = regrid_to_points(
        _source(field), target, qa, method="bilinear", source_label="pr:hist"
    )

    assert bool(out.isnull().all())  # inside the hole: no value invented
    assert qa[-1].status == "warn" and qa[-1].context["empty"] == 1


def test_target_longitudes_west_of_greenwich_match_a_0_360_grid():
    la, lo = np.meshgrid(SRC_LAT, SRC_LON, indexing="ij")
    target = _target(np.array([0.0]), np.array([-60.0]))  # = 300 E
    out = regrid_to_points(
        _source(lo + 0 * la), target, [], method="bilinear", source_label="pr:hist"
    )
    assert float(out.isel(time=0, station=0)) == pytest.approx(300.0)


def test_unknown_method_is_rejected():
    with pytest.raises(ValueError, match="Unknown regrid method"):
        regrid_to_points(
            _source(np.zeros((128, 256))),
            FINE,
            [],
            method="spline",
            source_label="pr:hist",
        )
