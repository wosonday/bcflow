"""Tests for calibration/validation periods: schema rules and their effect on a run."""

from __future__ import annotations

import copy
from datetime import date

import pytest
from pydantic import ValidationError

from bcflow.config_schema import HistSimSourceConfig, PeriodsConfig
from bcflow.pipeline import run_pipeline


def test_overlapping_periods_rejected():
    with pytest.raises(ValidationError, match="overlap"):
        PeriodsConfig(calibration=("2000", "2010"), validation=("2010-06-01", "2015"))


def test_validation_requires_calibration():
    with pytest.raises(ValidationError, match="requires"):
        PeriodsConfig(validation=("2015", "2020"))


def test_start_after_end_rejected():
    with pytest.raises(ValidationError, match="after"):
        PeriodsConfig(calibration=("2010", "2000"))


def test_invalid_date_rejected():
    with pytest.raises(ValidationError, match="invalid date"):
        PeriodsConfig(calibration=("2000-13-01", "2010"))


def test_unquoted_yaml_years_and_dates_are_accepted():
    """YAML reads `2000` as an int and `2012-12-31` as a date; a non-coder
    should not have to know that and quote them."""
    p = PeriodsConfig.model_validate({"calibration": [2000, date(2012, 12, 31)]})
    assert p.calibration == ("2000", "2012-12-31")
    src = HistSimSourceConfig.model_validate(
        {
            "format": "netcdf",
            "path": "x.nc",
            "hist_period": [1980, 2014],
            "sim_period": [2015, 2100],
        }
    )
    assert src.hist_period == ("1980", "2014")


def test_out_of_sample_evaluation_with_validation_period(toy_config):
    cfg = copy.deepcopy(toy_config)
    for v in cfg["variables"]:
        v["periods"] = {"calibration": ["2000", "2012"], "validation": ["2013", "2020"]}

    out = run_pipeline(cfg)

    for name in ("tasmax", "pr"):
        res = out["results"][name]
        table = res["evaluation"]
        assert {"mean_bias", "sd_ratio", "qq_rmse", "rmse"} <= set(table["metric"])
        assert set(table["period"]) == {"calibration", "validation"}
        # The correction is applied to hist over BOTH periods, each day labelled.
        hist_eval = res["hist_eval"]
        assert set(hist_eval["period"].values) == {"calibration", "validation"}
        years = hist_eval["time"].dt.year
        assert int(years.min()) == 2000 and int(years.max()) == 2020
        assert {f"evaluation:{name}:calibration", f"evaluation:{name}:validation"} <= {
            c.name for c in out["qa"]
        }
    assert "wet_day_freq_diff" in set(out["results"]["pr"]["evaluation"]["metric"])
    assert not any(c.name.endswith(":in_sample") for c in out["qa"])
    done = next(c for c in out["qa"] if c.name == "train_adjust:tasmax:done")
    assert done.context["calibration"]["start"].startswith("2000")
    assert done.context["calibration"]["end"].startswith("2012")


def test_without_periods_evaluation_is_in_sample_and_flagged(toy_config):
    out = run_pipeline(toy_config)
    assert set(out["results"]["tasmax"]["evaluation"]["period"]) == {"calibration"}
    assert any(
        c.name == "evaluation:tasmax:in_sample" and c.status == "warn"
        for c in out["qa"]
    )


def test_calibration_period_outside_data_fails_that_variable_only(toy_config):
    cfg = copy.deepcopy(toy_config)
    cfg["variables"][0]["periods"] = {"calibration": ["1950", "1960"]}

    out = run_pipeline(cfg)

    assert out["results"]["tasmax"] is None
    assert out["results"]["pr"] is not None
    assert any(
        c.name == "periods:tasmax:ref:calibration_empty" and c.status == "fail"
        for c in out["qa"]
    )
