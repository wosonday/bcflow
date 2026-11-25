"""build_grouper (window/pool_dims) and the grouping sample-size QA."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bcflow.grouping import GROUPS, build_grouper, effective_samples, sample_size_qa
from bcflow.qa import QACheck
from bcflow.variables import VariableSpec


def _spec(**overrides) -> VariableSpec:
    base = {"name": "tmax", "units": "degC", "kind": "+"}
    base.update(overrides)
    return VariableSpec(**base)


# --- build_grouper: previous behaviour is unchanged -------------------------


def test_month_spec_matches_previous_grouper():
    spec = _spec(group="month", window=15)  # window was ignored for month before
    grouper = build_grouper(spec)
    assert grouper.name == "time.month"
    assert grouper.dim == "time" and grouper.prop == "month"
    assert grouper.window == 1
    assert grouper.add_dims == []


def test_dayofyear_spec_keeps_its_own_window():
    grouper = build_grouper(_spec(group="dayofyear", window=31))
    assert grouper.name == "time.dayofyear"
    assert grouper.window == 31


def test_none_spec_matches_previous_grouper():
    grouper = build_grouper(_spec(group="none"))
    assert grouper.name == "time"
    assert grouper.window == 1
    assert grouper.add_dims == []


def test_group_string_and_keywords():
    grouper = build_grouper("dayofyear", window=15, pool_dims=("station",))
    assert grouper.name == "time.dayofyear"
    assert grouper.window == 15
    assert list(grouper.add_dims) == ["station"]


def test_pool_dims_populates_add_dims():
    grouper = build_grouper("month", pool_dims=("station", "lat"))
    assert list(grouper.add_dims) == ["station", "lat"]


def test_empty_pool_dims_means_no_add_dims():
    assert build_grouper("month", pool_dims=()).add_dims == []


def test_explicit_window_overrides_spec_window():
    grouper = build_grouper(_spec(group="dayofyear", window=31), window=7)
    assert grouper.window == 7


def test_explicit_window_one_overrides_spec_window():
    grouper = build_grouper(_spec(group="dayofyear", window=31), window=1)
    assert grouper.window == 1


def test_window_applies_to_month_when_explicit():
    grouper = build_grouper(_spec(group="month"), window=3)
    assert grouper.name == "time.month"
    assert grouper.window == 3


def test_unknown_group_rejected():
    with pytest.raises(ValueError, match="dayofyear"):
        build_grouper("week")
    with pytest.raises(ValueError, match="week"):
        build_grouper(replace(_spec(), group="week"))


def test_groups_constant_still_excludes_season():
    assert GROUPS == ("dayofyear", "month", "none")


# --- effective_samples ------------------------------------------------------


def test_effective_samples_dayofyear_window_scales_with_years_and_window():
    assert effective_samples("dayofyear", 1, 1) == 1
    assert effective_samples("dayofyear", 31, 30) == 930
    assert effective_samples("dayofyear", 5, 10) == 50


def test_effective_samples_month_and_none():
    assert effective_samples("month", 1, 1) == 30
    assert effective_samples("month", 1, 4) == 122
    # one group per year holds the whole year (window must be 1 for "none")
    assert effective_samples("none", 1, 2) == 730


def test_effective_samples_month_ignores_window():
    # window is in days, so it cannot widen a monthly group.
    assert effective_samples("month", 3, 20) == effective_samples("month", 1, 20)


def test_effective_samples_none_ignores_window():
    # a single whole-year group is not widened by a daily window either.
    assert effective_samples("none", 7, 2) == effective_samples("none", 1, 2)
    assert effective_samples("none", 7, 2) == 730


def test_effective_samples_rejects_unknown_group():
    with pytest.raises(ValueError, match="Unknown group"):
        effective_samples("week", 1, 30)


# --- sample_size_qa ---------------------------------------------------------


def test_month_with_window_warns_about_units():
    check = sample_size_qa("month", 3, 20, 50, variable="tmax")
    assert isinstance(check, QACheck)
    assert check.status == "warn"
    assert check.name == "grouping:tmax:window_units"
    assert check.context["window"] == 3
    assert "days" in check.message


def test_short_calibration_warns_about_sample_size():
    check = sample_size_qa("month", 1, 1, 50, variable="precip")
    assert check.status == "warn"
    assert check.name == "grouping:precip:sample_size"
    assert check.context["samples_per_group"] == 30
    assert check.is_ok()  # a warning does not fail the run


def test_dayofyear_window_31_over_30_years_passes():
    check = sample_size_qa("dayofyear", 31, 30, 50, variable="tmax")
    assert check.status == "pass"
    assert check.name == "grouping:tmax:sample_size"
    assert check.context["samples_per_group"] == 930
    assert check.is_ok()


def test_sample_size_boundary_is_one_third():
    # month over 4 years -> ~122 samples per group, so 122 // 3 == 40.
    assert sample_size_qa("month", 1, 4, 40, variable="tmax").status == "pass"
    assert sample_size_qa("month", 1, 4, 41, variable="tmax").status == "warn"
