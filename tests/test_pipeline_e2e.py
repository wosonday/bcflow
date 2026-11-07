"""End-to-end run_pipeline() on the toy data: real train+adjust through xsdba."""

from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from bcflow.adjust import run_variable
from bcflow.config_schema import load_config
from bcflow.extract import extract_nearest
from bcflow.loaders import load_model_variable, load_station_variable
from bcflow.pipeline import run_pipeline


def test_both_variables_succeed(toy_config):
    out = run_pipeline(toy_config)

    assert all(c.status != "fail" for c in out["qa"]), [c.as_dict() for c in out["qa"] if c.status == "fail"]
    assert out["results"]["tmax"] is not None
    assert out["results"]["precip"] is not None


def test_tmax_adjustment_moves_mean_toward_ref(toy_config):
    """Sanity check: after correction the mean of scen is close to the stations'."""
    cfg = load_config(toy_config)
    qa = []
    ref = load_station_variable(cfg.stations, "tmax", "degC", qa)
    scen = run_variable(cfg, "tmax", qa)["scen"]

    # make_toy_data.py biases hist/sim by +1.8/+2.1 degC on purpose.
    assert np.all(np.abs(scen.mean("time").values - ref.mean("time").values) < 0.5)


def test_precip_adapt_freq_reduces_dry_day_mismatch(toy_config):
    """adapt_freq must bring the share of dry days of scen closer to the stations'."""
    cfg = load_config(toy_config)
    qa = []
    ref = load_station_variable(cfg.stations, "precip", "mm d-1", qa)
    sim = extract_nearest(
        load_model_variable(cfg.model, "sim", "precip", qa), ref, qa, source_label="precip:sim"
    )
    scen = run_variable(cfg, "precip", qa)["scen"]

    wet_months = slice("2010-08-01", "2010-09-30")
    # Reduce by dimension NAME, never by axis position.
    dry_ref = (ref.sel(time=wet_months) == 0).mean("time")
    dry_sim = (sim.sel(time=wet_months) == 0).mean("time")
    dry_scen = (scen.sel(time=wet_months) == 0).mean("time")

    assert float(abs(dry_scen - dry_ref).mean()) <= float(abs(dry_sim - dry_ref).mean()) + 1e-9


def test_precip_is_never_negative(toy_config):
    out = run_pipeline(
        {**toy_config, "variables": ["precip"], "correction": {"precip": toy_config["correction"]["precip"]}}
    )
    assert float(out["results"]["precip"]["scen"].min()) >= 0.0


def test_defaults_run_without_any_override(toy_config):
    """No `correction:` block at all: QDM, monthly groups, adapt_freq at 1 mm/d."""
    cfg = {**toy_config, "variables": ["precip"]}
    del cfg["correction"]

    out = run_pipeline(cfg)

    assert out["results"]["precip"] is not None, [c.message for c in out["qa"] if c.status == "fail"]
    done = next(c for c in out["qa"] if c.name == "train_adjust:precip:done")
    assert done.context["method"] == "quantile_delta_mapping" and done.context["group"] == "month"


def test_default_precip_has_no_nan_or_inf_from_dry_days(toy_config):
    """Regression test: QDM's factor ref/hist is inf or NaN where hist is 0;
    without jittering the zeros, dry days became NaN and evaluation inf."""
    cfg = {**toy_config, "variables": ["precip"]}
    del cfg["correction"]

    out = run_pipeline(cfg)
    result = out["results"]["precip"]

    assert bool(np.isfinite(result["scen"]).all())
    assert bool(np.isfinite(result["hist_eval"]["corrected"]).all())
    assert np.isfinite(result["evaluation"].select_dtypes("number").to_numpy()).all()
    assert float((result["scen"] == 0).mean()) > 0.3  # dry days stay exactly 0
    assert not any(c.name == "adjust:precip:non_finite" for c in out["qa"])


@pytest.mark.filterwarnings("ignore:All-[Nn]a[Nn] slice:RuntimeWarning")  # the empty station, on purpose
def test_station_without_calibration_data_is_named(toy_config):
    """A station whose observations all fall outside calibration gives an
    all-NaN corrected series; the QA must name it."""
    path = Path(toy_config["stations"]["data_dir"]) / "ST02.csv"
    df = pd.read_csv(path)
    df.loc[pd.to_datetime(df["date"]) < "2013-01-01", "tmax"] = np.nan
    df.to_csv(path, index=False)
    cfg = {**toy_config, "variables": ["tmax"], "periods": {"calibration": ["2000", "2012"]}}
    del cfg["correction"]["precip"]

    out = run_pipeline(cfg)

    warn = next(c for c in out["qa"] if c.name == "adjust:tmax:non_finite")
    assert warn.status == "warn" and warn.context["stations"] == ["ST02"]


def test_random_steps_are_reproducible(toy_config):
    """Jitter and adapt_freq are random; two runs must still be identical."""
    cfg = {**toy_config, "variables": ["precip"]}
    del cfg["correction"]

    first = run_pipeline(cfg)["results"]["precip"]["scen"]
    second = run_pipeline(cfg)["results"]["precip"]["scen"]

    assert bool((first == second).all())


def test_one_bad_variable_does_not_break_the_others(toy_config):
    """No station has tmax -> only tmax fails, precip still runs."""
    bad = copy.deepcopy(toy_config)
    for f in Path(bad["stations"]["data_dir"]).glob("*.csv"):
        pd.read_csv(f).drop(columns=["tmax"]).to_csv(f, index=False)

    out = run_pipeline(bad)

    assert out["results"]["tmax"] is None
    assert out["results"]["precip"] is not None
    assert any(c.name == "stations:tmax:none_loaded" and c.status == "fail" for c in out["qa"])


def test_unknown_method_fails_only_that_variable(toy_config):
    bad = copy.deepcopy(toy_config)
    bad["correction"]["tmax"]["method"] = "no_such_method"

    out = run_pipeline(bad)

    assert out["results"]["tmax"] is None and out["results"]["precip"] is not None
    assert any(c.name == "method:tmax:unknown" for c in out["qa"])


def test_invalid_config_raises_before_running(toy_config):
    bad = copy.deepcopy(toy_config)
    bad["correction"]["tmax"]["adapt_freq"] = True  # no threshold for tmax
    with pytest.raises(ValidationError, match="adapt_freq_thresh"):
        run_pipeline(bad)
