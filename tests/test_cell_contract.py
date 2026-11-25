"""Tests for the 7-step cell execution contract in src/bcflow/cell.py."""

from __future__ import annotations

import importlib.util
import pickle
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import xarray as xr
import xsdba
import xsdba.adjustment as xsdba_adj

from bcflow.cell import STACK_DIM, CellPlan, CellResult, PostStep, run_cell
from bcflow.preprocess import SEED
from bcflow.progress import RunCancelled, RunControl
from bcflow.qa import QACheck
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

    monkeypatch.setattr(xsdba_adj.OTC, "adjust", classmethod(lambda cls, ref_a, hist_a, **kw: fake_otc_adjust(ref_a, hist_a, **kw)))

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
    res_for_ipc = CellResult(
        scen=res.scen,
        trained_ds=res.trained_ds,
        qa=res.qa,
        timing=res.timing,
        clip_counts=res.clip_counts,
        trained_obj=None,
    )
    res_rt = pickle.loads(pickle.dumps(res_for_ipc))
    xr.testing.assert_identical(res_rt.scen, res.scen)
    xr.testing.assert_identical(res_rt.trained_ds, res.trained_ds)
    assert res_rt.qa == res.qa
    assert res_rt.timing == res.timing
    assert res_rt.clip_counts == res.clip_counts
    assert res_rt.trained_obj is None
