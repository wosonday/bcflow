"""Tests for units.py: conversion to the declared units, with QA on every outcome."""

from __future__ import annotations

import copy

import numpy as np
import pytest
import xarray as xr

from bcflow.pipeline import run_pipeline
from bcflow.units import harmonize, to_units


def _da(values, units):
    attrs = {} if units is None else {"units": units}
    return xr.DataArray(np.asarray(values, dtype=float), dims="time", attrs=attrs)


def test_kelvin_to_celsius():
    qa = []
    out, converted_from = to_units(_da([273.15, 300.0], "K"), "degC", qa, label="tas:hist")
    np.testing.assert_allclose(out.values, [0.0, 26.85])
    assert converted_from == "K"
    assert qa == []


def test_precip_mass_flux_to_depth_rate_uses_water_density():
    """1 kg m-2 s-1 of water = 1 mm s-1 = 86400 mm d-1."""
    out, _ = to_units(_da([1 / 86400, 2 / 86400], "kg m-2 s-1"), "mm d-1", [], label="pr:hist")
    np.testing.assert_allclose(out.values, [1.0, 2.0])
    assert out.attrs["units"] == "mm d-1"


def test_equivalent_spellings_are_not_a_conversion():
    _, converted_from = to_units(_da([1.0], "mm/day"), "mm d-1", [], label="pr:sim")
    assert converted_from is None


def test_missing_units_warns_and_assumes_target():
    qa = []
    out, converted_from = to_units(_da([1.0], None), "mm/day", qa, label="pr:sim")
    assert out.attrs["units"] == "mm/day" and converted_from is None
    assert [(c.name, c.status) for c in qa] == [("units:pr:sim:missing", "warn")]


@pytest.mark.parametrize("src", ["K", "not_a_unit"])
def test_incompatible_or_unknown_units_fail_clearly(src):
    qa = []
    with pytest.raises(ValueError, match="Cannot convert"):
        to_units(_da([300.0], src), "mm/day", qa, label="pr:hist")
    assert (qa[-1].name, qa[-1].status) == ("units:pr:hist:incompatible", "fail")


def test_harmonize_reports_all_conversions_in_one_check():
    qa = []
    ref, hist, sim, units = harmonize(_da([20.0], "degC"), _da([293.15], "K"), _da([293.15], "K"), "degC", qa, name="tas")
    np.testing.assert_allclose([hist.item(), sim.item()], [20.0, 20.0])
    assert units == "degC"
    summary = next(c for c in qa if c.name == "units:tas:summary")
    assert summary.context["converted"] == {"hist": "K", "sim": "K"}


def test_harmonize_without_declared_units_uses_hist_units_and_warns():
    qa = []
    ref, *_, units = harmonize(_da([1.0], ""), _da([1.0], "mm/day"), _da([1.0], "mm/day"), "", qa, name="pr")
    assert units == "mm/day" and ref.attrs["units"] == "mm/day"
    assert any(c.name == "units:pr:undeclared" and c.status == "warn" for c in qa)


def test_pipeline_corrects_kelvin_model_data_like_celsius(toy_config, toy_paths):
    """Same data, model side stored in K: the correction must not change."""
    cfg = copy.deepcopy(toy_config)
    cfg["variables"] = [cfg["variables"][0]]  # tasmax only
    baseline = run_pipeline(cfg)["results"]["tasmax"]["scen"]

    for key in ("tasmax_hist", "tasmax_sim"):
        with xr.open_dataset(toy_paths[key]) as ds:
            ds = ds.load()
        ds["tasmax"] = (ds["tasmax"] + 273.15).assign_attrs(units="K")
        ds.to_netcdf(toy_paths[key])

    out = run_pipeline(cfg)
    np.testing.assert_allclose(out["results"]["tasmax"]["scen"].values, baseline.values, atol=1e-6)
    summary = next(c for c in out["qa"] if c.name == "units:tasmax:summary")
    assert summary.context["converted"] == {"hist": "K", "sim": "K"}
