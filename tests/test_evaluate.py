"""Tests for evaluate.py: distribution-based before/after metrics."""

from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr

from bcflow.evaluate import evaluate, summarize

rng = np.random.default_rng(0)


def _series(values) -> xr.DataArray:
    """Two identical stations A and B, daily from 2000-01-01."""
    values = np.asarray(values, dtype=float)
    return xr.DataArray(
        np.column_stack([values, values]),
        dims=("time", "station"),
        coords={"time": pd.date_range("2000-01-01", periods=len(values)), "station": ["A", "B"]},
    )


def test_removing_a_shift_is_reported_as_improved_regardless_of_timing():
    obs_values = rng.normal(10, 2, 2000)
    obs = _series(obs_values)
    raw = _series(rng.permutation(obs_values) + 5)   # same distribution, shifted, other timing
    corrected = _series(rng.permutation(obs_values))  # same distribution, other timing

    table = evaluate(obs, raw, corrected)
    mean_bias = table[table["metric"] == "mean_bias"]
    np.testing.assert_allclose(mean_bias["raw"], 5, atol=1e-9)
    np.testing.assert_allclose(mean_bias["corrected"], 0, atol=1e-9)
    assert mean_bias["improved"].all()

    # Distribution metrics ignore day-by-day timing: a permuted copy is perfect.
    qq = table[table["metric"] == "qq_rmse"]
    np.testing.assert_allclose(qq["corrected"], 0, atol=1e-9)
    assert set(table.columns) == {"station", "metric", "ideal", "raw", "corrected", "improved"}


def test_wet_day_frequency_only_with_a_threshold():
    obs = _series(np.r_[np.zeros(50), np.full(50, 5.0)])  # 50 % wet days
    raw = _series(np.r_[np.zeros(10), np.full(90, 5.0)])  # 90 % wet days (drizzle problem)

    assert "wet_day_freq_diff" not in set(evaluate(obs, raw, obs)["metric"])
    table = evaluate(obs, raw, obs, wet_thresh=1.0)
    wet = table[table["metric"] == "wet_day_freq_diff"]
    np.testing.assert_allclose(wet["raw"], 0.4)
    np.testing.assert_allclose(wet["corrected"], 0.0)


def test_summary_warns_when_in_sample_and_when_bias_grows():
    obs = _series(rng.normal(10, 2, 500))
    table = evaluate(obs, raw=obs, corrected=obs + 1)  # a perfect raw model made worse
    qa = []
    summarize(table, qa, name="tas", period=None, in_sample=True)
    status = {c.name: c.status for c in qa}
    assert status["evaluation:tas:in_sample"] == "warn"
    assert status["evaluation:tas:mean_bias_not_reduced"] == "warn"


def test_summary_passes_when_bias_is_reduced_everywhere():
    obs = _series(rng.normal(10, 2, 500))
    table = evaluate(obs, raw=obs + 3, corrected=obs)
    qa = []
    summarize(table, qa, name="tas", period=("2013", "2020"), in_sample=False)
    assert [(c.name, c.status) for c in qa] == [("evaluation:tas:summary", "pass")]
    assert "2013..2020" in qa[0].message
