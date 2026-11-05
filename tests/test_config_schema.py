"""Config sai phải bị chặn NGAY khi nạp (pydantic), trước khi mở file dữ liệu."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from bcflow.config_schema import load_config


def test_valid_config_loads(toy_config):
    cfg = load_config(toy_config)
    assert len(cfg.variables) == 2
    assert cfg.variables[1].adapt_freq_thresh == "0.5 mm d-1"


def test_adapt_freq_true_without_threshold_rejected(toy_config):
    bad = copy.deepcopy(toy_config)
    del bad["variables"][1]["adapt_freq_thresh"]
    with pytest.raises(ValidationError, match="adapt_freq_thresh"):
        load_config(bad)


def test_both_hist_sim_and_split_rejected(toy_config):
    bad = copy.deepcopy(toy_config)
    bad["variables"][0]["data"]["hist_sim"] = {
        "format": "netcdf", "path": "x.nc",
        "hist_period": ["2000-01-01", "2010-12-31"],
        "sim_period": ["2011-01-01", "2020-12-31"],
    }
    with pytest.raises(ValidationError, match="hist_sim"):
        load_config(bad)


def test_no_source_at_all_rejected(toy_config):
    bad = copy.deepcopy(toy_config)
    del bad["variables"][0]["data"]["hist"]
    del bad["variables"][0]["data"]["sim"]
    with pytest.raises(ValidationError):
        load_config(bad)


def test_invalid_kind_rejected(toy_config):
    bad = copy.deepcopy(toy_config)
    bad["variables"][0]["kind"] = "x"  # chỉ "+" hoặc "*" hợp lệ
    with pytest.raises(ValidationError):
        load_config(bad)
