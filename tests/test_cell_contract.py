"""Tests for the 7-step cell execution contract in src/bcflow/cell.py."""

from __future__ import annotations

import copy
import importlib.util
import pickle
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
import xsdba
import xsdba.adjustment as xsdba_adj

from bcflow import adjust
from bcflow.cell import (
    STACK_DIM,
    CellPlan,
    CellResult,
    PostStep,
    adjust_trained,
    run_cell,
)
from bcflow.config_schema import PostConfig, TargetConfig, load_config
from bcflow.preprocess import SEED, ZeroHandling
from bcflow.progress import RunCancelled, RunControl
from bcflow.qa import QACheck
from bcflow.resources import WorkerSelection
from bcflow.variables import DEFAULTS, VariableSpec


def _make_dataset(
    *,
    years: int = 3,
    start: str = "2000-01-01",
    seed: int = 10,
    dims: tuple[str, str] = ("time", "station"),
    dry_frac: float = 0.5,
    scale: float = 4.0,
) -> xr.Dataset:
    rng = np.random.default_rng(seed)
    n = 365 * years
    time = pd.date_range(start, periods=n, freq="D")
    stations = ["s1", "s2"]

    rain = np.where(rng.random((n, 2)) < dry_frac, 0.0, rng.gamma(2.0, scale, (n, 2)))
    doy = np.arange(n)[:, None]
    temp = 15.0 + 10.0 * np.sin(2 * np.pi * doy / 365.0) + rng.normal(0.0, 2.0, (n, 2))

    precip_da = xr.DataArray(
        rain.astype("float64"),
        dims=("time", "station"),
        coords={"time": time, "station": stations},
        attrs={"units": "mm d-1"},
    )
    tmax_da = xr.DataArray(
        temp.astype("float64"),
        dims=("time", "station"),
        coords={"time": time, "station": stations},
        attrs={"units": "degC"},
    )
    if dims != ("time", "station"):
        precip_da = precip_da.transpose(*dims)
        tmax_da = tmax_da.transpose(*dims)

    return xr.Dataset({"precip": precip_da, "tmax": tmax_da})


def _specs(*names: str) -> dict[str, VariableSpec]:
    return {name: DEFAULTS[name] for name in names}


def _units(*names: str) -> dict[str, str]:
    return {name: DEFAULTS[name].units for name in names}


@pytest.mark.parametrize("method", ["mbcn", "npdf_transform"])
@pytest.mark.parametrize("seed", [SEED, 1234])
def test_multivariate_random_rotations_are_reproducible(method, seed):
    ref = _make_dataset(years=1, seed=151)
    hist = _make_dataset(years=1, seed=152)
    sim = _make_dataset(years=1, start="2030-01-01", seed=153)
    params = (("n_iter", 2), ("n_escore", -1), ("base_kws", {"nquantiles": 10}))
    plan = CellPlan(
        target_name="joint",
        variables=("tmax", "precip"),
        method=method,
        group="none",
        train_params=params if method == "mbcn" else (),
        adjust_params=params if method == "npdf_transform" else (),
    )
    outputs = []
    for global_seed in (999, 54321):
        np.random.seed(global_seed)
        outputs.append(
            run_cell(
                plan,
                ref=ref,
                hist=hist,
                sim=sim,
                specs=_specs("tmax", "precip"),
                units=_units("tmax", "precip"),
                seed=seed,
            )
        )
    first, second = outputs
    xr.testing.assert_equal(first.scen, second.scen)
    for name in plan.variables:
        assert bool(np.isfinite(first.scen[name]).all())
        assert first.scen[name].values.tobytes() == second.scen[name].values.tobytes()
    if method == "mbcn":
        xr.testing.assert_equal(first.trained_ds, second.trained_ds)
    else:
        assert first.trained_ds is second.trained_ds is None


def test_qdm_precip_jitter_adapt_freq_and_seed_parity():
    ref = _make_dataset(years=3, start="2000-01-01", seed=1, dry_frac=0.4, scale=4.0)
    hist = _make_dataset(years=3, start="2000-01-01", seed=2, dry_frac=0.75, scale=2.5)
    sim = _make_dataset(years=3, start="2030-01-01", seed=3, dry_frac=0.70, scale=3.0)

    plan = CellPlan(
        target_name="precip",
        variables=("precip",),
        method="quantile_delta_mapping",
        group="month",
    )

    np.random.seed(999)
    res1 = run_cell(
        plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip"),
        units=_units("precip"),
        seed=SEED,
    )
    np.random.seed(12345)
    res2 = run_cell(
        plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip"),
        units=_units("precip"),
        seed=SEED,
    )

    xr.testing.assert_equal(res1.scen, res2.scen)
    assert res1.trained_ds is not None
    assert res1.trained_obj is not None
    assert res1.scen["precip"].dtype == np.float64
    assert res1.scen["precip"].dims == ("time", "station")
    assert res1.scen["precip"].attrs["units"] == "mm d-1"
    assert float(res1.scen["precip"].min()) >= 0.0
    # Restored dry days should remain exact 0.0 (no sub-thresh values < 0.01).
    vals = res1.scen["precip"].values
    assert np.any(vals == 0.0)
    assert not np.any((vals > 0.0) & (vals < 0.01))

    qa_names = {c.name: c for c in res1.qa}
    assert qa_names["preprocess:precip:jitter"].status == "pass"
    assert qa_names["adapt_freq:precip:applied"].status == "pass"
    assert "grouping:precip:sample_size" in qa_names
    assert qa_names["grouping:precip:sample_size"].status == "warn"
    assert "bounds:precip:ok" in qa_names or "bounds:precip:clipped" in qa_names
    assert res1.timing["total"] >= 0.0


def test_qdm_native_adapt_freq_guard_avoids_double_application(monkeypatch):
    ref = _make_dataset(years=2, start="2000-01-01", seed=11)
    hist = _make_dataset(years=2, start="2000-01-01", seed=12)
    sim = _make_dataset(years=2, start="2030-01-01", seed=13)

    called = False

    def _boom(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("apply_adapt_freq should not be called when native")

    monkeypatch.setattr("bcflow.cell.apply_adapt_freq", _boom)

    plan = CellPlan(
        target_name="precip",
        variables=("precip",),
        method="quantile_delta_mapping",
        train_params=(("adapt_freq_thresh", "1 mm d-1"),),
    )
    res = run_cell(
        plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip"),
        units=_units("precip"),
    )
    assert not called
    af = next(c for c in res.qa if c.name == "adapt_freq:precip:applied")
    assert af.status == "pass"
    assert af.context.get("native") is True


def test_scaling_omits_nquantiles_extrapolation_and_jitter():
    ref = _make_dataset(years=2, start="2000-01-01", seed=21)
    hist = _make_dataset(years=2, start="2000-01-01", seed=22)
    sim = _make_dataset(years=2, start="2030-01-01", seed=23)

    # precip with scaling: no jitter, no nquantiles, no extrapolation, adapt_freq still runs
    plan = CellPlan(
        target_name="precip",
        variables=("precip",),
        method="scaling",
        group="month",
    )
    res = run_cell(
        plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip"),
        units=_units("precip"),
    )
    assert res.trained_ds is not None
    assert "quantiles" not in res.trained_ds.dims
    qa_names = {c.name for c in res.qa}
    assert "preprocess:precip:jitter" not in qa_names
    assert "grouping:precip:sample_size" not in qa_names
    assert "adapt_freq:precip:applied" in qa_names
    assert res.scen["precip"].attrs["units"] == "mm d-1"


def test_loci_injects_thresh_via_registry_defaults():
    ref = _make_dataset(years=2, start="2000-01-01", seed=31, dry_frac=0.4)
    hist = _make_dataset(years=2, start="2000-01-01", seed=32, dry_frac=0.7)
    sim = _make_dataset(years=2, start="2030-01-01", seed=33, dry_frac=0.6)

    # Note: xsdba's LOCI defaults interp="linear", which fails on string station
    # coords when broadcasting monthly groups (DataArray.interp on string coords);
    # interp="nearest" uses .sel(..., method="nearest") cleanly while still exercising
    # registry_defaults injection of `thresh` and non-quantile skipping of jitter.
    plan = CellPlan(
        target_name="precip",
        variables=("precip",),
        method="loci",
        group="month",
        adjust_params=(("interp", "nearest"),),
    )
    res = run_cell(
        plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip"),
        units=_units("precip"),
    )
    assert res.trained_ds is not None
    assert res.scen["precip"].dims == ("time", "station")
    assert res.scen["precip"].attrs["units"] == "mm d-1"
    assert "preprocess:precip:jitter" not in {c.name for c in res.qa}


def test_loci_defaults_interp_nearest_on_string_station_coords():
    ref = _make_dataset(
        years=2, start="2000-01-01", seed=34, dry_frac=0.4
    ).assign_coords(station=["ST01", "ST02"])
    hist = _make_dataset(
        years=2, start="2000-01-01", seed=35, dry_frac=0.7
    ).assign_coords(station=["ST01", "ST02"])
    sim = _make_dataset(
        years=2, start="2030-01-01", seed=36, dry_frac=0.6
    ).assign_coords(station=["ST01", "ST02"])

    plan = CellPlan(
        target_name="precip",
        variables=("precip",),
        method="loci",
        group="month",
    )
    res = run_cell(
        plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip"),
        units=_units("precip"),
    )
    assert res.trained_ds is not None
    assert res.scen["precip"].dims == ("time", "station")
    assert res.scen["precip"].coords["station"].values.tolist() == ["ST01", "ST02"]
    assert float(res.scen["precip"].min()) >= 0.0


def test_adjust_trained_matches_run_cell_without_duplicate_sample_size_qa():
    ref = _make_dataset(years=2, start="2000-01-01", seed=37, dry_frac=0.4)
    hist = _make_dataset(years=2, start="2000-01-01", seed=38, dry_frac=0.7)
    sim = _make_dataset(years=2, start="2030-01-01", seed=39, dry_frac=0.6)

    tmax_plan = CellPlan(
        target_name="tmax",
        variables=("tmax",),
        method="quantile_delta_mapping",
        group="month",
        train_params=(("nquantiles", 15),),
    )
    tmax_res = run_cell(
        tmax_plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("tmax"),
        units=_units("tmax"),
        seed=SEED,
    )
    tmax_eval_qa: list[QACheck] = []
    tmax_scen = adjust_trained(
        tmax_plan,
        tmax_res.trained_obj,
        sim=sim,
        ref=ref,
        hist=hist,
        specs=_specs("tmax"),
        units=_units("tmax"),
        zeros=tmax_res.zeros,
        qa=tmax_eval_qa,
    )
    # xsdba stamps a wall-clock time into attrs["history"], so compare values
    # and the meaningful unit metadata rather than identical attrs.
    xr.testing.assert_equal(tmax_scen, tmax_res.scen)
    assert tmax_scen["tmax"].attrs["units"] == tmax_res.scen["tmax"].attrs["units"]
    assert tmax_scen["tmax"].attrs["units"] == "degC"
    assert not any(c.name.startswith("grouping:") for c in tmax_eval_qa)

    precip_plan = CellPlan(
        target_name="precip",
        variables=("precip",),
        method="quantile_delta_mapping",
        group="month",
        train_params=(("nquantiles", 15),),
    )
    precip_res = run_cell(
        precip_plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip"),
        units=_units("precip"),
        seed=SEED,
    )
    # Match the RNG state right after ref & hist jitter (before sim jitter).
    aligned_zeros = ZeroHandling(DEFAULTS["precip"], "mm d-1", seed=SEED)
    aligned_zeros.jitter(ref["precip"])
    aligned_zeros.jitter(hist["precip"])
    precip_eval_qa: list[QACheck] = []
    precip_scen = adjust_trained(
        precip_plan,
        precip_res.trained_obj,
        sim=sim,
        ref=ref,
        hist=hist,
        specs=_specs("precip"),
        units=_units("precip"),
        zeros=aligned_zeros,
        qa=precip_eval_qa,
    )
    xr.testing.assert_equal(precip_scen, precip_res.scen)
    assert not any(c.name.startswith("grouping:") for c in precip_eval_qa)


def test_adjust_trained_reuses_post_trained_without_retraining(monkeypatch):
    ref = _make_dataset(years=4, start="2000-01-01", seed=41, dry_frac=0.4, scale=6.0)
    hist = _make_dataset(years=4, start="2000-01-01", seed=42, dry_frac=0.6, scale=4.0)
    sim = _make_dataset(years=4, start="2030-01-01", seed=43, dry_frac=0.5, scale=5.0)

    plan = CellPlan(
        target_name="precip",
        variables=("precip",),
        method="quantile_delta_mapping",
        group="month",
        train_params=(("nquantiles", 15),),
        post=(
            PostStep(
                method="extreme_values",
                train_params=(("q_thresh", 0.90),),
                adjust_params=(("frac", 0.7), ("power", 3.0)),
            ),
        ),
    )
    res = run_cell(
        plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip"),
        units=_units("precip"),
        seed=SEED,
    )
    assert len(res.post_trained) == 1

    def _no_retrain(*args, **kwargs):
        raise AssertionError(
            "ExtremeValues.train should not be called when post_trained is supplied"
        )

    monkeypatch.setattr(xsdba_adj.ExtremeValues, "train", classmethod(_no_retrain))

    aligned_zeros = ZeroHandling(DEFAULTS["precip"], "mm d-1", seed=SEED)
    aligned_zeros.jitter(ref["precip"])
    aligned_zeros.jitter(hist["precip"])
    eval_qa: list[QACheck] = []
    scen = adjust_trained(
        plan,
        res.trained_obj,
        sim=sim,
        ref=ref,
        hist=hist,
        specs=_specs("precip"),
        units=_units("precip"),
        zeros=aligned_zeros,
        post_trained=res.post_trained,
        qa=eval_qa,
    )
    xr.testing.assert_equal(scen, res.scen)
    assert any(
        c.name == "post:precip:extreme_values" and c.status == "pass" for c in eval_qa
    )


def test_extreme_values_post_step_and_step7_order(monkeypatch):
    ref = _make_dataset(years=4, start="2000-01-01", seed=41, dry_frac=0.4, scale=6.0)
    hist = _make_dataset(years=4, start="2000-01-01", seed=42, dry_frac=0.6, scale=4.0)
    # Use station-first dims on sim to verify transpose("time", ...) runs last.
    sim = _make_dataset(
        years=4,
        start="2030-01-01",
        seed=43,
        dims=("station", "time"),
        dry_frac=0.5,
        scale=5.0,
    )

    events: list[str] = []
    saw_jittered_zeros: list[bool] = []

    from dataclasses import replace

    real_ev_adjust = xsdba_adj.ExtremeValues.adjust

    def spy_ev_adjust(self, sim_arg, scen_arg, **kwargs):
        events.append("post")
        vals = scen_arg.values
        # Before zeros.restore(), jittered dry days are strictly in (0, 0.01).
        saw_jittered_zeros.append(bool(np.any((vals > 0.0) & (vals < 0.01))))
        out = real_ev_adjust(self, sim_arg, scen_arg, **kwargs)
        # Inject an above-bound value (survives zeros.restore()) to confirm
        # clip_to_bounds runs after post and restore.
        out = out.copy()
        out.values[0, 0] = 999.0
        return out

    monkeypatch.setattr(xsdba_adj.ExtremeValues, "adjust", spy_ev_adjust)

    from bcflow import cell as cell_mod

    real_clip = cell_mod.clip_to_bounds

    def spy_clip(ds, specs):
        events.append("clip")
        # Before clip_to_bounds, ZeroHandling.restore has already turned sub-thresh to 0.0.
        vals = ds["precip"].values
        assert not np.any((vals > 0.0) & (vals < 0.01))
        return real_clip(ds, specs)

    monkeypatch.setattr(cell_mod, "clip_to_bounds", spy_clip)

    bounded_precip = replace(DEFAULTS["precip"], bounds=(0.0, 200.0))
    plan = CellPlan(
        target_name="precip",
        variables=("precip",),
        method="quantile_delta_mapping",
        group="month",
        post=(
            PostStep(
                method="extreme_values",
                train_params=(("q_thresh", 0.90),),
                adjust_params=(("frac", 0.7), ("power", 3.0)),
            ),
        ),
    )
    res = run_cell(
        plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs={"precip": bounded_precip},
        units=_units("precip"),
    )

    assert events == ["post", "clip"]
    assert saw_jittered_zeros == [True]
    assert res.clip_counts["precip"][1] >= 1
    assert float(res.scen["precip"].min()) >= 0.0
    assert float(res.scen["precip"].max()) <= 200.0
    assert res.scen["precip"].dims == ("time", "station")
    assert res.scen["precip"].attrs["units"] == "mm d-1"

    qa_by_name = {c.name: c for c in res.qa}
    assert qa_by_name["post:precip:extreme_values"].status == "pass"
    assert qa_by_name["bounds:precip:clipped"].status == "warn"


def test_principal_components_multivar_stacked():
    ref = _make_dataset(years=2, start="2000-01-01", seed=51)
    hist = _make_dataset(years=2, start="2000-01-01", seed=52)
    sim = _make_dataset(years=2, start="2030-01-01", seed=53)

    plan = CellPlan(
        target_name="precip_tmax",
        variables=("precip", "tmax"),
        method="principal_components",
        group="month",
    )
    res = run_cell(
        plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip", "tmax"),
        units=_units("precip", "tmax"),
    )

    assert res.trained_ds is not None
    assert STACK_DIM not in res.scen.dims
    assert set(res.scen.data_vars) == {"precip", "tmax"}
    assert res.scen["precip"].dims == ("time", "station")
    assert res.scen["tmax"].dims == ("time", "station")
    assert res.scen["precip"].attrs["units"] == "mm d-1"
    assert res.scen["tmax"].attrs["units"] == "degC"
    assert float(res.scen["precip"].min()) >= 0.0


def test_mbcn_multivar_stacked_and_base_kws_vars(monkeypatch):
    ref = _make_dataset(years=1, start="2000-01-01", seed=61)
    hist = _make_dataset(years=1, start="2000-01-01", seed=62)
    sim = _make_dataset(years=1, start="2030-01-01", seed=63)

    seen_base_kws_vars: list[dict] = []
    real_mbcn_adjust = xsdba_adj.MBCn.adjust

    def spy_mbcn_adjust(self, sim_arg, ref_arg, hist_arg, **kwargs):
        seen_base_kws_vars.append(kwargs.get("base_kws_vars"))
        return real_mbcn_adjust(self, sim_arg, ref_arg, hist_arg, **kwargs)

    monkeypatch.setattr(xsdba_adj.MBCn, "adjust", spy_mbcn_adjust)

    plan = CellPlan(
        target_name="precip_tmax",
        variables=("precip", "tmax"),
        method="mbcn",
        group="none",
        train_params=(("n_iter", 2), ("n_escore", -1)),
    )
    res = run_cell(
        plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip", "tmax"),
        units=_units("precip", "tmax"),
    )

    assert seen_base_kws_vars == [{"precip": {"kind": "*"}, "tmax": {"kind": "+"}}]
    assert res.trained_ds is not None
    assert set(res.scen.data_vars) == {"precip", "tmax"}
    assert res.scen["precip"].attrs["units"] == "mm d-1"
    assert res.scen["tmax"].attrs["units"] == "degC"
    assert float(res.scen["precip"].min()) >= 0.0


def test_npdf_transform_adjust_only_and_dataset_scen_unwrapping():
    ref = _make_dataset(years=1, start="2000-01-01", seed=71)
    hist = _make_dataset(years=1, start="2000-01-01", seed=72)
    sim = _make_dataset(years=1, start="2030-01-01", seed=73)

    plan = CellPlan(
        target_name="precip_tmax",
        variables=("precip", "tmax"),
        method="npdf_transform",
        group="month",
        adjust_params=(("n_iter", 2), ("n_escore", -1)),
    )

    res = run_cell(
        plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip", "tmax"),
        units=_units("precip", "tmax"),
    )
    assert res.trained_ds is None
    assert res.trained_obj is None
    assert set(res.scen.data_vars) == {"precip", "tmax"}
    assert res.scen["precip"].dims == ("time", "station")
    assert res.scen["precip"].attrs["units"] == "mm d-1"
    assert res.scen["tmax"].attrs["units"] == "degC"

    # Also verify explicit xr.Dataset({"scen": ..., "scenh": ...}) unwrapping
    # (when xsdba EXTRA_OUTPUT is active).
    with xsdba.set_options(extra_output=True):
        res_extra = run_cell(
            plan,
            ref=ref,
            hist=hist,
            sim=sim,
            specs=_specs("precip", "tmax"),
            units=_units("precip", "tmax"),
        )
    assert res_extra.trained_ds is None
    assert set(res_extra.scen.data_vars) == {"precip", "tmax"}


def test_otc_projection_rejection_and_mocked_non_projection(monkeypatch):
    ref = _make_dataset(years=1, start="2000-01-01", seed=81)
    hist = _make_dataset(years=1, start="2000-01-01", seed=82)
    sim = _make_dataset(years=1, start="2000-01-01", seed=83)

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda mod: object() if mod == "ot" else real_find_spec(mod),
    )

    # has_projection=True must be rejected cleanly.
    plan_proj = CellPlan(
        target_name="precip_tmax",
        variables=("precip", "tmax"),
        method="otc",
        group="none",
        has_projection=True,
    )
    with pytest.raises(ValueError, match="does not support projection"):
        run_cell(
            plan_proj,
            ref=ref,
            hist=hist,
            sim=sim,
            specs=_specs("precip", "tmax"),
            units=_units("precip", "tmax"),
        )

    # has_projection=False works with mocked OTC.adjust.
    received_args: list[tuple] = []

    def fake_otc_adjust(ref_arg, hist_arg, **kwargs):
        received_args.append((ref_arg, hist_arg, kwargs))
        assert "sim" not in kwargs
        assert "group" in kwargs
        return hist_arg.copy()

    monkeypatch.setattr(
        xsdba_adj.OTC,
        "adjust",
        classmethod(
            lambda cls, ref_a, hist_a, **kw: fake_otc_adjust(ref_a, hist_a, **kw)
        ),
    )

    plan_noproj = CellPlan(
        target_name="precip_tmax",
        variables=("precip", "tmax"),
        method="otc",
        group="none",
        has_projection=False,
    )
    res = run_cell(
        plan_noproj,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip", "tmax"),
        units=_units("precip", "tmax"),
    )
    assert len(received_args) == 1
    assert res.trained_ds is None
    assert res.trained_obj is None
    assert set(res.scen.data_vars) == {"precip", "tmax"}
    assert res.scen["precip"].attrs["units"] == "mm d-1"
    assert res.scen["tmax"].attrs["units"] == "degC"


def test_validation_guards_and_cancellation():
    ref = _make_dataset(years=1, start="2000-01-01", seed=91)
    hist = _make_dataset(years=1, start="2000-01-01", seed=92)
    sim = _make_dataset(years=1, start="2030-01-01", seed=93)

    # Post method rejected as primary method.
    post_as_primary = CellPlan(
        target_name="precip",
        variables=("precip",),
        method="extreme_values",
    )
    with pytest.raises(ValueError, match="family 'post'"):
        run_cell(
            post_as_primary,
            ref=ref,
            hist=hist,
            sim=sim,
            specs=_specs("precip"),
            units=_units("precip"),
        )

    # Too few variables for multivariate method.
    too_few = CellPlan(
        target_name="precip",
        variables=("precip",),
        method="principal_components",
    )
    with pytest.raises(ValueError, match="requires at least 2 variable"):
        run_cell(
            too_few,
            ref=ref,
            hist=hist,
            sim=sim,
            specs=_specs("precip"),
            units=_units("precip"),
        )

    # Non-post method rejected inside PostStep.
    bad_post = CellPlan(
        target_name="precip",
        variables=("precip",),
        method="scaling",
        post=(PostStep(method="scaling"),),
    )
    with pytest.raises(ValueError, match="must have family 'post'"):
        run_cell(
            bad_post,
            ref=ref,
            hist=hist,
            sim=sim,
            specs=_specs("precip"),
            units=_units("precip"),
        )

    # Cooperative cancellation.
    ctrl = RunControl()
    ctrl.request_cancel()
    with pytest.raises(RunCancelled):
        run_cell(
            CellPlan(target_name="precip", variables=("precip",), method="scaling"),
            ref=ref,
            hist=hist,
            sim=sim,
            specs=_specs("precip"),
            units=_units("precip"),
            control=ctrl,
        )


def test_caller_supplied_qa_list_receives_fail_check_before_raise(monkeypatch):
    ref = _make_dataset(years=1, start="2000-01-01", seed=111)
    hist = _make_dataset(years=1, start="2000-01-01", seed=112)
    sim = _make_dataset(years=1, start="2000-01-01", seed=113)

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda mod: object() if mod == "ot" else real_find_spec(mod),
    )

    plan = CellPlan(
        target_name="precip_tmax",
        variables=("precip", "tmax"),
        method="otc",
        group="none",
        has_projection=True,
    )
    qa: list[QACheck] = []
    with pytest.raises(ValueError, match="does not support projection"):
        run_cell(
            plan,
            ref=ref,
            hist=hist,
            sim=sim,
            specs=_specs("precip", "tmax"),
            units=_units("precip", "tmax"),
            qa=qa,
        )

    assert len(qa) == 1
    check = qa[0]
    assert isinstance(check, QACheck)
    assert check.name == "method:precip_tmax:no_projection"
    assert check.status == "fail"
    assert check.context == {"target": "precip_tmax", "method": "otc"}


def test_caller_supplied_qa_list_is_returned_as_cell_result_qa():
    ref = _make_dataset(years=1, start="2000-01-01", seed=121)
    hist = _make_dataset(years=1, start="2000-01-01", seed=122)
    sim = _make_dataset(years=1, start="2030-01-01", seed=123)

    plan = CellPlan(target_name="precip", variables=("precip",), method="scaling")
    qa: list[QACheck] = []
    res = run_cell(
        plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip"),
        units=_units("precip"),
        qa=qa,
    )
    assert res.qa is qa
    assert any(c.name == "bounds:precip:ok" for c in qa)


def test_pickle_roundtrip_cell_plan_and_cell_result():
    ref = _make_dataset(years=2, start="2000-01-01", seed=101)
    hist = _make_dataset(years=2, start="2000-01-01", seed=102)
    sim = _make_dataset(years=2, start="2030-01-01", seed=103)

    plan = CellPlan(
        target_name="precip",
        variables=("precip",),
        method="quantile_delta_mapping",
        group="month",
        window=1,
        pool_dims=(),
        train_params=(("nquantiles", 20),),
        adjust_params=(("interp", "nearest"),),
        post=(PostStep(method="extreme_values", train_params=(("q_thresh", 0.95),)),),
        has_projection=True,
    )
    plan_rt = pickle.loads(pickle.dumps(plan))
    assert plan_rt == plan

    # Run without post to keep test fast; strip trained_obj=None like WorkerPool callers.
    res = run_cell(
        CellPlan(
            target_name="precip",
            variables=("precip",),
            method="quantile_delta_mapping",
            train_params=(("nquantiles", 20),),
        ),
        ref=ref,
        hist=hist,
        sim=sim,
        specs=_specs("precip"),
        units=_units("precip"),
    )
    assert res.zeros is not None
    res_for_ipc = CellResult(
        scen=res.scen,
        trained_ds=res.trained_ds,
        qa=res.qa,
        timing=res.timing,
        clip_counts=res.clip_counts,
        trained_obj=None,
        zeros=None,
        post_trained=(),
    )
    res_rt = pickle.loads(pickle.dumps(res_for_ipc))
    xr.testing.assert_identical(res_rt.scen, res.scen)
    xr.testing.assert_identical(res_rt.trained_ds, res.trained_ds)
    assert res_rt.qa == res.qa
    assert res_rt.timing == res.timing
    assert res_rt.clip_counts == res.clip_counts
    assert res_rt.trained_obj is None
    assert res_rt.zeros is None
    assert res_rt.post_trained == ()


@pytest.mark.parametrize(
    ("method", "group", "plan_train", "plan_adjust"),
    [
        ("mbcn", "none", (("n_iter", 2), ("n_escore", -1)), ()),
        ("principal_components", "month", (), ()),
        ("npdf_transform", "month", (), (("n_iter", 2), ("n_escore", -1))),
    ],
)
def test_precip_univariate_params_do_not_leak_into_multivariate_methods(
    monkeypatch, method, group, plan_train, plan_adjust
):
    ref = _make_dataset(years=1, start="2000-01-01", seed=131)
    hist = _make_dataset(years=1, start="2000-01-01", seed=132)
    sim = _make_dataset(years=1, start="2030-01-01", seed=133)

    leaky_precip = replace(
        DEFAULTS["precip"],
        train_params={"nquantiles": 33, "leaky_train_kw": "bad"},
        adjust_params={
            "interp": "nearest",
            "extrapolation": "constant",
            "leaky_adjust_kw": "bad",
        },
    )
    specs = {"precip": leaky_precip, "tmax": DEFAULTS["tmax"]}

    seen_train_kws: list[dict] = []
    seen_adjust_kws: list[dict] = []

    if method == "mbcn":
        real_train = xsdba_adj.MBCn.train
        real_adjust = xsdba_adj.MBCn.adjust

        def spy_train(cls, ref_a, hist_a, **kw):
            seen_train_kws.append(dict(kw))
            return real_train(ref_a, hist_a, **kw)

        def spy_adjust(self, sim_a, ref_a, hist_a, **kw):
            seen_adjust_kws.append(dict(kw))
            return real_adjust(self, sim_a, ref_a, hist_a, **kw)

        monkeypatch.setattr(xsdba_adj.MBCn, "train", classmethod(spy_train))
        monkeypatch.setattr(xsdba_adj.MBCn, "adjust", spy_adjust)
    elif method == "principal_components":
        real_train = xsdba_adj.PrincipalComponents.train
        real_adjust = xsdba_adj.PrincipalComponents.adjust

        def spy_train(cls, ref_a, hist_a, **kw):
            seen_train_kws.append(dict(kw))
            return real_train(ref_a, hist_a, **kw)

        def spy_adjust(self, sim_a, **kw):
            seen_adjust_kws.append(dict(kw))
            return real_adjust(self, sim_a, **kw)

        monkeypatch.setattr(
            xsdba_adj.PrincipalComponents, "train", classmethod(spy_train)
        )
        monkeypatch.setattr(xsdba_adj.PrincipalComponents, "adjust", spy_adjust)
    else:
        real_adjust = xsdba_adj.NpdfTransform.adjust

        def spy_adjust(cls, ref_a, hist_a, sim_a, **kw):
            seen_adjust_kws.append(dict(kw))
            return real_adjust(ref_a, hist_a, sim_a, **kw)

        monkeypatch.setattr(xsdba_adj.NpdfTransform, "adjust", classmethod(spy_adjust))

    plan = CellPlan(
        target_name="precip_tmax",
        variables=("precip", "tmax"),
        method=method,
        group=group,
        train_params=plan_train,
        adjust_params=plan_adjust,
    )
    res = run_cell(
        plan,
        ref=ref,
        hist=hist,
        sim=sim,
        specs=specs,
        units=_units("precip", "tmax"),
    )
    assert set(res.scen.data_vars) == {"precip", "tmax"}
    for kw in seen_train_kws:
        assert "nquantiles" not in kw
        assert "leaky_train_kw" not in kw
    for kw in seen_adjust_kws:
        assert "interp" not in kw
        assert "extrapolation" not in kw
        assert "leaky_adjust_kw" not in kw
    if method == "mbcn":
        assert seen_adjust_kws[0]["base_kws_vars"] == {
            "precip": {"kind": "*"},
            "tmax": {"kind": "+"},
        }
    elif method == "principal_components":
        assert seen_train_kws[0]["crd_dim"] == STACK_DIM


def test_run_target_univariate_parity_stations_and_grid(
    toy_config, grid_config, monkeypatch
):
    cfg_st = load_config(toy_config)
    for target in cfg_st.resolved_targets():
        v = target.variables[0]
        qa_var: list[QACheck] = []
        qa_tgt: list[QACheck] = []
        res_var = adjust.run_variable(cfg_st, v, qa_var)
        res_tgt = adjust.run_target(cfg_st, target, qa_tgt)[v]
        xr.testing.assert_identical(res_var["scen"].coords, res_tgt["scen"].coords)
        xr.testing.assert_equal(res_var["scen"], res_tgt["scen"])
        assert res_var["scen"].attrs["units"] == res_tgt["scen"].attrs["units"]
        xr.testing.assert_equal(res_var["adj"].ds, res_tgt["adj"].ds)
        pd.testing.assert_frame_equal(res_var["evaluation"], res_tgt["evaluation"])
        xr.testing.assert_equal(res_var["hist_eval"], res_tgt["hist_eval"])

    open_reference = adjust.open_grid_reference
    load_model = adjust.load_model_variable
    monkeypatch.setattr(
        adjust,
        "open_grid_reference",
        lambda *a, **kw: (
            (ref := open_reference(*a, **kw))[0].sel(time=slice("2000", "2002")),
            ref[1],
            ref[2],
        ),
    )
    monkeypatch.setattr(
        adjust,
        "load_model_variable",
        lambda *a, **kw: load_model(*a, **kw).sel(time=slice("2000", "2002")),
    )
    raw_grid = copy.deepcopy(grid_config)
    raw_grid["grid"].update(region={"lat": [15, 16], "lon": [105, 107]}, block_size=3)
    raw_grid["periods"] = {
        "calibration": ["2000", "2001"],
        "validation": ["2002", "2002"],
    }
    cfg_gr = load_config(raw_grid)
    target_tmax = cfg_gr.resolved_targets()[0]
    res_var_g = adjust.run_variable(cfg_gr, "tmax", [])
    res_tgt_g = adjust.run_target(cfg_gr, target_tmax, [])["tmax"]
    xr.testing.assert_identical(res_var_g["scen"].coords, res_tgt_g["scen"].coords)
    xr.testing.assert_equal(res_var_g["scen"], res_tgt_g["scen"])
    assert res_var_g["scen"].attrs["units"] == res_tgt_g["scen"].attrs["units"]
    xr.testing.assert_equal(res_var_g["adj"].ds, res_tgt_g["adj"].ds)
    pd.testing.assert_frame_equal(res_var_g["evaluation"], res_tgt_g["evaluation"])
    xr.testing.assert_equal(res_var_g["hist_eval"], res_tgt_g["hist_eval"])


def test_run_target_early_method_validation_before_loading(toy_config, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("No loader should run when target method check fails")

    monkeypatch.setattr(adjust, "load_station_variable", _boom)
    monkeypatch.setattr(adjust, "open_grid_reference", _boom)
    cfg = load_config(toy_config)

    bad_targets = [
        (
            TargetConfig(name="t_unk", variables=["tmax", "precip"], method="nope"),
            "method:t_unk:unknown",
        ),
        (
            TargetConfig(name="t_post", variables=["tmax"], method="extreme_values"),
            "method:t_post:invalid_family",
        ),
        (
            TargetConfig(
                name="t_arity", variables=["tmax"], method="mbcn", group="none"
            ),
            "method:t_arity:min_vars",
        ),
        (
            TargetConfig(name="t_uni", variables=["tmax", "precip"], method="scaling"),
            "method:t_uni:univariate_only",
        ),
        (
            TargetConfig(
                name="t_bad_post",
                variables=["tmax", "precip"],
                method="principal_components",
                post=[PostConfig(method="scaling")],
            ),
            "post:t_bad_post:invalid_family",
        ),
    ]
    for target, expected_qa in bad_targets:
        qa: list[QACheck] = []
        with pytest.raises(ValueError):
            adjust.run_target(cfg, target, qa)
        assert any(c.name == expected_qa and c.status == "fail" for c in qa)

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda mod: object() if mod == "ot" else real_find_spec(mod),
    )
    qa_otc: list[QACheck] = []
    with pytest.raises(ValueError, match="does not support projection"):
        adjust.run_target(
            cfg,
            TargetConfig(
                name="t_otc", variables=["tmax", "precip"], method="otc", group="none"
            ),
            qa_otc,
        )
    assert any(
        c.name == "method:t_otc:no_projection" and c.status == "fail" for c in qa_otc
    )


def test_run_target_multivariate_stations_and_intersection(toy_config):
    cfg_dict = copy.deepcopy(toy_config)
    cfg_dict.pop("correction", None)
    cfg_dict["targets"] = [
        {
            "name": "joint",
            "variables": ["tmax", "precip"],
            "method": "principal_components",
            "group": "month",
        }
    ]
    cfg = load_config(cfg_dict)
    target = cfg.resolved_targets()[0]
    qa: list[QACheck] = []
    out = adjust.run_target(cfg, target, qa)
    assert set(out) == {"tmax", "precip"}
    assert not any(c.status == "fail" for c in qa)
    assert out["tmax"]["adj"] is out["precip"]["adj"]
    assert float(out["precip"]["scen"].min()) >= 0.0
    assert out["tmax"]["evaluation"] is not None

    # Drop precip column from one station -> warn & continue on common stations.
    st2 = Path(cfg_dict["stations"]["data_dir"]) / "ST02.csv"
    pd.read_csv(st2).drop(columns=["precip"]).to_csv(st2, index=False)
    qa_drop: list[QACheck] = []
    out_drop = adjust.run_target(cfg, target, qa_drop)
    warn = next(c for c in qa_drop if c.name == "target:joint:station_intersection")
    assert warn.status == "warn"
    assert warn.context["dropped_stations"] == ["ST02"]
    assert (
        out_drop["tmax"]["scen"].sizes["station"] < out["tmax"]["scen"].sizes["station"]
    )

    # Drop precip from ST01 and tmax from ST03 -> 0 common stations -> fail cleanly.
    st1 = Path(cfg_dict["stations"]["data_dir"]) / "ST01.csv"
    st3 = Path(cfg_dict["stations"]["data_dir"]) / "ST03.csv"
    pd.read_csv(st1).drop(columns=["precip"]).to_csv(st1, index=False)
    pd.read_csv(st3).drop(columns=["tmax"]).to_csv(st3, index=False)
    qa_empty: list[QACheck] = []
    with pytest.raises(ValueError, match="0 common stations"):
        adjust.run_target(cfg, target, qa_empty)
    assert any(
        c.name == "target:joint:station_intersection" and c.status == "fail"
        for c in qa_empty
    )


def test_run_target_multivariate_grid_parallel_matches_sequential_and_ipc_safe(
    grid_config, monkeypatch
):
    raw = copy.deepcopy(grid_config)
    raw.pop("correction", None)
    raw["grid"].update(region={"lat": [15, 16], "lon": [105, 107]}, block_size=3)
    raw["periods"] = {"calibration": ["2000", "2001"], "validation": ["2002", "2002"]}
    raw["targets"] = [
        {
            "name": "joint",
            "variables": ["tmax", "precip"],
            "method": "principal_components",
            "group": "month",
        }
    ]

    open_reference = adjust.open_grid_reference
    load_model = adjust.load_model_variable

    def short_reference(*args, **kwargs):
        ref, lat, lon = open_reference(*args, **kwargs)
        return ref.sel(time=slice("2000", "2002")), lat, lon

    def short_model(*args, **kwargs):
        return load_model(*args, **kwargs).sel(time=slice("2000", "2002"))

    def select(requested, fraction, remaining, estimated):
        budget = remaining * estimated
        return WorkerSelection(
            workers=min(requested, remaining),
            cpu_limit=8,
            memory_limit=remaining,
            available_bytes=budget * 2,
            budget_bytes=budget,
            estimated_task_bytes=estimated,
            over_budget=False,
        )

    monkeypatch.setattr(adjust, "open_grid_reference", short_reference)
    monkeypatch.setattr(adjust, "load_model_variable", short_model)
    monkeypatch.setattr(adjust, "select_workers", select)

    regrid_methods: dict[str, set[str]] = {"tmax": set(), "precip": set()}
    orig_regrid = adjust.regrid_to_points

    def spy_regrid(da, target, qa, **kwargs):
        var = kwargs["source_label"].split(":")[0]
        regrid_methods[var].add(kwargs["method"])
        return orig_regrid(da, target, qa, **kwargs)

    monkeypatch.setattr(adjust, "regrid_to_points", spy_regrid)

    outputs = []
    qas = []
    for workers in (1, 2):
        raw["grid"]["workers"] = workers
        cfg = load_config(raw)
        target = cfg.resolved_targets()[0]
        qa: list[QACheck] = []
        outputs.append(adjust.run_target(cfg, target, qa))
        qas.append(qa)

    assert regrid_methods["precip"] == {"conservative"}
    assert regrid_methods["tmax"] == {"bilinear"}

    seq, par = outputs
    assert not [c for c in qas[0] if c.status == "fail"]
    assert not [c for c in qas[1] if c.status == "fail"]
    # precip has bounds (0.0, None) -> 1 aggregated bounds check; tmax has (None, None) -> 0.
    assert sum(c.name.startswith("bounds:precip:") for c in qas[1]) == 1
    assert sum(c.name.startswith("bounds:tmax:") for c in qas[1]) == 0
    for v in ("tmax", "precip"):
        xr.testing.assert_identical(seq[v]["scen"].coords, par[v]["scen"].coords)
        xr.testing.assert_allclose(seq[v]["scen"], par[v]["scen"])
        xr.testing.assert_allclose(seq[v]["adj"].ds, par[v]["adj"].ds)
        pd.testing.assert_frame_equal(seq[v]["evaluation"], par[v]["evaluation"])
        xr.testing.assert_allclose(seq[v]["hist_eval"], par[v]["hist_eval"])

    # Verify _correct_target_block_task strips live TrainAdjust/ZeroHandling/post_trained.
    ds = _make_dataset(years=1, start="2000-01-01", seed=141)
    cfg = load_config(raw)
    target = cfg.resolved_targets()[0]
    specs = {v: cfg.spec(v) for v in target.variables}
    plan = adjust._build_target_plan(target, specs)
    part, _ = adjust._correct_target_block_task(
        cfg,
        target,
        specs,
        {"tmax": ds["tmax"], "precip": ds["precip"]},
        {"tmax": ds["tmax"], "precip": ds["precip"]},
        {"tmax": ds["tmax"], "precip": ds["precip"]},
        [],
        plan,
    )
    assert isinstance(part["adj"], adjust._CombinedAdjustment)
    assert "zeros" not in part and "post_trained" not in part
    pickle.loads(pickle.dumps(part))


def test_run_target_grid_memory_estimate_uses_max_time_steps(grid_config, monkeypatch):
    raw = copy.deepcopy(grid_config)
    raw.pop("correction", None)
    raw["grid"].update(
        region={"lat": [15, 16], "lon": [105, 107]}, block_size=3, workers=1
    )
    raw["periods"] = {"calibration": ["2000", "2001"], "validation": None}
    raw["targets"] = [
        {
            "name": "joint",
            "variables": ["tmax", "precip"],
            "method": "principal_components",
            "group": "month",
        }
    ]
    open_reference = adjust.open_grid_reference
    load_model = adjust.load_model_variable
    lengths: dict[tuple[str, str], int] = {}

    def varied_reference(grid_cfg, variable, qa):
        ref, lat, lon = open_reference(grid_cfg, variable, qa)
        end = "2002" if variable == "tmax" else "2001"
        sliced = ref.sel(time=slice("2000", end))
        lengths[("ref", variable)] = sliced.sizes["time"]
        return sliced, lat, lon

    def varied_model(model_cfg, kind, variable, qa):
        da = load_model(model_cfg, kind, variable, qa)
        end = "2002" if variable == "tmax" else "2001"
        sliced = da.sel(time=slice("2000", end))
        lengths[(kind, variable)] = sliced.sizes["time"]
        return sliced

    seen_steps: list[tuple[int, int, int, int, float]] = []
    real_estimate = adjust.estimate_task_bytes

    def spy_estimate(ref_steps, hist_steps, sim_steps, cells, **kwargs):
        seen_steps.append(
            (
                ref_steps,
                hist_steps,
                sim_steps,
                kwargs["n_vars"],
                kwargs["method_factor"],
            )
        )
        return real_estimate(ref_steps, hist_steps, sim_steps, cells, **kwargs)

    monkeypatch.setattr(adjust, "open_grid_reference", varied_reference)
    monkeypatch.setattr(adjust, "load_model_variable", varied_model)
    monkeypatch.setattr(adjust, "estimate_task_bytes", spy_estimate)

    cfg = load_config(raw)
    target = cfg.resolved_targets()[0]
    qa: list[QACheck] = []
    adjust.run_target(cfg, target, qa)

    assert seen_steps
    assert lengths[("ref", "tmax")] > lengths[("ref", "precip")]
    expected_ref = max(lengths[("ref", "tmax")], lengths[("ref", "precip")])
    expected_hist = max(lengths[("hist", "tmax")], lengths[("hist", "precip")])
    expected_sim = max(lengths[("sim", "tmax")], lengths[("sim", "precip")])
    assert all(
        r == expected_ref and h == expected_hist and s == expected_sim
        for r, h, s, _, _ in seen_steps
    )
    assert all(n == 2 and f == 1.5 for _, _, _, n, f in seen_steps)


@pytest.mark.parametrize(
    ("train_params", "adjust_params", "expected"),
    [
        ({}, {}, 50),
        ({"nquantiles": 15}, {}, 15),
        ({"nquantiles": np.int64(75)}, {}, 75),
        ({"nquantiles": [0.1, 0.5, 0.9]}, {}, 3),
        ({"base_kws": {"nquantiles": 200}}, {}, 200),
        ({}, {"base_kws": {"nquantiles": 10_000}}, 10_000),
        ({}, {"nquantiles": 300}, 300),
        ({"nquantiles": 20, "base_kws": {"nquantiles": 400}}, {}, 400),
        (
            {"nquantiles": 20, "base_kws": {"nquantiles": 400}},
            {"nquantiles": 600, "base_kws": {"nquantiles": 500}},
            600,
        ),
        (
            {"nquantiles": 20},
            {"base_kws": {"nquantiles": np.linspace(0, 1, 100)}},
            100,
        ),
        (
            {"base_kws": {"nquantiles": 400}},
            {"base_kws_vars": {"tmax": {"nquantiles": 800}}},
            800,
        ),
    ],
)
def test_grid_quantile_count_uses_largest_known_parameter_path(
    train_params, adjust_params, expected
):
    plan = CellPlan(
        target_name="joint",
        variables=("tmax", "precip"),
        method="npdf_transform",
        train_params=tuple(train_params.items()),
        adjust_params=tuple(adjust_params.items()),
    )
    assert adjust._quantile_count(plan) == expected
