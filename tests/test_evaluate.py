"""Tests for evaluate.py: distribution-based and time-paired before/after metrics."""

from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr
from time import perf_counter

from bcflow.evaluate import change_signal, evaluate, obs_on_model_dates, paired_series, summarize

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


def test_annual_cycle_lag1_and_ks_have_hand_computed_values():
    time = pd.date_range("2001-01-01", periods=365)
    obs = _series(np.tile([0.0, 1.0], 183)[:365], time=time)
    raw = obs + 2
    table = evaluate(obs, raw, obs)
    for name, expected in (("annual_cycle_rmse", 2), ("lag1_autocorr_diff", 0), ("ks_stat", 1)):
        np.testing.assert_allclose(_metric(table, name)["raw"], expected, atol=1e-12)
        np.testing.assert_allclose(_metric(table, name)["corrected"], 0, atol=1e-12)

    short_obs = _series([0, 1, 0, 1])
    short_model = _series([0, 0, 1, 1])
    short_table = evaluate(short_obs, short_model, short_obs)
    np.testing.assert_allclose(_metric(short_table, "ks_stat")["raw"], 0)
    np.testing.assert_allclose(_metric(short_table, "lag1_autocorr_diff")["raw"], 1.5)
    assert _metric(short_table, "annual_cycle_rmse")["raw"].isna().all()


def test_ks_handles_ties_missing_values_and_two_stations():
    time = pd.date_range("2001-01-01", periods=5)
    obs = xr.DataArray(
        [[0, 1], [0, 2], [1, 3], [1, np.nan], [np.nan, np.nan]],
        dims=("time", "station"), coords={"time": time, "station": ["A", "B"]},
    )
    model = xr.DataArray(
        [[0, 1], [1, 1], [1, 1], [1, np.nan], [np.nan, np.nan]],
        dims=obs.dims, coords=obs.coords,
    )
    scores = _metric(evaluate(obs, model, obs), "ks_stat").set_index("station")
    np.testing.assert_allclose(scores.loc["A", "raw"], 0.25)
    np.testing.assert_allclose(scores.loc["B", "raw"], 2 / 3)


def test_precipitation_extremes_spells_and_360_day_calendar():
    time = xr.date_range("2001-01-01", periods=360, calendar="360_day", use_cftime=True)
    obs_values = np.zeros(360)
    obs_values[:4] = [10, 20, 30, 40]
    obs = _series(obs_values, time=time)
    model_values = obs_values.copy()
    model_values[3] = 50
    table = evaluate(obs, _series(model_values, time=time), obs, wet_thresh=1)
    for name in ("rx1day_bias", "r95p_bias"):
        np.testing.assert_allclose(_metric(table, name)["raw"], 10)
        np.testing.assert_allclose(_metric(table, name)["corrected"], 0)

    alternating = np.tile([0.0, 2.0], 180)
    changed = alternating.copy()
    changed[:3] = 0
    changed[10:12] = 2
    changed[20] = np.nan
    spell_table = evaluate(
        _series(alternating, time=time), _series(changed, time=time),
        _series(alternating, time=time), wet_thresh=1,
    )
    np.testing.assert_allclose(_metric(spell_table, "cdd_bias")["raw"], 2)
    np.testing.assert_allclose(_metric(spell_table, "cwd_bias")["raw"], 2)


def test_short_periods_and_missing_series_return_nan_for_annual_metrics():
    obs = _series(np.ones(100))
    table = evaluate(obs, obs, obs, wet_thresh=1)
    for name in ("annual_cycle_rmse", "rx1day_bias", "r95p_bias", "cdd_bias", "cwd_bias"):
        assert _metric(table, name)["raw"].isna().all()
    one = _series([1])
    one_table = evaluate(one, one, one)
    assert _metric(one_table, "lag1_autocorr_diff")["raw"].isna().all()
    assert _metric(one_table, "ks_stat")["raw"].isna().all()


def test_noleap_year_and_nan_gap_breaks_dry_spell():
    time = xr.date_range("2001-01-01", periods=365, calendar="noleap", use_cftime=True)
    obs_values = np.full(365, 2.0)
    obs_values[:2] = 0
    model_values = obs_values.copy()
    model_values[2:5] = [np.nan, 0, 0]
    table = evaluate(
        _series(obs_values, time=time), _series(model_values, time=time),
        _series(obs_values, time=time), wet_thresh=1,
    )
    np.testing.assert_allclose(_metric(table, "cdd_bias")["raw"], 0)
    np.testing.assert_allclose(_metric(table, "rx1day_bias")["raw"], 0)


def test_change_signal_additive_and_multiplicative():
    hist = _series([2, 4])
    sim = _series([6, 8])
    scen_hist = _series([3, 5])
    scen_sim = _series([9, 11])
    additive = change_signal(hist, sim, scen_hist, scen_sim, "+")
    multiplicative = change_signal(hist, sim, scen_hist, scen_sim, "*")
    for field, expected in (("raw", 4), ("corrected", 6), ("difference", 2)):
        np.testing.assert_allclose(additive[field], expected)
    np.testing.assert_allclose(multiplicative["raw"], 7 / 3)
    np.testing.assert_allclose(multiplicative["corrected"], 2.5)
    np.testing.assert_allclose(multiplicative["difference"], 1 / 6)


def test_ks_station_blocks_timing_sanity():
    time = pd.date_range("2001-01-01", periods=720)
    values = rng.normal(size=(720, 128))
    obs = xr.DataArray(values, dims=("time", "station"), coords={"time": time, "station": np.arange(128)})
    start = perf_counter()
    table = evaluate(obs, obs + 0.5, obs)
    assert perf_counter() - start < 20
    assert len(_metric(table, "ks_stat")) == 128
