"""Process-grid equivalence and deterministic resource-aware batch contracts."""

from __future__ import annotations

import copy
from concurrent.futures import ProcessPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from dask.base import compute as dask_compute
from pydantic import ValidationError

from bcflow import adjust
from bcflow.config_schema import GridReferenceConfig, load_config
from bcflow.pipeline import run_pipeline
from bcflow.progress import RunControl
from bcflow.regrid import TargetGrid
from bcflow.resources import WorkerSelection


def _selection(workers, estimated, *, memory_limit=None):
    memory_limit = workers if memory_limit is None else memory_limit
    budget = memory_limit * estimated
    return WorkerSelection(
        workers=workers,
        cpu_limit=8,
        memory_limit=memory_limit,
        available_bytes=budget * 2,
        budget_bytes=budget,
        estimated_task_bytes=estimated,
        over_budget=False,
    )


@pytest.mark.parametrize("variable", ["tmax", "precip"])
def test_two_process_grid_matches_sequential(grid_config, monkeypatch, variable):
    raw = copy.deepcopy(grid_config)
    raw["variables"] = [variable]
    raw["correction"] = {variable: raw["correction"][variable]}
    raw["grid"].update(region={"lat": [15, 16], "lon": [105, 107]}, block_size=3)
    raw["periods"] = {"calibration": ["2000", "2001"], "validation": ["2002", "2002"]}
    # Slice in the parent: workers still execute the real correction on full
    # (selected) time series, with no worker-side mocks or open file handles.
    open_reference = adjust.open_grid_reference
    load_model = adjust.load_model_variable

    def short_reference(*args, **kwargs):
        ref, lat, lon = open_reference(*args, **kwargs)
        return ref.sel(time=slice("2000", "2002")), lat, lon

    def short_model(*args, **kwargs):
        return load_model(*args, **kwargs).sel(time=slice("2000", "2002"))

    def select(requested, fraction, remaining, estimated):
        assert fraction == 0.6
        return _selection(min(requested, remaining), estimated)

    monkeypatch.setattr(adjust, "open_grid_reference", short_reference)
    monkeypatch.setattr(adjust, "load_model_variable", short_model)
    monkeypatch.setattr(adjust, "select_workers", select)
    outputs, event_lists = [], []
    for workers in (1, 2):
        raw["grid"]["workers"] = workers
        events = []
        qa = []
        result = adjust.run_variable(
            load_config(raw), variable, qa, control=RunControl(events.append)
        )
        assert not [check.message for check in qa if check.status == "fail"]
        outputs.append(result)
        event_lists.append(events)

    sequential, parallel = outputs
    assert sequential["grid"].size == parallel["grid"].size == 8
    for events in event_lists:
        starts = [e for e in events if e.get("stage") == "block_started"]
        assert [e["block_cells"] for e in starts] == [3, 3, 2]
        assert [e["block_index"] for e in starts] == [1, 2, 3]
    assert [
        e["workers"] for e in event_lists[1] if e.get("stage") == "worker_selection"
    ] == [2, 1]
    xr.testing.assert_identical(sequential["scen"].coords, parallel["scen"].coords)
    xr.testing.assert_allclose(sequential["scen"], parallel["scen"])
    xr.testing.assert_allclose(sequential["adj"].ds, parallel["adj"].ds)
    assert sequential["evaluation"] is not None
    pd.testing.assert_frame_equal(sequential["evaluation"], parallel["evaluation"])
    if sequential["hist_eval"] is not None:
        xr.testing.assert_allclose(sequential["hist_eval"], parallel["hist_eval"])


@pytest.fixture
def mock_grid(monkeypatch):
    """Seven cells make two parallel batches and a final sequential block."""
    raw = {
        "variables": ["tmax"],
        "grid": {
            "path": "unused.nc",
            "var_map": {"tmax": "tasmax"},
            "workers": "auto",
            "memory_fraction": 0.6,
            "block_size": 2,
        },
        "model": {
            "hist": "hist_{var}.nc",
            "sim": "sim_{var}.nc",
            "var_map": {"tmax": "tasmax"},
        },
    }
    target = TargetGrid(
        np.array([15.0]), np.arange(7.0), np.zeros(7, dtype=int), np.arange(7)
    )
    reference = xr.DataArray(
        np.ones((3, 1, 7)),
        dims=("time", "lat", "lon"),
        coords={
            "time": pd.date_range("2000-01-01", periods=3),
            "lat": target.lat,
            "lon": target.lon,
        },
        attrs={"units": "degC"},
    )
    monkeypatch.setattr(
        adjust, "open_grid_reference", lambda *a, **k: (reference, "lat", "lon")
    )
    monkeypatch.setattr(
        adjust,
        "scan_grid",
        lambda *a, **k: (np.ones((1, 7), dtype=bool), np.full((1, 7), 3), 3),
    )
    monkeypatch.setattr(adjust, "grid_target", lambda *a: target)
    monkeypatch.setattr(adjust, "load_model_variable", lambda *a: reference)

    def points(ref, block, *args, **kwargs):
        return xr.DataArray(
            np.broadcast_to(block.ix, (3, block.size)).copy(),
            dims=("time", "station"),
            coords={"time": reference.time, "station": block.ids},
            attrs={"units": "degC"},
        )

    monkeypatch.setattr(adjust, "load_grid_points", points)
    monkeypatch.setattr(adjust, "regrid_to_points", points)
    corrected = []

    def correct(cfg, spec, ref, hist, sim, qa, **kwargs):
        assert kwargs["compact"] is True
        xr.testing.assert_identical(ref, hist)
        xr.testing.assert_identical(ref, sim)
        ids = ref.station.values.tolist()
        corrected.append(ids)
        return {"ids": ids, "adj": SimpleNamespace(ds=xr.Dataset())}

    monkeypatch.setattr(adjust, "_correct", correct)
    finish = Mock(
        side_effect=lambda cfg, spec, parts, qa, **kw: {
            "parts": [p["ids"] for p in parts]
        }
    )
    monkeypatch.setattr(adjust, "_finish", finish)
    return raw, target, corrected, finish


def test_batches_reselect_memory_and_preserve_order(mock_grid, monkeypatch):
    raw, target, corrected, finish = mock_grid
    trace, calls, selections, events = [], [], [], []
    pools: set[int] = set()

    def select(requested, fraction, remaining, estimated):
        assert requested == "auto"
        assert fraction == 0.6
        assert estimated > 0
        # Free memory drops after the first batch; cap subsequent work at one.
        workers = 2 if not selections else 1
        selection = _selection(workers, estimated)
        selections.append(selection)
        calls.append(remaining)
        trace.append("select")
        return selection

    def synchronous(*tasks, **kwargs):
        assert kwargs["scheduler"] == "processes" and kwargs["chunksize"] == 1
        assert isinstance(kwargs["pool"], ProcessPoolExecutor)
        pools.add(id(kwargs["pool"]))
        assert len(tasks) == 2
        trace.append("compute")
        return dask_compute(*tasks, scheduler="synchronous")

    monkeypatch.setattr(adjust, "select_workers", select)
    compute = Mock(side_effect=synchronous)
    monkeypatch.setattr(adjust, "compute", compute)
    cfg = load_config(raw)
    result = adjust.run_variable(cfg, "tmax", [], control=RunControl(events.append))
    expected = [target.ids[i : i + 2] for i in range(0, 7, 2)]
    assert result == {"parts": expected}
    # Execution order is scheduler-dependent; returned parts must remain ordered.
    assert sorted(corrected) == sorted(expected)
    assert calls == [4, 2, 1]
    assert trace == ["select", "compute", "select", "select"]
    compute.assert_called_once()
    finish.assert_called_once()
    finishes = [e for e in events if e.get("stage") == "block_finished"]
    assert [e["block_index"] for e in finishes] == [1, 2, 3, 4]
    assert [e["completed_cells"] for e in finishes] == [2, 4, 6, 7]
    resource_events = [e for e in events if e.get("stage") == "worker_selection"]
    for event, selection in zip(resource_events, selections, strict=True):
        for field in (
            "workers",
            "cpu_limit",
            "memory_limit",
            "available_bytes",
            "budget_bytes",
            "estimated_task_bytes",
            "over_budget",
        ):
            assert event[field] == getattr(selection, field)


@pytest.mark.parametrize("cancel_batch", [1, 2])
def test_cancel_at_compute_boundary_discards_current_variable(
    mock_grid, monkeypatch, cancel_batch
):
    raw, _target, corrected, finish = mock_grid
    events = []
    control = RunControl(events.append)
    select = Mock(
        side_effect=lambda requested, fraction, remaining, estimated: _selection(
            min(2, remaining), estimated
        )
    )
    monkeypatch.setattr(adjust, "select_workers", select)
    batches = []
    pools: set[int] = set()

    def synchronous(*tasks, **kwargs):
        assert kwargs["scheduler"] == "processes" and kwargs["chunksize"] == 1
        assert isinstance(kwargs["pool"], ProcessPoolExecutor)
        pools.add(id(kwargs["pool"]))
        parts = dask_compute(*tasks, scheduler="synchronous")
        batches.append(parts)
        if len(batches) == cancel_batch:
            control.request_cancel()
        return parts

    monkeypatch.setattr(adjust, "compute", synchronous)
    out = run_pipeline(raw, control=control)
    assert out["cancelled"] is True
    assert out["results"] == {}
    assert out["variable_statuses"] == {"tmax": "cancelled"}
    assert len(batches) == select.call_count == cancel_batch
    assert len(pools) == 1  # one pool, reused by every batch
    assert len(corrected) == cancel_batch * 2
    finish.assert_not_called()
    finishes = [e for e in events if e.get("stage") == "block_finished"]
    assert [e["block_index"] for e in finishes] == list(
        range(1, (cancel_batch - 1) * 2 + 1)
    )
    assert not any(e.get("stage") == "combining" for e in events)
    assert not any(c.status == "fail" for c in out["qa"])
    assert events[-1]["event"] == "run_cancelled"


@pytest.mark.parametrize("workers", [0, -1, True, False, "2", "invalid", 1.0])
def test_grid_schema_rejects_invalid_workers(workers):
    with pytest.raises(ValidationError, match="workers"):
        GridReferenceConfig(
            path="unused.nc", var_map={"tmax": "tasmax"}, workers=workers
        )


@pytest.mark.parametrize("fraction", [0, -0.1, 1.01, float("nan")])
def test_grid_schema_rejects_invalid_memory_fraction(fraction):
    with pytest.raises(ValidationError, match="memory_fraction"):
        GridReferenceConfig(
            path="unused.nc", var_map={"tmax": "tasmax"}, memory_fraction=fraction
        )


def test_grid_schema_resource_defaults():
    cfg = GridReferenceConfig(path="unused.nc", var_map={"tmax": "tasmax"})
    assert cfg.workers == "auto"
    assert cfg.memory_fraction == 0.6


@pytest.mark.parametrize("workers", ["auto", 1, 2])
@pytest.mark.parametrize("fraction", [0.6, 1.0])
def test_grid_schema_accepts_resource_options(workers, fraction):
    cfg = GridReferenceConfig(
        path="unused.nc",
        var_map={"tmax": "tasmax"},
        workers=workers,
        memory_fraction=fraction,
    )
    assert cfg.workers == workers
    assert cfg.memory_fraction == fraction
