"""A wrong config must be rejected when it is validated, before any data file is opened."""

from __future__ import annotations

import copy
from typing import get_args

import pytest
from pydantic import ValidationError

from bcflow.config_schema import VariableName, load_config
from bcflow.discovery import get_method_meta
from bcflow.variables import DEFAULTS, VARIABLES, VariableSpec, resolve, resolve_method_params


def test_valid_config_loads(toy_config):
    cfg = load_config(toy_config)
    assert cfg.variables == ["tmax", "precip"]
    assert cfg.spec("precip").adapt_freq_thresh == "0.5 mm d-1"


def test_variable_names_match_the_defaults():
    assert set(get_args(VariableName)) == set(VARIABLES) == set(DEFAULTS)


def test_spec_merges_defaults_and_overrides(toy_config):
    cfg = load_config(toy_config)
    precip, tmax = cfg.spec("precip"), cfg.spec("tmax")
    # overridden
    assert precip.group == "none" and precip.method == "empirical_quantile_mapping"
    assert tmax.group == "dayofyear" and tmax.window == 15
    # kept from the defaults
    assert precip.kind == "*" and precip.adapt_freq is True and precip.bounds == (0.0, None)
    assert tmax.kind == "+" and tmax.units == "degC"


def test_defaults_alone_are_valid(toy_config):
    raw = copy.deepcopy(toy_config)
    del raw["correction"]
    cfg = load_config(raw)
    assert cfg.spec("precip").adapt_freq_thresh == "1 mm d-1"
    assert cfg.spec("tmax").group == "month"


def test_model_path_replaces_var_placeholder(toy_config):
    cfg = load_config(toy_config)
    assert cfg.model.path("hist", "precip").endswith("model_hist_pr.nc")
    assert cfg.model.path("sim", "tmax").endswith("model_sim_tasmax.nc")


def test_unknown_variable_rejected(toy_config):
    bad = copy.deepcopy(toy_config)
    bad["variables"] = ["tmax", "rain"]
    with pytest.raises(ValidationError):
        load_config(bad)


def test_variable_without_var_map_entry_rejected(toy_config):
    bad = copy.deepcopy(toy_config)
    bad["variables"] = ["tmax", "precip", "tmin"]
    with pytest.raises(ValidationError, match="var_map"):
        load_config(bad)


def test_duplicated_variable_rejected(toy_config):
    bad = copy.deepcopy(toy_config)
    bad["variables"] = ["tmax", "tmax"]
    with pytest.raises(ValidationError, match="more than once"):
        load_config(bad)


def test_correction_for_unlisted_variable_rejected(toy_config):
    bad = copy.deepcopy(toy_config)
    bad["variables"] = ["tmax"]
    with pytest.raises(ValidationError, match="precip"):
        load_config(bad)


def test_adapt_freq_without_threshold_rejected(toy_config):
    bad = copy.deepcopy(toy_config)
    bad["correction"]["tmax"]["adapt_freq"] = True  # tmax has no default threshold
    with pytest.raises(ValidationError, match="adapt_freq_thresh"):
        load_config(bad)


@pytest.mark.parametrize(
    "override",
    [{"group": "season"}, {"window": 0}, {"kind": "+"}],  # kind is fixed per variable
)
def test_invalid_correction_rejected(toy_config, override):
    bad = copy.deepcopy(toy_config)
    bad["correction"]["tmax"].update(override)
    with pytest.raises(ValidationError):
        load_config(bad)


def test_file_pattern_needs_a_placeholder(toy_config):
    bad = copy.deepcopy(toy_config)
    bad["stations"]["file_pattern"] = "station.csv"
    with pytest.raises(ValidationError, match="file_pattern"):
        load_config(bad)


def test_search_radius_is_bounded(toy_config):
    bad = copy.deepcopy(toy_config)
    bad["model"]["search_radius"] = 6
    with pytest.raises(ValidationError):
        load_config(bad)


def test_old_match_key_is_rejected(toy_config):
    bad = copy.deepcopy(toy_config)
    bad["stations"]["match_key"] = "name"
    with pytest.raises(ValidationError):
        load_config(bad)


def test_multiplicative_variable_requires_non_negative_lower_bound():
    with pytest.raises(ValueError, match="non-negative lower bound"):
        VariableSpec(name="x", units="1", kind="*", bounds=(None, None))
    with pytest.raises(ValueError, match="non-negative lower bound"):
        VariableSpec(name="x", units="1", kind="*", bounds=(-0.1, None))
    with pytest.raises(ValueError, match="kind must be"):
        VariableSpec(name="x", units="1", kind="/", bounds=(0.0, None))


def test_resolve_method_params_parity():
    train_qdm, adjust_qdm = resolve_method_params(
        DEFAULTS["precip"], get_method_meta("quantile_delta_mapping")
    )
    assert train_qdm == {"nquantiles": 50}
    assert adjust_qdm == {"interp": "nearest", "extrapolation": "constant"}

    train_eqm, adjust_eqm = resolve_method_params(
        DEFAULTS["tmax"], get_method_meta("empirical_quantile_mapping")
    )
    assert train_eqm == {"nquantiles": 50}
    assert adjust_eqm == {"interp": "linear", "extrapolation": "constant"}

    train_sc, adjust_sc = resolve_method_params(
        DEFAULTS["precip"], get_method_meta("scaling")
    )
    assert train_sc == {}
    assert adjust_sc == {}
    assert "nquantiles" not in adjust_sc
    assert "extrapolation" not in adjust_sc
    assert "interp" not in adjust_sc


def test_resolve_method_params_explicit_adjust_on_scaling_passes_through():
    spec = resolve("precip", {"method": "scaling", "adjust_params": {"interp": "linear"}})
    train, adjust = resolve_method_params(spec, get_method_meta("scaling"))
    assert train == {}
    assert adjust == {"interp": "linear"}

    _, explicit_adjust = resolve_method_params(
        DEFAULTS["precip"],
        get_method_meta("scaling"),
        explicit_adjust={"interp": "nearest"},
    )
    assert explicit_adjust == {"interp": "nearest"}
