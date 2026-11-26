"""Numerical and pipeline tests for bcflow-native grouped adjustments."""

from __future__ import annotations

import pickle
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from scipy import stats

from bcflow.cell import CellPlan, run_cell
from bcflow.native import ParametricQuantileMapping, PowerTransformation, VarianceScaling
from bcflow.variables import DEFAULTS


def _series(values: np.ndarray, *, units: str = "degC") -> xr.DataArray:
    return xr.DataArray(
        values.astype(float), dims=("time",),
        coords={"time": pd.date_range("2000-01-01", periods=len(values))},
        attrs={"units": units},
    )


@pytest.mark.parametrize("cls", [VarianceScaling, ParametricQuantileMapping])
def test_temperature_mean_and_variance_are_corrected(cls):
    z = np.random.default_rng(12).standard_normal(730)
    ref, hist = _series(10 + 2 * z), _series(13 + 4 * z)
    scen = cls.train(ref, hist, kind="+").adjust(hist)
    assert abs(float(scen.mean()) - float(ref.mean())) < 1e-8
    assert abs(float(scen.std()) - float(ref.std())) < 1e-8


def test_variance_scaling_multiplicative_uses_linear_mean_scaling():
    hist = _series(np.arange(1, 366), units="mm d-1")
    ref = _series(2 * hist.values, units="mm d-1")
    scen = VarianceScaling.train(ref, hist, kind="*").adjust(hist)
    np.testing.assert_allclose(scen, ref)


def test_gamma_quantile_mapping_corrects_wet_distribution_and_dry_days():
    q = (np.arange(1, 801) - 0.5) / 800
    hist_values = np.r_[np.zeros(100), stats.gamma.ppf(q, a=2, scale=3)]
    ref_values = np.r_[np.zeros(100), stats.gamma.ppf(q, a=4, scale=2)]
    hist = _series(hist_values, units="mm d-1")
    ref = _series(ref_values, units="mm d-1")
    scen = ParametricQuantileMapping.train(
        ref, hist, kind="*", thresh="0 mm d-1"
    ).adjust(hist)
    assert bool((scen[:100] == 0).all())
    assert abs(float(scen.mean()) - float(ref.mean())) / float(ref.mean()) < 0.03
    assert abs(float(scen.std()) - float(ref.std())) / float(ref.std()) < 0.05


def test_power_transformation_matches_mean_and_wet_cv():
    hist_values = np.r_[np.zeros(50), np.linspace(0.2, 15, 500)]
    ref_values = 1.7 * hist_values**1.4
    hist = _series(hist_values, units="mm d-1")
    ref = _series(ref_values, units="mm d-1")
    scen = PowerTransformation.train(ref, hist, kind="*").adjust(hist)
    assert abs(float(scen.mean()) - float(ref.mean())) < 1e-6
    wet = lambda values: np.std(values[values > 0]) / np.mean(values[values > 0])
    assert abs(wet(scen.values) - wet(ref.values)) < 1e-6


def test_power_transformation_rejects_additive_kind():
    hist = _series(np.arange(1, 21), units="mm d-1")
    with pytest.raises(ValueError, match="requires kind='\\*'"):
        PowerTransformation.train(hist, hist, kind="+")


@pytest.mark.parametrize("cls,kind", [
    (VarianceScaling, "+"),
    (ParametricQuantileMapping, "+"),
    (PowerTransformation, "*"),
])
def test_month_group_and_pickle_round_trip(cls, kind):
    rng = np.random.default_rng(5)
    time = pd.date_range("2000-01-01", periods=731)
    base = rng.uniform(1, 4, len(time)) if kind == "*" else rng.normal(size=len(time))
    month = time.month.to_numpy()
    hist_values = base * (2 + month / 10) if kind == "*" else base * 3 + month
    ref_values = base**1.25 * 2 if kind == "*" else base * 1.5 + month / 4
    hist, ref = _series(hist_values), _series(ref_values)
    trained = cls.train(ref, hist, group="time.month", kind=kind)
    scen = trained.adjust(hist, interp="nearest")
    restored = pickle.loads(pickle.dumps(trained))
    xr.testing.assert_allclose(scen, restored.adjust(hist, interp="nearest"))
    xr.testing.assert_equal(trained.ds, restored.ds)
    for index in (1, 7, 12):
        mask = month == index
        assert abs(float(scen.values[mask].mean()) - float(ref_values[mask].mean())) < 0.1


@pytest.mark.parametrize("method,variable", [
    ("variance_scaling", "tmax"),
    ("parametric_quantile_mapping", "tmax"),
    ("power_transformation", "precip"),
])
def test_run_cell_two_stations(method, variable):
    rng = np.random.default_rng(19)
    time = pd.date_range("2000-01-01", periods=365)
    base = rng.uniform(0.5, 5, (365, 2)) if variable == "precip" else rng.normal(size=(365, 2))
    hist_values = base * 2 if variable == "precip" else 13 + 4 * base
    ref_values = base**1.3 * 1.4 if variable == "precip" else 10 + 2 * base
    spec = replace(DEFAULTS[variable], adapt_freq=False, jitter_under_thresh=None)
    def dataset(values):
        da = xr.DataArray(values, dims=("time", "station"),
                          coords={"time": time, "station": ["s1", "s2"]},
                          attrs={"units": spec.units})
        return xr.Dataset({variable: da})
    result = run_cell(
        CellPlan(target_name=variable, variables=(variable,), method=method, group="none"),
        ref=dataset(ref_values), hist=dataset(hist_values), sim=dataset(hist_values),
        specs={variable: spec}, units={variable: spec.units},
    )
    assert result.trained_ds is not None
    assert result.scen[variable].sizes == {"time": 365, "station": 2}
    np.testing.assert_allclose(result.scen[variable].mean("time"), ref_values.mean(axis=0), rtol=0.04)
