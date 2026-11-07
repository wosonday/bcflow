"""Tests for evaluate.py: distribution-based and time-paired before/after metrics."""

from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr

from bcflow.evaluate import evaluate, obs_on_model_dates, paired_series, summarize

rng = np.random.default_rng(0)


def _series(values, time=None) -> xr.DataArray:
    """Two identical stations A and B, daily from 2000-01-01 unless `time` is given."""
    values = np.asarray(values, dtype=float)
    time = pd.date_range("2000-01-01", periods=len(values)) if time is None else time
    return xr.DataArray(
        np.column_stack([values, values]),
        dims=("time", "station"),
        coords={"time": time, "station": ["A", "B"]},
    )


def _metric(table: pd.DataFrame, name: str) -> pd.DataFrame:
    return table[table["metric"] == name]


def test_removing_a_shift_is_reported_as_improved_regardless_of_timing():
    obs_values = rng.normal(10, 2, 2000)
    obs = _series(obs_values)
    # Same distribution, shifted by +5, other timing.
    raw = _series(rng.permutation(obs_values) + 5)
    corrected = _series(rng.permutation(obs_values))  # same distribution, other timing

    table = evaluate(obs, raw, corrected)
    mean_bias = table[table["metric"] == "mean_bias"]
    np.testing.assert_allclose(mean_bias["raw"], 5, atol=1e-9)
    np.testing.assert_allclose(mean_bias["corrected"], 0, atol=1e-9)
    assert bool(mean_bias["improved"].all())

    # Distribution metrics ignore day-by-day timing: a permuted copy is perfect.
    qq = table[table["metric"] == "qq_rmse"]
    np.testing.assert_allclose(qq["corrected"], 0, atol=1e-9)
    assert set(table.columns) == {
        "period",
        "station",
        "metric",
        "ideal",
        "raw",
        "corrected",
        "improved",
    }


def test_wet_day_frequency_only_with_a_threshold():
    obs = _series(np.r_[np.zeros(50), np.full(50, 5.0)])  # 50 % wet days
    # 90 % wet days (drizzle problem).
    raw = _series(np.r_[np.zeros(10), np.full(90, 5.0)])

    assert "wet_day_freq_diff" not in set(evaluate(obs, raw, obs)["metric"])
    table = evaluate(obs, raw, obs, wet_thresh=1.0)
    wet = table[table["metric"] == "wet_day_freq_diff"]
    np.testing.assert_allclose(wet["raw"], 0.4)
    np.testing.assert_allclose(wet["corrected"], 0.0)


def test_summary_warns_when_bias_grows():
    obs = _series(rng.normal(10, 2, 500))
    table = evaluate(obs, raw=obs, corrected=obs + 1)  # a perfect raw model made worse
    qa = []
    summarize(table, qa, name="tas", label="validation", period=("2013", "2020"))
    assert [(c.name, c.status) for c in qa] == [("evaluation:tas:validation", "warn")]


def test_summary_passes_and_reports_both_rmses():
    obs = _series(rng.normal(10, 2, 500))
    table = evaluate(obs, raw=obs + 3, corrected=obs)
    qa = []
    summarize(table, qa, name="tas", label="calibration", period=("2000", "2012"))
    assert [(c.name, c.status) for c in qa] == [("evaluation:tas:calibration", "pass")]
    assert all(s in qa[0].message for s in ("2000..2012", "qq_rmse", "rmse"))


def test_paired_series_labels_each_day_with_its_period():
    obs = _series([1.0, 2.0, 3.0])
    ds = paired_series(obs, raw=obs + 1, corrected=obs, period="calibration")
    assert set(ds.data_vars) == {"obs", "raw", "corrected"}
    assert list(ds["period"].values) == ["calibration"] * 3


def test_paired_rmse_is_zero_after_removing_a_same_day_shift():
    obs_values = rng.normal(10, 2, 2000)
    obs = _series(obs_values)
    table = evaluate(obs, raw=_series(obs_values + 5), corrected=obs)
    rmse = _metric(table, "rmse")
    np.testing.assert_allclose(rmse["raw"], 5, atol=1e-9)
    np.testing.assert_allclose(rmse["corrected"], 0, atol=1e-9)
    assert bool(rmse["improved"].all())


def test_paired_rmse_sees_timing_errors_that_distribution_metrics_ignore():
    """The same values on other days: a perfect distribution, a poor day-by-day match."""
    obs_values = rng.normal(10, 2, 2000)
    obs = _series(obs_values)
    shuffled = _series(rng.permutation(obs_values))
    table = evaluate(obs, raw=shuffled, corrected=shuffled)
    np.testing.assert_allclose(_metric(table, "qq_rmse")["corrected"], 0, atol=1e-9)
    # Two independent draws of N(10, 2): expected RMSE = sqrt(2) * 2 ~ 2.83.
    np.testing.assert_allclose(
        _metric(table, "rmse")["corrected"], 2 * np.sqrt(2), rtol=0.1
    )


def test_paired_rmse_matches_dates_across_calendars():
    """A 'noleap' model has no 29 Feb: that station day is skipped, not shifted."""
    t_obs = pd.date_range("2000-02-27", "2000-03-02")  # includes 29 Feb
    t_mod = xr.date_range(
        "2000-02-27", "2000-03-02", calendar="noleap", use_cftime=True
    )
    obs = _series([1.0, 2.0, 99.0, 3.0, 4.0], time=t_obs)  # 99 falls on 29 Feb
    model = _series([1.0, 2.0, 3.0, 4.0], time=t_mod)

    np.testing.assert_allclose(
        obs_on_model_dates(obs, model).sel(station="A").values, [1, 2, 3, 4]
    )
    table = evaluate(obs, raw=model, corrected=model)
    np.testing.assert_allclose(_metric(table, "rmse")["raw"], 0, atol=1e-12)
