"""Test end-to-end run_pipeline() với dữ liệu toy: train+adjust thật, qua xsdba."""

from __future__ import annotations

import copy

import numpy as np

from bcflow.pipeline import run_pipeline


def test_both_variables_succeed(toy_config):
    out = run_pipeline(toy_config)

    assert all(c.status != "fail" for c in out["qa"]), [c.as_dict() for c in out["qa"] if c.status == "fail"]
    assert out["results"]["tasmax"] is not None
    assert out["results"]["pr"] is not None


def test_tasmax_adjustment_moves_mean_toward_ref(toy_config):
    """Sau hiệu chỉnh, trung bình scen phải gần ref hơn hẳn so với trước (sanity check thống kê)."""
    from bcflow.config_schema import load_config
    from bcflow.adjust import run_variable
    from bcflow.loaders import load_ref_points

    cfg = load_config(toy_config)
    qa = []
    ref = load_ref_points(cfg.variables[0].data.ref, qa, "degC")
    scen, _adj = run_variable(cfg.variables[0], qa)

    ref_mean = ref.mean("time").values
    scen_mean = scen.mean("time").values
    # Bias cố ý đưa vào hist/sim trong make_toy_data.py là +1.8/+2.1 độ.
    # Sau hiệu chỉnh, sai khác với ref phải nhỏ hơn 0.5 độ ở mọi trạm.
    assert np.all(np.abs(scen_mean - ref_mean) < 0.5), (ref_mean, scen_mean)


def test_pr_adapt_freq_reduces_dry_day_mismatch(toy_config):
    """adapt_freq phải làm tỷ lệ ngày khô (mùa mưa) của scen gần ref hơn so với sim gốc."""
    from bcflow.config_schema import load_config
    from bcflow.adjust import run_variable
    from bcflow.loaders import load_raster_variable, load_ref_points
    from bcflow.extract import extract_nearest

    cfg = load_config(toy_config)
    qa = []
    ref = load_ref_points(cfg.variables[1].data.ref, qa, "mm/day")
    sim_raw = load_raster_variable(cfg.variables[1].data.sim, "pr")
    sim_raw_at_points = extract_nearest(sim_raw, ref, cfg.variables[1].data.sim, qa, source_label="pr:sim_raw")

    scen, _adj = run_variable(cfg.variables[1], qa)

    wet_months = slice("2010-08-01", "2010-09-30")
    dry_ref = (ref.sel(time=wet_months).values == 0).mean(axis=0)
    dry_sim = (sim_raw_at_points.sel(time=wet_months).values == 0).mean(axis=0)
    dry_scen = (scen.sel(time=wet_months).values == 0).mean(axis=0)

    # Khoảng cách tỷ lệ ngày khô giữa scen và ref phải không lớn hơn khoảng cách
    # giữa sim gốc (chưa hiệu chỉnh) và ref, trung bình trên các trạm.
    assert np.mean(np.abs(dry_scen - dry_ref)) <= np.mean(np.abs(dry_sim - dry_ref)) + 1e-9


def test_one_bad_variable_does_not_break_the_others(toy_config):
    """Method sai ở 1 biến -> chỉ biến đó fail, biến còn lại vẫn train+adjust thành công."""
    bad = copy.deepcopy(toy_config)
    bad["variables"][0]["method"] = "khong_ton_tai"

    out = run_pipeline(bad)

    assert out["results"]["tasmax"] is None
    assert out["results"]["pr"] is not None
    assert any(c.status == "fail" and "method" in c.name for c in out["qa"])


def test_adapt_freq_true_missing_threshold_fails_clearly(toy_config):
    """adapt_freq=true thiếu threshold -> chặn sớm ở pydantic, message rõ ràng."""
    bad = copy.deepcopy(toy_config)
    del bad["variables"][1]["adapt_freq_thresh"]
    bad["variables"] = [bad["variables"][1]]  # chỉ giữ biến pr

    out = run_pipeline(bad)

    assert out["results"]["pr"] is None
    assert any("adapt_freq_thresh" in c.message for c in out["qa"] if c.status == "fail")
