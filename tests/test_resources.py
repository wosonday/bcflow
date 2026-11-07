"""Deterministic resource planning tests; never inspect real host limits."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from bcflow import resources

GiB = 1024**3


@pytest.fixture(autouse=True)
def host(monkeypatch):
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


@pytest.mark.parametrize("cpus,expected", [(1, 1), (2, 1), (4, 3), (8, 7), (16, 15)])
def test_reserves_one_cpu(host, cpus, expected):
    host.cpus = cpus
    selection = resources.select_workers("auto", 1.0, 100, GiB)
    assert selection.cpu_limit == expected
    assert selection.workers == expected


@pytest.mark.parametrize(
    "requested,remaining,available,expected",
    [
        (2, 100, 32 * GiB, 2),
        (100, 100, 32 * GiB, 15),
        (100, 100, 4 * GiB, 2),
        (100, 3, 32 * GiB, 3),
        ("auto", 1, 32 * GiB, 1),
        ("auto", 0, 32 * GiB, 0),
    ],
)
def test_all_caps_apply_to_manual_and_auto(
    host, requested, remaining, available, expected
):
    host.available = available
    selection = resources.select_workers(requested, 0.5, remaining, GiB)
    assert selection.workers == expected


def test_selection_fields_and_frozen_dataclass():
    selection = resources.select_workers("auto", 0.5, 100, 2 * GiB)
    assert selection == resources.WorkerSelection(
        8, 15, 8, 32 * GiB, 16 * GiB, 2 * GiB, False
    )
    with pytest.raises(FrozenInstanceError):
        selection.workers = 10  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.parametrize("available", [0, GiB // 2, GiB])
def test_insufficient_ram_keeps_one_and_flags_over_budget(host, available):
    host.available = available
    selection = resources.select_workers(10, 0.5, 10, GiB)
    assert selection.workers == selection.memory_limit == 1
    assert selection.available_bytes == available
    assert selection.budget_bytes == int(available * 0.5)
    assert selection.over_budget


def test_task_exactly_fits_budget(host):
    host.available = 2 * GiB
    selection = resources.select_workers("auto", 0.5, 10, GiB)
    assert selection.workers == 1
    assert not selection.over_budget


def test_budget_and_memory_limit_round_down(host):
    host.available = 101
    selection = resources.select_workers("auto", 0.5, 100, 13)
    assert selection.budget_bytes == 50
    assert selection.memory_limit == selection.workers == 3


def test_unknown_ram_falls_back_to_one(monkeypatch):
    def unavailable():
        raise OSError("RAM query unavailable")

    monkeypatch.setattr(resources.psutil, "virtual_memory", unavailable)
    selection = resources.select_workers("auto", 0.8, 100, GiB)
    assert selection.workers == selection.memory_limit == 1
    assert selection.available_bytes == selection.budget_bytes == 0
    assert selection.over_budget


@pytest.mark.parametrize(
    "limit_file,usage_file",
    [
        ("memory.max", "memory.current"),
        ("memory/memory.limit_in_bytes", "memory/memory.usage_in_bytes"),
    ],
)
def test_cgroup_v2_and_v1_headroom(host, limit_file, usage_file):
    host.files = {
        f"/sys/fs/cgroup/{limit_file}": str(10 * GiB),
        f"/sys/fs/cgroup/{usage_file}": str(4 * GiB),
    }
    selection = resources.select_workers("auto", 0.5, 100, GiB)
    assert selection.available_bytes == 6 * GiB
    assert selection.budget_bytes == 3 * GiB
    assert selection.memory_limit == selection.workers == 3


@pytest.mark.parametrize("v2", [True, False])
def test_nested_cgroup_and_ancestor_limits(host, v2):
    base = "/sys/fs/cgroup" if v2 else "/sys/fs/cgroup/memory"
    limit, usage = (
        ("memory.max", "memory.current")
        if v2
        else ("memory.limit_in_bytes", "memory.usage_in_bytes")
    )
    host.files = {
        "/proc/self/cgroup": "0::/team/job" if v2 else "5:cpu,memory:/team/job",
        f"{base}/team/job/{limit}": str(12 * GiB),
        f"{base}/team/job/{usage}": str(2 * GiB),
        f"{base}/team/{limit}": str(8 * GiB),
        f"{base}/team/{usage}": str(5 * GiB),
    }
    assert resources.select_workers("auto", 1.0, 100, GiB).available_bytes == 3 * GiB


@pytest.mark.parametrize(
    "limit,usage",
    [("max", "100"), ("bad", "0"), ("100", "bad"), ("-1", "0"), ("100", "-1")],
)
def test_unlimited_or_malformed_cgroups_are_ignored(host, limit, usage):
    host.files = {
        "/sys/fs/cgroup/memory.max": limit,
        "/sys/fs/cgroup/memory.current": usage,
    }
    assert resources.select_workers("auto", 1.0, 100, GiB).available_bytes == 32 * GiB


def test_exhausted_cgroup_has_zero_headroom(host):
    host.files = {
        "/sys/fs/cgroup/memory.max": "100",
        "/sys/fs/cgroup/memory.current": "101",
    }
    selection = resources.select_workers("auto", 1.0, 100, GiB)
    assert selection.available_bytes == 0
    assert selection.workers == 1
    assert selection.over_budget


def test_cgroup_inactive_file_cache_counts_as_available(host):
    """Page cache the kernel can drop must not block workers (WSL/Docker)."""
    host.files = {
        "/sys/fs/cgroup/memory.max": str(10 * GiB),
        "/sys/fs/cgroup/memory.current": str(8 * GiB),
        "/sys/fs/cgroup/memory.stat": f"anon {2 * GiB}\ninactive_file {5 * GiB}\n",
    }
    assert resources.select_workers("auto", 1.0, 100, GiB).available_bytes == 7 * GiB


def test_cgroup_never_increases_host_available_ram(host):
    host.files = {
        "/sys/fs/cgroup/memory.max": str(100 * GiB),
        "/sys/fs/cgroup/memory.current": "0",
    }
    assert resources.select_workers("auto", 1.0, 100, GiB).available_bytes == 32 * GiB


@pytest.mark.parametrize("requested", [0, -1, "other", True])
def test_invalid_requested_workers(requested):
    with pytest.raises(ValueError, match="requested"):
        resources.select_workers(requested, 0.5, 10, GiB)


@pytest.mark.parametrize("fraction", [0, -0.5, 1.1, float("nan"), float("inf")])
def test_invalid_memory_fraction(fraction):
    with pytest.raises(ValueError, match="memory_fraction"):
        resources.select_workers("auto", fraction, 10, GiB)


def test_invalid_task_or_remaining_blocks():
    with pytest.raises(ValueError, match="estimated_task_bytes"):
        resources.select_workers("auto", 0.5, 10, 0)
    with pytest.raises(ValueError, match="remaining_blocks"):
        resources.select_workers("auto", 0.5, -1, GiB)


def test_estimate_baseline_and_exact_formula():
    baseline = 768 * 1024**2
    assert resources.estimate_task_bytes(0, 0, 0, 0) == baseline
    expected = (1000 + 2000 + 3000) * 10000 * 8 * 4 + 10000 * 50 * 12 * 8 * 3 + baseline
    assert resources.estimate_task_bytes(1000, 2000, 3000, 10000) == expected


def test_estimate_matches_the_measured_grid_block():
    """Measured peak RSS: 1.19 GiB for 287 cells, 1.79 GiB for 680 cells
    (CNRM-ESM2-1 + OBS.Vietnam tmax, 1980-2014 reference, 2015-2100 sim)."""
    for cells, measured in ((287, 1.19 * GiB), (680, 1.79 * GiB)):
        estimate = resources.estimate_task_bytes(12784, 12784, 31411, cells)
        assert measured * 0.9 <= estimate <= measured * 1.3


@pytest.mark.parametrize(
    "dimension",
    ["ref_steps", "hist_steps", "sim_steps", "cells", "nquantiles", "groups"],
)
def test_estimate_scales_with_every_dimension(dimension):
    dimensions = dict(
        ref_steps=1000,
        hist_steps=2000,
        sim_steps=3000,
        cells=10000,
        nquantiles=50,
        groups=12,
    )
    baseline = resources.estimate_task_bytes(**dimensions)
    dimensions[dimension] *= 2
    assert resources.estimate_task_bytes(**dimensions) > baseline


def test_estimate_rejects_negative_dimensions():
    with pytest.raises(ValueError, match="nonnegative"):
        resources.estimate_task_bytes(-1, 100, 100, 10)
