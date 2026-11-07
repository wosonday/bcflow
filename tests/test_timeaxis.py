"""Tests for timeaxis.py: one daily time axis for station and model data."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from bcflow.pipeline import run_pipeline
from bcflow.timeaxis import align_daily


def _series(time, value=1.0) -> xr.DataArray:
    """One station 'A', constant value, on the given time axis."""
    return xr.DataArray(
        np.full((len(time), 1), value),
        dims=("time", "station"),
        coords={"time": time, "station": ["A"]},
    )


def test_midday_model_stamps_are_moved_to_midnight():
    """CNRM-style 12:00 stamps vs station 00:00: same days, identical axis after."""
    ref = _series(pd.date_range("2000-01-01", periods=5))
    hist = _series(pd.date_range("2000-01-01T12:00", periods=5))
    qa = []

    ref, hist, _ = align_daily(ref, hist, hist, qa, name="pr")

    assert (ref["time"].values == hist["time"].values).all()
    assert ref.sizes["time"] == 5
    aligned = next(c for c in qa if c.name == "time:pr:aligned")
    assert aligned.status == "pass" and "hist" in aligned.context["shifted"]


def test_noleap_model_drops_29_february_from_station_data():
    ref = _series(pd.date_range("2000-02-27", "2000-03-02"))  # 5 days, has 29 Feb
    hist = _series(
        xr.date_range("2000-02-27", "2000-03-02", calendar="noleap", use_cftime=True)
    )
    qa = []

    ref, hist, _ = align_daily(ref, hist, hist, qa, name="tas")

    assert ref.sizes["time"] == hist.sizes["time"] == 4
    assert (ref["time"].values == hist["time"].values).all()
    assert ref["time"].dt.calendar == "noleap"


def test_360_day_model_keeps_only_common_days():
    ref = _series(pd.date_range("2000-01-01", "2000-12-31"))  # 366 days
    hist = _series(
        xr.date_range("2000-01-01", "2000-12-30", calendar="360_day", use_cftime=True)
    )
    qa = []

    ref, hist, _ = align_daily(ref, hist, hist, qa, name="tas")

    assert (ref["time"].values == hist["time"].values).all()
    assert 300 < ref.sizes["time"] < 360  # 31st days and 30 Feb have no partner


def test_sub_daily_data_is_rejected_clearly():
    ref = _series(pd.date_range("2000-01-01", periods=4))
    hist = _series(pd.date_range("2000-01-01", periods=8, freq="12h"))
    qa = []

    with pytest.raises(ValueError, match="not daily"):
        align_daily(ref, hist, hist, qa, name="pr")
    assert qa[-1].name == "time:pr:hist:sub_daily" and qa[-1].status == "fail"


def test_no_common_days_is_rejected_clearly():
    ref = _series(pd.date_range("1990-01-01", periods=3))
    hist = _series(pd.date_range("2000-01-01", periods=3))
    qa = []

    with pytest.raises(ValueError, match="No common days"):
        align_daily(ref, hist, hist, qa, name="pr")
    assert qa[-1].name == "time:pr:no_common_days"


def test_pipeline_runs_with_midday_model_stamps(toy_config, toy_paths):
    """Regression test for the CNRM-ESM2-1 case: model stamps at 12:00 used to
    make xsdba fail with 'operands could not be broadcast together'."""
    for key in ("hist_pr", "sim_pr"):
        with xr.open_dataset(toy_paths[key]) as ds:
            ds = ds.load()
        ds = ds.assign_coords(time=ds.indexes["time"] + pd.Timedelta(hours=12))
        ds.to_netcdf(toy_paths[key])
    cfg = {**toy_config, "variables": ["precip"], "correction": {"precip": toy_config["correction"]["precip"]}}

    out = run_pipeline(cfg)

    assert out["results"]["precip"] is not None, [c.message for c in out["qa"] if c.status == "fail"]
    assert out["results"]["precip"]["scen"]["time"].dt.hour.max() == 0
