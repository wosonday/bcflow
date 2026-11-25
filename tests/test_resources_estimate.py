"""Multivariate RAM estimation scaling; deterministic, never inspects the host."""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from bcflow import resources

GiB = 1024**3
BASELINE = 768 * 1024**2


@pytest.fixture(autouse=True)
def host(monkeypatch):
    """Same deterministic host as test_resources.py, so select_workers is stable."""
    state = SimpleNamespace(cpus=16, available=32 * GiB, files={})
    monkeypatch.setattr(resources.dask.system, "cpu_count", lambda: state.cpus)
    monkeypatch.setattr(
        resources.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=state.available),
    )

    def read_text(path, **kwargs):
        try:
            return state.files[str(path)]
        except KeyError:
            raise FileNotFoundError(str(path)) from None

    monkeypatch.setattr(Path, "read_text", read_text)
    return state


def _univariate_bytes(ref_steps=1000, hist_steps=2000, sim_steps=3000, cells=10000):
    """The pre-multivariate formula, computed with exact integer arithmetic."""
    inputs = (ref_steps + hist_steps + sim_steps) * cells * 8
    quantiles = cells * 50 * 12 * 8 * 3
    return inputs * 4 + quantiles


@pytest.mark.parametrize("cells", [0, 1, 287, 10000])
def test_defaults_match_explicit_univariate_arguments(cells):
    default = resources.estimate_task_bytes(1000, 2000, 3000, cells)
    explicit = resources.estimate_task_bytes(
        1000, 2000, 3000, cells, n_vars=1, method_factor=1.0
    )
    assert default == explicit
    assert default == _univariate_bytes(cells=cells) + BASELINE


def test_baseline_is_paid_once_not_per_variable_or_method():
    baseline_only = resources.estimate_task_bytes(0, 0, 0, 0)
    assert baseline_only == BASELINE
    assert resources.estimate_task_bytes(0, 0, 0, 0, n_vars=4) == BASELINE
    assert (
        resources.estimate_task_bytes(0, 0, 0, 0, method_factor=2.0) == BASELINE
    )


@pytest.mark.parametrize("n_vars", [2, 3])
def test_monotonic_scaling_with_n_vars_keeps_baseline_unmultiplied(n_vars):
    single = resources.estimate_task_bytes(1000, 2000, 3000, 10000)
    scaled = resources.estimate_task_bytes(1000, 2000, 3000, 10000, n_vars=n_vars)
    assert scaled == _univariate_bytes() * n_vars + BASELINE
    assert scaled > single
    # If the baseline were multiplied, the delta would include it.
    assert scaled - BASELINE == n_vars * (single - BASELINE)


def test_method_memory_factor_exactly_matches_the_registry():
    for method in ("mbcn", "npdf_transform", "otc", "dotc"):
        assert resources.method_memory_factor(method) == 2.0
    assert resources.method_memory_factor("principal_components") == 1.5
    for method in (
        "quantile_delta_mapping",
        "empirical_quantile_mapping",
        "detrended_quantile_mapping",
        "scaling",
        "loci",
        "not_a_method",
    ):
        assert resources.method_memory_factor(method) == 1.0


@pytest.mark.parametrize(
    "method, factor", [("mbcn", 2.0), ("npdf_transform", 2.0), ("principal_components", 1.5)]
)
def test_monotonic_scaling_with_method_factor_keeps_baseline_unmultiplied(method, factor):
    assert resources.method_memory_factor(method) == factor
    single = resources.estimate_task_bytes(1000, 2000, 3000, 10000)
    scaled = resources.estimate_task_bytes(
        1000, 2000, 3000, 10000, method_factor=resources.method_memory_factor(method)
    )
    assert scaled > single
    assert scaled - BASELINE == int((single - BASELINE) * factor)


def test_n_vars_and_method_factor_compose():
    both = resources.estimate_task_bytes(
        1000, 2000, 3000, 10000, n_vars=2, method_factor=2.0
    )
    assert both == _univariate_bytes() * 4 + BASELINE
    assert both - BASELINE == 4 * (
        resources.estimate_task_bytes(1000, 2000, 3000, 10000) - BASELINE
    )


@pytest.mark.parametrize("n_vars", [0, -1, True, False, 1.5, "2"])
def test_invalid_n_vars(n_vars):
    with pytest.raises(ValueError, match="n_vars"):
        resources.estimate_task_bytes(100, 100, 100, 10, n_vars=n_vars)


@pytest.mark.parametrize("method_factor", [0, -1, float("nan"), float("inf"), -0.5])
def test_invalid_method_factor(method_factor):
    with pytest.raises(ValueError, match="method_factor"):
        resources.estimate_task_bytes(100, 100, 100, 10, method_factor=method_factor)


def test_negative_dimensions_still_rejected_first():
    with pytest.raises(ValueError, match="nonnegative"):
        resources.estimate_task_bytes(-1, 100, 100, 10, n_vars=0)


def test_select_workers_reduces_concurrency_as_load_grows(host):
    budget = int(host.available * 0.5)
    loads = ((1, 1.0), (2, 1.0), (2, 2.0), (4, 2.0))
    estimates = [
        resources.estimate_task_bytes(
            1000, 2000, 3000, 5000, n_vars=n_vars, method_factor=factor
        )
        for n_vars, factor in loads
    ]
    selections = [
        resources.select_workers("auto", 0.5, 100, estimate) for estimate in estimates
    ]
    workers = [selection.workers for selection in selections]

    assert workers == sorted(workers, reverse=True)
    assert len(set(workers)) == len(workers)  # strictly less with every added load
    for selection, estimate in zip(selections, estimates, strict=True):
        assert selection.estimated_task_bytes == estimate
        assert selection.memory_limit == max(1, budget // estimate)
        assert selection.workers == min(selection.cpu_limit, selection.memory_limit, 100)


def test_method_factor_never_breaks_finite_estimates():
    estimate = resources.estimate_task_bytes(
        1, 1, 1, 1, method_factor=math.nextafter(0.0, 1.0)
    )
    assert isinstance(estimate, int) and estimate >= BASELINE
