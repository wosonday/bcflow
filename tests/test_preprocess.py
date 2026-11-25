"""Tests for src/bcflow/preprocess.py."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from bcflow.discovery import get_method_meta
from bcflow.grouping import build_grouper
from bcflow.preprocess import (
    SEED,
    ZeroHandling,
    apply_adapt_freq,
    clip_to_bounds,
    maybe_jitter,
    resolve_adapt_freq,
)
from bcflow.qa import QACheck
from bcflow.variables import DEFAULTS, VariableSpec


def _daily_da(values: np.ndarray, *, units: str = "mm d-1", name: str = "precip") -> xr.DataArray:
    time = pd.date_range("2000-01-01", periods=len(values), freq="D")
    return xr.DataArray(
        values.astype("float64"),
        dims=("time",),
        coords={"time": time},
        name=name,
        attrs={"units": units},
    )


# --- ZeroHandling & maybe_jitter --------------------------------------------


def test_zero_handling_deterministic_and_restores():
    spec = DEFAULTS["precip"]
    da = _daily_da(np.array([0.0, 0.005, 0.5, 2.0, np.nan]))

    j1 = ZeroHandling(spec, "mm d-1").jitter(da)
    j2 = ZeroHandling(spec, "mm d-1", seed=SEED).jitter(da)
    xr.testing.assert_identical(j1, j2)

    assert np.all((j1.values[:2] > 0.0) & (j1.values[:2] < 0.01))
    assert j1.values[2] == pytest.approx(0.5)
    assert np.isnan(j1.values[4])

    restored = ZeroHandling(spec, "mm d-1").restore(j1)
    assert restored.values[0] == 0.0
    assert restored.values[1] == 0.0
    assert restored.values[2] == pytest.approx(0.5)
    assert np.isnan(restored.values[4])


def test_maybe_jitter_deterministic_for_quantile_method():
    spec = DEFAULTS["precip"]
    qdm = get_method_meta("quantile_delta_mapping")
    da = _daily_da(np.array([0.0, 0.0, 1.2, 5.0]))

    out1, qa1 = maybe_jitter(da, spec, qdm, "mm d-1", seed=7)
    out2, qa2 = maybe_jitter(da, spec, qdm, "mm d-1", seed=7)

    xr.testing.assert_identical(out1, out2)
    assert np.all((out1.values[:2] > 0.0) & (out1.values[:2] < 0.01))
    assert out1.values[2] == pytest.approx(1.2)
    assert isinstance(qa1, QACheck)
    assert qa1 == qa2
    assert qa1.name == "preprocess:precip:jitter"
    assert qa1.status == "pass"
    assert qa1.context["seed"] == 7


def test_maybe_jitter_skipped_for_scaling_meta():
    spec = DEFAULTS["precip"]
    scaling = get_method_meta("scaling")
    da = _daily_da(np.array([0.0, 0.0, 1.2, 5.0]))

    out, qa = maybe_jitter(da, spec, scaling, "mm d-1")
    assert out is da
    xr.testing.assert_identical(out, da)
    assert qa is None


def test_maybe_jitter_skipped_when_spec_has_no_thresh():
    spec = DEFAULTS["tmax"]
    qdm = get_method_meta("quantile_delta_mapping")
    da = _daily_da(np.array([0.0, 10.0, 20.0]), units="degC", name="tmax")

    out, qa = maybe_jitter(da, spec, qdm, "degC")
    assert out is da
    assert qa is None


# --- resolve_adapt_freq -----------------------------------------------------


def test_resolve_adapt_freq_default_uses_pretrain():
    spec = DEFAULTS["precip"]
    qdm = get_method_meta("quantile_delta_mapping")
    scaling = get_method_meta("scaling")

    assert resolve_adapt_freq(spec, qdm, {}) == ("1 mm d-1", False)
    assert resolve_adapt_freq(spec, scaling, {}) == ("1 mm d-1", False)


def test_resolve_adapt_freq_native_guard_prevents_double_application():
    spec = DEFAULTS["precip"]  # adapt_freq=True, adapt_freq_thresh="1 mm d-1"
    qdm = get_method_meta("quantile_delta_mapping")

    thresh, is_native = resolve_adapt_freq(spec, qdm, {"adapt_freq_thresh": "2 mm d-1"})
    assert thresh == "2 mm d-1"
    assert is_native is True


def test_resolve_adapt_freq_explicit_thresh_on_non_native_raises():
    spec = DEFAULTS["precip"]
    scaling = get_method_meta("scaling")
    loci = get_method_meta("loci")

    with pytest.raises(ValueError, match="does not support native 'adapt_freq_thresh'"):
        resolve_adapt_freq(spec, scaling, {"adapt_freq_thresh": "1 mm d-1"})
    with pytest.raises(ValueError, match="does not support native 'adapt_freq_thresh'"):
        resolve_adapt_freq(spec, loci, {"adapt_freq_thresh": "1 mm d-1"})


def test_resolve_adapt_freq_disabled_returns_none():
    spec = DEFAULTS["tmax"]
    qdm = get_method_meta("quantile_delta_mapping")
    assert resolve_adapt_freq(spec, qdm, {}) == (None, False)


def test_resolve_adapt_freq_true_without_thresh_raises():
    spec = VariableSpec(name="tmax", units="degC", kind="+", adapt_freq=True, adapt_freq_thresh=None)
    qdm = get_method_meta("quantile_delta_mapping")
    with pytest.raises(ValueError, match="adapt_freq=true requires 'adapt_freq_thresh'"):
        resolve_adapt_freq(spec, qdm, {})


# --- apply_adapt_freq -------------------------------------------------------


def test_apply_adapt_freq_reproducible_and_emits_qa():
    rng = np.random.default_rng(42)
    n = 365 * 3
    ref_vals = np.where(rng.random(n) < 0.4, 0.0, rng.gamma(2.0, 3.0, n))
    hist_vals = np.where(rng.random(n) < 0.75, 0.0, rng.gamma(2.0, 2.0, n))

    ref_cal = _daily_da(ref_vals)
    hist_cal = _daily_da(hist_vals)
    group = build_grouper(DEFAULTS["precip"])

    np.random.seed(999)
    out1, qa1 = apply_adapt_freq(
        ref_cal, hist_cal, "1 mm d-1", group, variable="precip", seed=SEED
    )
    np.random.seed(12345)  # mutate global RNG before second call
    out2, qa2 = apply_adapt_freq(
        ref_cal, hist_cal, "1 mm d-1", group, variable="precip", seed=SEED
    )

    # xsdba stamps attrs["history"] with a wall-clock timestamp, so assert_equal
    # (values/coords, not attrs) plus explicit units checks stays reproducible
    # without weakening the numerical comparison.
    xr.testing.assert_equal(out1, out2)
    assert out1.name == out2.name
    assert out1.attrs.get("units") == out2.attrs.get("units")
    assert out1.attrs.get("units") == ref_cal.attrs.get("units")
    assert qa1 == qa2
    assert qa1.name == "adapt_freq:precip:applied"
    assert qa1.status == "pass"
    assert qa1.context == {"variable": "precip"}
    # Frequency adaptation should reduce excess dry days in hist toward ref.
    assert int((out1.values < 1.0).sum()) < int((hist_cal.values < 1.0).sum())


# --- clip_to_bounds ---------------------------------------------------------


def test_clip_to_bounds_counts_checks_and_preserves_attrs():
    time = pd.date_range("2000-01-01", periods=5, freq="D")
    ds = xr.Dataset(
        data_vars={
            "precip": xr.DataArray(
                np.array([-0.5, -1e-6, 0.0, 5.0, np.nan]),
                dims=("time",),
                attrs={"units": "mm d-1", "long_name": "Precipitation"},
            ),
            "humidity": xr.DataArray(
                np.array([-0.1, 0.2, 0.5, 1.0, 1.2]),
                dims=("time",),
                attrs={"units": "1", "standard_name": "relative_humidity"},
            ),
            "wind": xr.DataArray(
                np.array([0.0, 1.5, 3.0, 4.2, np.nan]),
                dims=("time",),
                attrs={"units": "m s-1"},
            ),
            "tmax": xr.DataArray(
                np.array([-30.0, 0.0, 15.0, 45.0, 50.0]),
                dims=("time",),
                attrs={"units": "degC"},
            ),
        },
        coords={"time": ("time", time, {"axis": "T"})},
        attrs={"title": "toy dataset", "institution": "bcflow"},
    )
    specs = {name: DEFAULTS[name] for name in ("precip", "humidity", "wind", "tmax")}

    clipped, checks, counts = clip_to_bounds(ds, specs)

    assert counts == {
        "precip": (2, 0),
        "humidity": (1, 1),
        "wind": (0, 0),
        "tmax": (0, 0),
    }

    np.testing.assert_allclose(clipped["precip"].values[:4], [0.0, 0.0, 0.0, 5.0])
    assert np.isnan(clipped["precip"].values[4])
    np.testing.assert_allclose(clipped["humidity"].values, [0.0, 0.2, 0.5, 1.0, 1.0])
    xr.testing.assert_identical(clipped["wind"], ds["wind"])
    xr.testing.assert_identical(clipped["tmax"], ds["tmax"])

    assert clipped.attrs == ds.attrs
    assert clipped["precip"].attrs == ds["precip"].attrs
    assert clipped["humidity"].attrs == ds["humidity"].attrs
    assert clipped["time"].attrs == ds["time"].attrs

    by_name = {c.name: c for c in checks}
    assert set(by_name) == {
        "bounds:precip:clipped",
        "bounds:humidity:clipped",
        "bounds:wind:ok",
    }
    assert by_name["bounds:precip:clipped"].status == "warn"
    assert by_name["bounds:precip:clipped"].context["n_below"] == 2
    assert by_name["bounds:precip:clipped"].context["n_above"] == 0
    assert by_name["bounds:humidity:clipped"].status == "warn"
    assert by_name["bounds:humidity:clipped"].context["n_below"] == 1
    assert by_name["bounds:humidity:clipped"].context["n_above"] == 1
    assert by_name["bounds:humidity:clipped"].context["n_clipped"] == 2
    assert by_name["bounds:wind:ok"].status == "pass"
    assert by_name["bounds:wind:ok"].context["n_clipped"] == 0
