"""Tests for runtime method registry (MethodSpec, METHODS, get_method, check_available)."""

from __future__ import annotations

import importlib.util

import pytest
import xsdba.adjustment

from bcflow.discovery import METHOD_CATALOG
from bcflow.methods import (
    METHOD_REGISTRY,
    METHODS,
    MethodSpec,
    check_available,
    get_method,
)


def test_all_catalog_methods_resolve_to_xsdba_classes():
    assert len(METHODS) == 11
    assert list(METHODS) == [m.name for m in METHOD_CATALOG]
    assert list(METHOD_REGISTRY) == [m.name for m in METHOD_CATALOG]

    for meta in METHOD_CATALOG:
        spec = get_method(meta.name)
        assert isinstance(spec, MethodSpec)
        expected_cls = getattr(xsdba.adjustment, meta.class_name)
        assert spec.cls is expected_cls
        assert METHOD_REGISTRY[meta.name] is expected_cls
        assert spec.meta is meta
        assert spec.name == meta.name
        assert spec.family == meta.family
        assert spec.min_vars == meta.min_vars
        assert spec.supports_projection == meta.supports_projection
        assert spec.native_adapt_freq == meta.native_adapt_freq
        assert spec.uses_quantiles == meta.uses_quantiles
        assert spec.extra == meta.extra
        assert spec.registry_defaults == meta.registry_defaults
        assert spec.default_train_params == meta.default_train_params
        assert spec.default_adjust_params == meta.default_adjust_params


def test_quantile_vs_non_quantile_defaults():
    for name in (
        "empirical_quantile_mapping",
        "detrended_quantile_mapping",
        "quantile_delta_mapping",
    ):
        spec = get_method(name)
        assert dict(spec.default_train_params) == {"nquantiles": 50}
        assert dict(spec.default_adjust_params) == {
            "interp": "linear",
            "extrapolation": "constant",
        }

    for name in (
        "scaling",
        "loci",
        "extreme_values",
        "principal_components",
        "mbcn",
        "npdf_transform",
        "otc",
        "dotc",
    ):
        spec = get_method(name)
        assert dict(spec.default_train_params) == {}
        assert dict(spec.default_adjust_params) == {}


def test_get_method_unknown_raises_value_error():
    with pytest.raises(ValueError, match="not registered"):
        get_method("unknown")


def test_check_available_pot_missing_and_present(monkeypatch):
    # Non-POT methods never require ot.
    qdm = get_method("quantile_delta_mapping")
    check_available(qdm)

    real_find_spec = importlib.util.find_spec

    for name in ("otc", "dotc"):
        spec = get_method(name)
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda mod: None if mod == "ot" else real_find_spec(mod),
        )
        with pytest.raises(ValueError, match="pip install POT"):
            check_available(spec)

        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda mod: object() if mod == "ot" else real_find_spec(mod),
        )
        check_available(spec)
