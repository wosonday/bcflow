"""Run the full correction for ONE variable or ONE multivariate target.

Steps, in order:
    1. resolve the variable/target settings: defaults + overrides
       (variables.py), and check the method name    (methods.py)
    2. load the reference, report the stations / cells with too few
       calibration days                             (loaders.py, coverage.py)
       and load the model hist/sim
    3. put the model on the reference: the grid cell nearest to each
       station (extract.py), or regridded onto the reference grid
       (regrid.py), one block of cells at a time to save memory
    4. bring all three to one unit                  (units.py)
       and onto one daily time axis                 (timeaxis.py)
    5. keep the calibration period for training     (config: periods)
    6. jitter exact zeros (precip), optional frequency adaptation, train,
       adjust sim (xsdba), then restore the zeros and clip to the
       variable's physical bounds
    7. apply the correction to hist over the calibration and validation
       periods, and evaluate before/after for each  (evaluate.py)

It knows nothing about running several targets/variables independently, or about
isolating one failure from another -- that is pipeline.py's job.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any, NoReturn, cast

import numpy as np
import pandas as pd
import pint
import xarray as xr
from dask.base import compute
from dask.delayed import delayed
from xsdba.units import convert_units_to

from .cell import CellPlan, PostStep, adjust_trained, run_cell
from .config_schema import PeriodsConfig, PipelineConfig, PostConfig, TargetConfig
from .coverage import check_coverage, coverage_table_from_counts, report_coverage
from .discovery import get_method_meta
from .evaluate import evaluate, obs_on_model_dates, paired_series, summarize
from .extract import extract_nearest
from .loaders import (
    grid_target,
    intersect_grid_masks,
    intersect_station_arrays,
    load_grid_points,
    load_model_variable,
    load_station_variable,
    open_grid_reference,
    read_grid_cells,
    scan_grid,
)
from .methods import METHODS, MethodSpec, check_available, get_method
from .preprocess import SEED, ZeroHandling
from .progress import RunControl
from .qa import QACheck
from .regrid import TargetGrid, model_box, regrid_to_points
from .resources import (
    WorkerPool,
    estimate_task_bytes,
    method_memory_factor,
    select_workers,
)
from .timeaxis import align_daily
from .units import harmonize
from .variables import VariableSpec, resolve_method_params

# Wet-day threshold for evaluating precipitation when the config gives none:
# 1 mm/d, the ETCCDI convention (Zhang et al., 2011, WIREs Climate Change
# 2(6), 851-870, doi:10.1002/wcc.147).
_DEFAULT_WET_DAY = "1 mm d-1"

# Fixed seed for the random steps (jitter of exact zeros, adapt_freq), so
# two runs on the same data write identical files.
_SEED = SEED


def _fail_qa(
    qa: list[QACheck],
    name: str,
    message: str,
    context: dict[str, Any],
    exc_msg: str | None = None,
) -> NoReturn:
    qa.append(QACheck(name=name, status="fail", message=message, context=context))
    raise ValueError(exc_msg or message)


def run_variable(
    cfg: PipelineConfig,
    variable: str,
    qa: list[QACheck],
    *,
    control: RunControl | None = None,
) -> dict:
    """Bias-correct one variable of a validated config.

    Returns {"scen": corrected sim (time, station),
             "adj": the trained xsdba object (its .ds is written to adj.nc),
             "evaluation": before/after table with a 'period' column, or None,
             "hist_eval": obs / raw hist / corrected hist (xr.Dataset) over the
                          calibration and validation periods, or None,
             "grid": the TargetGrid of a gridded reference, else None}.
    With a gridded reference, 'station' holds one id per grid cell.
    An optional control receives stage progress and cooperatively cancels
    at stage boundaries (and after each reference scan chunk).
    """
    if control is not None:
        control.check_cancelled()
    spec = cfg.spec(variable)
    override = cfg.correction.get(spec.name)
    target = TargetConfig.model_construct(
        name=variable,
        variables=[variable],
        method=spec.method,
        group=spec.group,
        window=spec.window,
        pool_dims=list(override.pool_dims) if override else [],
        train_params=dict(override.train_params or {}) if override else {},
        adjust_params=dict(override.adjust_params or {}) if override else {},
        post=list(override.post) if override else [],
    )
    return _run_target(cfg, target, qa, control=control, legacy=True)[variable]


def run_target(
    cfg: PipelineConfig,
    target: TargetConfig,
    qa: list[QACheck],
    *,
    control: RunControl | None = None,
) -> dict[str, dict]:
    """Bias-correct one target (univariate or multivariate) on stations or a grid.

    Returns ``{variable: result_dict}`` for every variable in ``target.variables``,
    matching the structure returned by ``run_variable()``.
    """
    return _run_target(cfg, target, qa, control=control, legacy=False)


def _run_target(
    cfg: PipelineConfig,
    target: TargetConfig,
    qa: list[QACheck],
    *,
    control: RunControl | None,
    legacy: bool,
) -> dict[str, dict]:
    if control is None:
        control = RunControl()
    control.check_cancelled()
    _validate_primary_method(
        target.name,
        target.method,
        qa,
        is_target=not legacy,
        variables=target.variables,
        group=target.group,
        post=target.post,
    )
    specs = {
        v: replace(cfg.spec(v), method=target.method, group=target.group, window=target.window)
        if len(target.variables) == 1 else cfg.spec(v)
        for v in target.variables
    }
    plan = _build_target_plan(target, specs)
    if cfg.grid is not None:
        return _run_grid(cfg, target, specs, qa, control=control, plan=plan)
    return _run_station(cfg, target, specs, qa, control=control, plan=plan)


def _execution_label(target: TargetConfig) -> str:
    return target.variables[0] if len(target.variables) == 1 else target.name


def _load_station_series(
    cfg: PipelineConfig,
    spec: VariableSpec,
    qa: list[QACheck],
    *,
    control: RunControl,
) -> tuple[xr.DataArray, xr.DataArray, xr.DataArray]:
    """Load station reference, check calibration coverage, and extract nearest model series."""
    variable, model = spec.name, cfg.model
    control.progress(variable, "loading_reference")
    ref = load_station_variable(cfg.stations, variable, spec.units, qa)  # type: ignore[arg-type]
    # Early warning, before the (slow) model files are opened.
    check_coverage(
        ref,
        cfg.periods.calibration,
        qa,
        name=variable,
        min_coverage=cfg.periods.min_coverage,
    )
    grid = {
        "lat_name": model.lat_name,
        "lon_name": model.lon_name,
        "search_radius": model.search_radius,
    }
    control.progress(variable, "loading_model", source="hist")
    hist_field = _reference_years(load_model_variable(model, "hist", variable, qa), ref)
    control.progress(variable, "extracting", source="hist")
    hist = extract_nearest(hist_field, ref, qa, source_label=f"{variable}:hist", **grid)
    control.progress(variable, "loading_model", source="sim")
    sim_field = load_model_variable(model, "sim", variable, qa)
    control.progress(variable, "extracting", source="sim")
    sim = extract_nearest(sim_field, ref, qa, source_label=f"{variable}:sim", **grid)
    return ref, hist, sim


def _run_station(
    cfg: PipelineConfig,
    target: TargetConfig,
    specs: dict[str, VariableSpec],
    qa: list[QACheck],
    *,
    control: RunControl,
    plan: CellPlan,
) -> dict[str, dict]:
    """Load and correct all station series for one target."""
    ref_by_var: dict[str, xr.DataArray] = {}
    hist_by_var: dict[str, xr.DataArray] = {}
    sim_by_var: dict[str, xr.DataArray] = {}
    for v in target.variables:
        ref_by_var[v], hist_by_var[v], sim_by_var[v] = _load_station_series(
            cfg, specs[v], qa, control=control
        )

    part = _correct_block(
        cfg,
        target,
        specs,
        ref_by_var,
        hist_by_var,
        sim_by_var,
        qa,
        compact=False,
        control=control,
        plan=plan,
        preparing=len(target.variables) == 1,
    )
    control.progress(_execution_label(target), "combining")
    results = _finish_target(cfg, target, specs, [part], qa, grid=None)
    control.check_cancelled()
    return results


def _run_grid(
    cfg: PipelineConfig,
    target_cfg: TargetConfig,
    specs: dict[str, VariableSpec],
    qa: list[QACheck],
    *,
    control: RunControl,
    plan: CellPlan,
) -> dict[str, dict]:
    """Regrid in the parent, then correct CPU/RAM-budgeted batches of spatial blocks.

    Workers keep full time series. Completed results accumulate in the parent;
    budgeting is a heuristic, not a bound on total run memory. Parallel worker
    stages are reported at batch boundaries, where cancellation is checked.
    """
    grid_cfg, model = cfg.grid, cfg.model
    assert grid_cfg is not None  # narrowed for the type checker; checked by the caller
    variables = list(target_cfg.variables)
    label = _execution_label(target_cfg)
    group = target_cfg.group

    references: dict[str, xr.DataArray] = {}
    lats: dict[str, Any] = {}
    lons: dict[str, Any] = {}
    has_data_by_var: dict[str, np.ndarray] = {}
    valid_by_var: dict[str, np.ndarray] = {}
    window_days_by_var: dict[str, int] = {}
    v0 = variables[0]

    for v in variables:
        control.progress(v, "loading_reference")
        ref_v, lat_v, lon_v = open_grid_reference(grid_cfg, v, qa)
        if len(variables) > 1:
            # Shared cell indices are safe only after sorting the actual geographic
            # coordinates (which may differ from the dimension indexes).
            ref_v = ref_v.sortby([lat_v, lon_v])
            if v != v0:
                for axis, coord_v, coord_0 in (
                    ("latitude", lat_v, lats[v0]),
                    ("longitude", lon_v, lons[v0]),
                ):
                    values = np.asarray(ref_v[coord_v].values, dtype=float)
                    expected = np.asarray(references[v0][coord_0].values, dtype=float)
                    if values.shape != expected.shape or not np.allclose(
                        values, expected, rtol=0, atol=1e-6
                    ):
                        _fail_qa(
                            qa,
                            f"grid:{label}:axes_mismatch",
                            f"Reference grid {axis} axes differ between '{v0}' and '{v}' "
                            f"for target '{label}' -- see QACheck 'grid:{label}:axes_mismatch'.",
                            {
                                "target": label,
                                "variables": [v0, v],
                                "axis": axis,
                                "atol": 1e-6,
                            },
                        )
        has_data_v, valid_v, window_days_v = scan_grid(
            ref_v,
            period=cfg.periods.calibration,
            on_progress=lambda completed, total, var=v: control.progress(
                var,
                "scanning_reference",
                completed=completed,
                total=total,
                unit="time steps",
            ),
        )
        references[v], lats[v], lons[v] = ref_v, lat_v, lon_v
        has_data_by_var[v], valid_by_var[v], window_days_by_var[v] = (
            has_data_v,
            valid_v,
            window_days_v,
        )

    has_data = (
        has_data_by_var[v0]
        if len(variables) == 1
        else intersect_grid_masks([has_data_by_var[v] for v in variables])
    )
    target_grid = grid_target(references[v0], lats[v0], lons[v0], has_data)
    if target_grid.size == 0:
        if len(variables) > 1:
            _fail_qa(
                qa,
                f"grid:{label}:ref:empty",
                f"The reference grid has no common data for target '{label}' (in the selected region).",
                {"target": label, "variables": variables},
                exc_msg=f"No reference data for target '{label}' -- see QACheck 'grid:{label}:ref:empty'.",
            )
        _fail_qa(
            qa,
            f"grid:{label}:ref:empty",
            f"The reference grid has no data for '{label}' (in the selected region).",
            {"variable": label},
            exc_msg=f"No reference data for '{label}' -- see QACheck 'grid:{label}:ref:empty'.",
        )

    # Early warning, before the (slow) model files are opened.
    for v in variables:
        report_coverage(
            coverage_table_from_counts(
                target_grid.ids,
                valid_by_var[v][target_grid.iy, target_grid.ix],
                window_days_by_var[v],
            ),
            window_days_by_var[v] / 365.25,
            qa,
            name=v,
            min_coverage=cfg.periods.min_coverage,
            unit="cell",
        )

    # Read once, then sliced per block (see read_grid_cells / model_box).
    cells: dict[str, np.ndarray | None] = {}
    options_by_var: dict[str, dict[str, Any]] = {}
    hist_all: dict[str, xr.DataArray] = {}
    sim_all: dict[str, xr.DataArray] = {}
    for v in variables:
        spec = specs[v]
        cells[v] = read_grid_cells(
            references[v],
            target_grid,
            on_progress=lambda completed, total, var=v: control.progress(
                var,
                "reading_reference",
                completed=completed,
                total=total,
                unit="time steps",
            ),
        )
        options = {
            "method": spec.regrid,
            "params": spec.regrid_params,
            "lat_name": model.lat_name,
            "lon_name": model.lon_name,
        }
        options_by_var[v] = options
        control.progress(v, "loading_model", source="hist")
        hist_all[v] = model_box(
            _reference_years(load_model_variable(model, "hist", v, qa), references[v]),
            target_grid,
            **options,
        )
        control.progress(v, "loading_model", source="sim")
        sim_all[v] = model_box(
            load_model_variable(model, "sim", v, qa), target_grid, **options
        )

    parts: list[dict] = []
    block_size = grid_cfg.block_size
    total_blocks = (target_grid.size + block_size - 1) // block_size
    quantile_count = _quantile_count(plan)
    groups = {"month": 12, "dayofyear": 366, "season": 4, "none": 1}[group]
    method_factor = method_memory_factor(plan.method)
    ref_steps = max(r.sizes["time"] for r in references.values())
    hist_steps = max(h.sizes["time"] for h in hist_all.values())
    sim_steps = max(s.sizes["time"] for s in sim_all.values())
    completed_cells = 0
    warned_memory = False
    start = 0
    ctx_key = "variable" if len(variables) == 1 else "target"

    with WorkerPool() as pool:
        while start < target_grid.size:
            control.check_cancelled()
            estimated_bytes = estimate_task_bytes(
                ref_steps,
                hist_steps,
                sim_steps,
                min(block_size, target_grid.size - start),
                nquantiles=quantile_count,
                groups=groups,
                n_vars=len(variables),
                method_factor=method_factor,
            )
            selection = select_workers(
                grid_cfg.workers,
                grid_cfg.memory_fraction,
                (target_grid.size - start + block_size - 1) // block_size,
                estimated_bytes,
            )
            control.progress(
                label,
                "worker_selection",
                workers=selection.workers,
                cpu_limit=selection.cpu_limit,
                memory_limit=selection.memory_limit,
                available_bytes=selection.available_bytes,
                budget_bytes=selection.budget_bytes,
                estimated_task_bytes=estimated_bytes,
                over_budget=selection.over_budget,
            )
            if selection.over_budget and not warned_memory:
                qa.append(
                    QACheck(
                        name=f"resources:{label}:memory",
                        status="warn",
                        message="One block exceeds the estimated RAM budget, or available RAM could not be determined. "
                        "Using one worker; reduce grid.block_size if memory is tight. Estimates do not guarantee against out-of-memory errors.",
                        context={
                            ctx_key: label,
                            "estimated_task_bytes": estimated_bytes,
                            "budget_bytes": selection.budget_bytes,
                        },
                    )
                )
                warned_memory = True

            tasks, contexts, batch_parts = [], [], []
            batch_stop = min(target_grid.size, start + selection.workers * block_size)
            for block_start in range(start, batch_stop, block_size):
                block = target_grid.block(block_start, block_start + block_size)
                block_context = {
                    "block_index": block_start // block_size + 1,
                    "total_blocks": total_blocks,
                    "completed_cells": completed_cells,
                    "total_cells": target_grid.size,
                    "block_cells": block.size,
                }
                control.progress(label, "block_started", **block_context)
                block_qa: list[QACheck] = []
                ref_block: dict[str, xr.DataArray] = {}
                hist_block: dict[str, xr.DataArray] = {}
                sim_block: dict[str, xr.DataArray] = {}
                for v in variables:
                    spec, opts = specs[v], options_by_var[v]
                    control.progress(
                        v, "extracting", source="reference", **block_context
                    )
                    ref_block[v] = load_grid_points(
                        references[v],
                        block,
                        v,
                        values=None
                        if cells[v] is None
                        else cells[v][:, block_start : block_start + block.size],
                    )
                    control.progress(v, "regridding", source="hist", **block_context)
                    hist_block[v] = _clip(
                        regrid_to_points(
                            hist_all[v],
                            block,
                            block_qa,
                            source_label=f"{v}:hist",
                            **opts,
                        ),
                        spec,
                    )
                    control.progress(v, "regridding", source="sim", **block_context)
                    sim_block[v] = _clip(
                        regrid_to_points(
                            sim_all[v], block, block_qa, source_label=f"{v}:sim", **opts
                        ),
                        spec,
                    )
                contexts.append(block_context)
                if selection.workers == 1:
                    part = _correct_block(
                        cfg,
                        target_cfg,
                        specs,
                        ref_block,
                        hist_block,
                        sim_block,
                        block_qa,
                        compact=True,
                        control=control,
                        block_context=block_context,
                        plan=plan,
                    )
                    batch_parts.append((part, block_qa))
                else:
                    tasks.append(
                        delayed(_correct_block_task)(
                            cfg,
                            target_cfg,
                            specs,
                            ref_block,
                            hist_block,
                            sim_block,
                            block_qa,
                            plan,
                        )
                    )

            if tasks:
                control.progress(
                    label,
                    "training_and_adjusting",
                    **contexts[-1],
                    active_blocks=len(tasks),
                )
                batch_parts = list(
                    compute(
                        *tasks,
                        scheduler="processes",
                        pool=pool.get(selection.workers),
                        chunksize=1,
                    )
                )
            control.check_cancelled()
            for context, (part, block_qa) in zip(contexts, batch_parts, strict=True):
                parts.append(part)
                qa.extend(
                    c
                    for c in block_qa
                    if not c.name.startswith("bounds:")
                    and (context["block_index"] == 1 or c.status != "pass")
                )
                completed_cells += context["block_cells"]
                control.progress(
                    label,
                    "block_finished",
                    **{**context, "completed_cells": completed_cells},
                )
            # Release prepared inputs before measuring the next batch's free RAM.
            del tasks, batch_parts
            ref_block = hist_block = sim_block = {}
            start = batch_stop

    control.progress(label, "combining")
    result = _finish_target(cfg, target_cfg, specs, parts, qa, grid=target_grid)
    control.check_cancelled()
    return result


def _strip_adj_for_ipc(part: dict) -> dict:
    """Strip live xsdba objects (TrainAdjust, ZeroHandling, post_trained) before IPC."""
    part["adj"] = None if part["adj"] is None else _CombinedAdjustment(part["adj"].ds)
    return part


def _correct_block_task(
    cfg: PipelineConfig,
    target: TargetConfig,
    specs: dict[str, VariableSpec],
    ref_by_var: dict[str, xr.DataArray],
    hist_by_var: dict[str, xr.DataArray],
    sim_by_var: dict[str, xr.DataArray],
    block_qa: list[QACheck],
    plan: CellPlan,
) -> tuple[dict, list[QACheck]]:
    """Run one block in a worker process; return only pickle-safe state."""
    part = _correct_block(
        cfg,
        target,
        specs,
        ref_by_var,
        hist_by_var,
        sim_by_var,
        block_qa,
        compact=True,
        plan=plan,
    )
    return _strip_adj_for_ipc(part), block_qa


def _reference_years(hist: xr.DataArray, reference: xr.DataArray) -> xr.DataArray:
    """hist cut to the years the reference covers.

    Only those days can be trained on or evaluated (align_daily keeps the
    common days anyway), so a CMIP hist of 1850-2014 against observations
    of 1980-2014 would otherwise be read, regridded and held in memory
    ~5 times over for nothing. The result is unchanged.
    """
    index = hist.indexes["time"]
    if not index.is_monotonic_increasing:
        return hist
    years = reference["time"].dt.year
    return hist.sel(time=slice(str(int(years.min())), str(int(years.max()))))


def _clip(da: xr.DataArray, spec: VariableSpec) -> xr.DataArray:
    """Keep regridded values within physical limits: bicubic and patch can
    overshoot, e.g. slightly negative rain next to a dry cell."""
    low, high = spec.bounds
    if low is None and high is None:
        return da
    return da.clip(min=low, max=high, keep_attrs=True)


def _harmonize_and_align(
    variables: list[str],
    specs: dict[str, VariableSpec],
    ref_by_var: dict[str, xr.DataArray],
    hist_by_var: dict[str, xr.DataArray],
    sim_by_var: dict[str, xr.DataArray],
    qa: list[QACheck],
) -> tuple[
    dict[str, xr.DataArray],
    dict[str, xr.DataArray],
    dict[str, xr.DataArray],
    dict[str, str],
]:
    """Harmonize units and align daily time axes for every variable."""
    aligned_ref, aligned_hist, aligned_sim, units = {}, {}, {}, {}
    for v in variables:
        ref_v, hist_v, sim_v, unit_v = harmonize(
            ref_by_var[v], hist_by_var[v], sim_by_var[v], specs[v].units, qa, name=v
        )
        ref_v, hist_v, sim_v = align_daily(ref_v, hist_v, sim_v, qa, name=v)
        aligned_ref[v], aligned_hist[v], aligned_sim[v], units[v] = (
            ref_v,
            hist_v,
            sim_v,
            unit_v,
        )
    return aligned_ref, aligned_hist, aligned_sim, units


def _finalize_cell_part(
    variables: list[str],
    scen_ds: xr.Dataset,
    sim_ds: xr.Dataset,
    adj: Any | None,
    tables_by_var: dict[str, dict[str, pd.DataFrame]],
    series_by_var: dict[str, list[xr.Dataset]],
    units: dict[str, str],
    calibration_span: dict,
    clip_counts: dict[str, tuple[int, int]],
    *,
    compact: bool,
    control: RunControl,
) -> dict:
    """Optionally cast outputs to float32, materialize lazy arrays, and build part."""
    scen_by_var = {
        v: (scen_ds[v].astype("float32") if compact else scen_ds[v]) for v in variables
    }
    if compact:
        series_by_var = {
            v: [s.astype("float32") for s in series_by_var[v]] for v in variables
        }
    control.check_cancelled()
    # Accessing .values for QA computes lazy arrays but does not retain them.
    for v in variables:
        scen_by_var[v].load()
        for s in series_by_var[v]:
            s.load()
    if adj is not None:
        adj.ds.load()
    control.check_cancelled()
    return {
        "scen": scen_by_var,
        "adj": adj,
        "tables": tables_by_var,
        "series": series_by_var,
        "non_finite": {v: _non_finite(scen_by_var[v], sim_ds[v]) for v in variables},
        "units": units,
        "calibration": calibration_span,
        "clip_counts": clip_counts,
    }


def _run_cell_and_evaluate(
    cfg: PipelineConfig,
    progress_label: str,
    variables: list[str],
    specs: dict[str, VariableSpec],
    ref_by_var: dict[str, xr.DataArray],
    hist_by_var: dict[str, xr.DataArray],
    sim_by_var: dict[str, xr.DataArray],
    units: dict[str, str],
    qa: list[QACheck],
    *,
    compact: bool,
    control: RunControl,
    plan: CellPlan,
    block_context: dict[str, int] | None = None,
) -> dict:
    """Slice calibration period, execute run_cell, evaluate, and finalize the part dict."""
    context = block_context or {}
    calibration = cfg.periods.calibration
    ref_cal = {
        v: _calibration_slice(ref_by_var[v], calibration, qa, label=f"{v}:ref")
        for v in variables
    }
    hist_cal = {
        v: _calibration_slice(hist_by_var[v], calibration, qa, label=f"{v}:hist")
        for v in variables
    }
    ref_cal_ds, hist_cal_ds = xr.Dataset(ref_cal), xr.Dataset(hist_cal)
    sim_ds = xr.Dataset({v: sim_by_var[v] for v in variables})

    control.progress(progress_label, "training", **context)
    res = run_cell(
        plan,
        ref=ref_cal_ds,
        hist=hist_cal_ds,
        sim=sim_ds,
        specs=specs,
        units=units,
        control=control,
        seed=_SEED,
        qa=qa,
    )
    control.progress(progress_label, "adjustment", **context)
    adj = res.trained_obj

    control.progress(progress_label, "evaluation", **context)
    tables_by_var, series_by_var = _evaluate_block(
        variables,
        specs,
        cfg.periods,
        plan,
        adj,
        res.zeros,
        ref_by_var,
        hist_by_var,
        ref_cal_ds,
        hist_cal_ds,
        units,
        qa,
        post_trained=res.post_trained,
    )
    return _finalize_cell_part(
        variables,
        res.scen,
        sim_ds,
        adj,
        tables_by_var,
        series_by_var,
        units,
        _span(ref_cal[variables[0]]),
        res.clip_counts,
        compact=compact,
        control=control,
    )


def _correct_block(
    cfg: PipelineConfig,
    target: TargetConfig,
    specs: dict[str, VariableSpec],
    ref_by_var: dict[str, xr.DataArray],
    hist_by_var: dict[str, xr.DataArray],
    sim_by_var: dict[str, xr.DataArray],
    qa: list[QACheck],
    *,
    plan: CellPlan,
    compact: bool = False,
    control: RunControl | None = None,
    block_context: dict[str, int] | None = None,
    preparing: bool = True,
) -> dict:
    """Harmonize, align, run the cell contract, and evaluate one station/block set."""
    if control is None:
        control = RunControl()
    variables = list(target.variables)
    if preparing:
        control.progress(_execution_label(target), "preparing", **(block_context or {}))
    aligned_ref, aligned_hist, aligned_sim, units = _harmonize_and_align(
        variables, specs, ref_by_var, hist_by_var, sim_by_var, qa
    )
    if len(variables) > 1:
        aligned_ref, aligned_hist, aligned_sim = _intersect_target_station_data(
            target.name, variables, aligned_ref, aligned_hist, aligned_sim, qa
        )
    return _run_cell_and_evaluate(
        cfg,
        _execution_label(target),
        variables,
        specs,
        aligned_ref,
        aligned_hist,
        aligned_sim,
        units,
        qa,
        compact=compact,
        control=control,
        block_context=block_context,
        plan=plan,
    )


class _CombinedAdjustment:
    """The trained factors of several blocks of cells, for writing adj.nc."""

    def __init__(self, ds: xr.Dataset):
        self.ds = ds


def _quantile_count(plan: CellPlan) -> int:
    """Budget for the largest quantile table in train or adjustment parameters."""
    counts = []
    for pairs in (plan.train_params, plan.adjust_params):
        params = dict(pairs)
        containers = [params]
        base_kws = params.get("base_kws")
        if isinstance(base_kws, dict):
            containers.append(base_kws)
        base_kws_vars = params.get("base_kws_vars")
        if isinstance(base_kws_vars, dict):
            containers.extend(
                kws for kws in base_kws_vars.values() if isinstance(kws, dict)
            )
        for kws in containers:
            quantiles = kws.get("nquantiles")
            if quantiles is not None:
                counts.append(
                    int(quantiles)
                    if isinstance(quantiles, (int, np.integer))
                    else len(quantiles)
                )
    return max(counts, default=50)


def _concat_adj(parts: list[dict]) -> Any | None:
    """Combine trained adjustment datasets across spatial blocks, or return None."""
    if parts[0]["adj"] is None:
        return None
    if len(parts) == 1:
        return parts[0]["adj"]
    return _CombinedAdjustment(
        cast(xr.Dataset, xr.concat([p["adj"].ds for p in parts], dim="station"))
    )


def _emit_bounds_summary(
    spec: VariableSpec, n_below: int, n_above: int, qa: list[QACheck]
) -> None:
    """Emit one summary bounds QACheck across all grid blocks of a variable."""
    low, high = spec.bounds
    if low is None and high is None:
        return
    variable, n_total = spec.name, n_below + n_above
    clip_context = {
        "variable": variable,
        "bounds": [low, high],
        "n_below": n_below,
        "n_above": n_above,
        "n_clipped": n_total,
    }
    if n_total > 0:
        qa.append(
            QACheck(
                name=f"bounds:{variable}:clipped",
                status="warn",
                message=f"Clipped {n_total} value(s) of '{variable}' to bounds [{low}, {high}] ({n_below} below, {n_above} above).",
                context=clip_context,
            )
        )
    else:
        qa.append(
            QACheck(
                name=f"bounds:{variable}:ok",
                status="pass",
                message=f"All values of '{variable}' are within bounds [{low}, {high}].",
                context=clip_context,
            )
        )


def _emit_non_finite_qa(
    variable: str, non_finite: list[str], unit: str, qa: list[QACheck]
) -> None:
    if non_finite:
        qa.append(
            QACheck(
                name=f"adjust:{variable}:non_finite",
                status="warn",
                message=(
                    f"The correction of '{variable}' produced NaN/inf where the model had valid values at "
                    f"{len(non_finite)} {unit}(s), e.g. no observation in the calibration period: {non_finite[:10]}"
                ),
                context={"variable": variable, "stations": non_finite},
            )
        )


def _finish_variable(
    cfg: PipelineConfig,
    spec: VariableSpec,
    parts: list[dict],
    qa: list[QACheck],
    *,
    adj: Any | None,
    grid: TargetGrid | None,
    method: str,
    group: str,
    window: int,
    target_name: str | None = None,
) -> dict:
    """Combine one variable's blocks and emit bounds, non-finite, done, and evaluation QA."""
    variable = spec.name
    unit = "cell" if grid is not None else "station"
    scen_pieces = [p["scen"][variable] for p in parts]
    scen = (
        scen_pieces[0]
        if len(scen_pieces) == 1
        else cast(xr.DataArray, xr.concat(scen_pieces, dim="station"))
    )

    if grid is not None:
        n_below = sum(p.get("clip_counts", {}).get(variable, (0, 0))[0] for p in parts)
        n_above = sum(p.get("clip_counts", {}).get(variable, (0, 0))[1] for p in parts)
        _emit_bounds_summary(spec, n_below, n_above, qa)

    non_finite = [
        s
        for p in parts
        for s in p["non_finite"][variable]
    ]
    _emit_non_finite_qa(variable, non_finite, unit, qa)

    context: dict[str, Any] = {"variable": variable}
    if target_name is not None:
        context["target"] = target_name
    context.update(
        {
            "method": method,
            "group": group,
            "regrid": spec.regrid if grid is not None else None,
            "n_stations": int(scen.sizes.get("station", 0)),
            "calibration": parts[0]["calibration"],
            "sim": _span(scen),
        }
    )
    qa.append(
        QACheck(
            name=f"train_adjust:{variable}:done",
            status="pass",
            message=(
                f"Completed train+adjust for '{variable}' using '{method}' "
                f"(group={group}{f', window={window}' if group == 'dayofyear' else ''}"
                + (
                    f", regrid={spec.regrid}, {len(parts)} block(s)"
                    if grid is not None
                    else ""
                )
                + ")."
            ),
            context=context,
        )
    )

    var_parts = [
        {"tables": p["tables"][variable], "series": p["series"][variable]}
        for p in parts
    ]
    evaluation, hist_eval = _summarize_evaluation(
        spec, cfg.periods, var_parts, qa, unit=unit
    )
    return {
        "scen": scen,
        "adj": adj,
        "evaluation": evaluation,
        "hist_eval": hist_eval,
        "grid": grid,
    }


def _finish_target(
    cfg: PipelineConfig,
    target: TargetConfig,
    specs: dict[str, VariableSpec],
    parts: list[dict],
    qa: list[QACheck],
    *,
    grid: TargetGrid | None,
) -> dict[str, dict]:
    """Concatenate blocks and return one result dict per variable."""
    adj = _concat_adj(parts)
    return {
        v: _finish_variable(
            cfg,
            specs[v],
            parts,
            qa,
            adj=adj,
            grid=grid,
            method=target.method,
            group=target.group,
            window=target.window,
            target_name=None if len(target.variables) == 1 else target.name,
        )
        for v in target.variables
    }


def _check_post_steps(
    label: str,
    post_steps: Sequence[PostConfig],
    qa: list[QACheck],
    *,
    is_target: bool = False,
) -> None:
    """Validate every post-processing step before loading data."""
    ctx_key = "target" if is_target else "variable"
    noun = "target" if is_target else "variable"
    for step in post_steps:
        if step.method not in METHODS:
            _fail_qa(
                qa,
                f"post:{label}:unknown",
                f"Post-step method '{step.method}' is not registered. Valid methods: {sorted(METHODS)}.",
                {ctx_key: label, "method": step.method},
                exc_msg=f"Invalid post-step method for {noun} '{label}': {step.method!r}",
            )
        post_spec = get_method(step.method)
        if post_spec.family != "post":
            msg = f"Post-step method '{step.method}' must have family 'post', got '{post_spec.family}'."
            _fail_qa(
                qa,
                f"post:{label}:invalid_family",
                msg,
                {ctx_key: label, "method": step.method, "family": post_spec.family},
                exc_msg=msg,
            )
        try:
            check_available(post_spec)
        except ValueError as exc:
            qa.append(
                QACheck(
                    name=f"post:{label}:unavailable",
                    status="fail",
                    message=str(exc),
                    context={
                        ctx_key: label,
                        "method": step.method,
                        "extra": post_spec.extra,
                    },
                )
            )
            raise


def _validate_primary_method(
    label: str,
    method_name: str,
    qa: list[QACheck],
    *,
    is_target: bool,
    variables: Sequence[str],
    group: str | None = None,
    post: Sequence[PostConfig] = (),
) -> MethodSpec:
    """Validate primary method registration, family, arity, grouping, extras, and post steps."""
    ctx_key = "target" if is_target else "variable"
    noun = "target" if is_target else "variable"
    var_list = list(variables)
    if method_name not in METHODS:
        ctx = (
            {"target": label, "method": method_name}
            if is_target
            else {"variable": label}
        )
        _fail_qa(
            qa,
            f"method:{label}:unknown",
            f"Method '{method_name}' is not registered. Valid methods: {sorted(METHODS)}.",
            ctx,
            exc_msg=f"Invalid method for {noun} '{label}': {method_name!r}",
        )

    method = get_method(method_name)
    if method.family == "post":
        msg = (
            f"Method '{method_name}' has family 'post' and cannot be used as a primary method."
            if is_target
            else f"Method '{method_name}' has family 'post' and is only valid in 'post:' (Phase 3)."
        )
        _fail_qa(
            qa,
            f"method:{label}:invalid_family",
            msg,
            {ctx_key: label, "method": method_name, "family": method.family},
            exc_msg=f"Method '{method_name}' has family 'post' and cannot be used as a primary method.",
        )

    if len(var_list) < method.min_vars:
        if is_target:
            min_msg = f"Method '{method_name}' requires at least {method.min_vars} variable(s), got {len(var_list)}."
            min_ctx: dict[str, Any] = {
                "target": label,
                "method": method_name,
                "min_vars": method.min_vars,
                "n_vars": len(var_list),
            }
        else:
            min_msg = f"Method '{method_name}' requires at least {method.min_vars} variables and needs 'targets:' (Phase 4)."
            min_ctx = {
                "variable": label,
                "method": method_name,
                "min_vars": method.min_vars,
            }
        _fail_qa(
            qa,
            f"method:{label}:min_vars",
            min_msg,
            min_ctx,
            exc_msg=f"Method '{method_name}' requires at least {method.min_vars} variables.",
        )

    if is_target and len(var_list) > 1 and method.min_vars == 1:
        _fail_qa(
            qa,
            f"method:{label}:univariate_only",
            f"Method '{method_name}' is univariate and cannot be applied to multiple variables {var_list}.",
            {"target": label, "method": method_name, "variables": var_list},
            exc_msg=f"Method '{method_name}' is univariate and cannot be used with multiple variables.",
        )

    if is_target and method_name == "mbcn" and group != "none":
        _fail_qa(
            qa,
            f"method:{label}:group",
            "Method 'mbcn' requires group='none'.",
            {"target": label, "method": method_name, "group": group},
            exc_msg="Method 'mbcn' requires group='none'.",
        )

    if is_target and not method.supports_projection:
        msg = f"Method '{method_name}' does not support projection onto future 'sim'."
        _fail_qa(
            qa,
            f"method:{label}:no_projection",
            msg,
            {"target": label, "method": method_name},
            exc_msg=msg,
        )

    try:
        check_available(method)
    except ValueError as exc:
        qa.append(
            QACheck(
                name=f"method:{label}:unavailable",
                status="fail",
                message=str(exc),
                context={ctx_key: label, "method": method_name, "extra": method.extra},
            )
        )
        raise

    _check_post_steps(label, post, qa, is_target=is_target)
    return method


def _build_post_steps(post: list[PostConfig]) -> tuple[PostStep, ...]:
    return tuple(
        PostStep(
            method=p.method,
            train_params=tuple(p.train_params.items()),
            adjust_params=tuple(p.adjust_params.items()),
        )
        for p in post
    )


def _build_target_plan(
    target: TargetConfig, specs: dict[str, VariableSpec]
) -> CellPlan:
    meta = get_method_meta(target.method)
    if len(target.variables) == 1 and meta.min_vars == 1:
        train_params, adjust_params = resolve_method_params(
            specs[target.variables[0]],
            meta,
            explicit_train=dict(target.train_params) if target.train_params else None,
            explicit_adjust=dict(target.adjust_params)
            if target.adjust_params
            else None,
        )
    else:
        train_params, adjust_params = (
            dict(target.train_params),
            dict(target.adjust_params),
        )
    return CellPlan(
        target_name=target.name,
        variables=tuple(target.variables),
        method=target.method,
        group=target.group,
        window=target.window,
        pool_dims=tuple(target.pool_dims),
        train_params=tuple(train_params.items()),
        adjust_params=tuple(adjust_params.items()),
        post=_build_post_steps(target.post),
    )


def _intersect_target_station_data(
    target_name: str,
    variables: list[str],
    ref_by_var: dict[str, xr.DataArray],
    hist_by_var: dict[str, xr.DataArray],
    sim_by_var: dict[str, xr.DataArray],
    qa: list[QACheck],
) -> tuple[dict[str, xr.DataArray], dict[str, xr.DataArray], dict[str, xr.DataArray]]:
    """Intersect station IDs and time axes across all variables of a station target."""
    aligned_ref, dropped_ref = intersect_station_arrays(ref_by_var)
    aligned_hist, dropped_hist = intersect_station_arrays(hist_by_var)
    aligned_sim, dropped_sim = intersect_station_arrays(sim_by_var)

    v0 = variables[0]
    ref_stations = [str(s) for s in aligned_ref[v0]["station"].values]
    hist_set = {str(s) for s in aligned_hist[v0]["station"].values}
    sim_set = {str(s) for s in aligned_sim[v0]["station"].values}
    common_stations = [s for s in ref_stations if s in hist_set and s in sim_set]
    if (
        len(common_stations) != len(ref_stations)
        or len(common_stations) != len(hist_set)
        or len(common_stations) != len(sim_set)
    ):
        aligned_ref = {
            v: da.sel(station=common_stations) for v, da in aligned_ref.items()
        }
        aligned_hist = {
            v: da.sel(station=common_stations) for v, da in aligned_hist.items()
        }
        aligned_sim = {
            v: da.sel(station=common_stations) for v, da in aligned_sim.items()
        }

    if aligned_ref[v0].sizes["station"] > 0 and aligned_hist[v0].sizes["station"] > 0:
        ref0, hist0 = xr.align(aligned_ref[v0], aligned_hist[v0], join="inner")
        if (
            ref0.sizes["time"] != aligned_ref[v0].sizes["time"]
            or hist0.sizes["time"] != aligned_hist[v0].sizes["time"]
        ):
            common_time = ref0["time"].values
            aligned_ref = {v: da.sel(time=common_time) for v, da in aligned_ref.items()}
            aligned_hist = {
                v: da.sel(time=common_time) for v, da in aligned_hist.items()
            }

    kept_set = set(common_stations)
    all_dropped: list[str] = []
    for s in [*dropped_ref, *dropped_hist, *dropped_sim, *ref_stations]:
        if s not in kept_set and s not in all_dropped:
            all_dropped.append(s)

    n_stations = len(common_stations)
    n_ref_time, n_sim_time = (
        int(aligned_ref[v0].sizes["time"]),
        int(aligned_sim[v0].sizes["time"]),
    )
    if n_stations == 0:
        _fail_qa(
            qa,
            f"target:{target_name}:station_intersection",
            f"Target '{target_name}' has 0 common stations across variables {variables} (dropped: {all_dropped[:10]}).",
            {
                "target": target_name,
                "variables": variables,
                "dropped_stations": all_dropped,
                "n_common_stations": 0,
            },
            exc_msg=f"Target '{target_name}' has 0 common stations across {variables}.",
        )
    if n_ref_time == 0 or n_sim_time == 0:
        _fail_qa(
            qa,
            f"target:{target_name}:time_intersection",
            f"Target '{target_name}' has 0 common time steps across variables {variables}.",
            {
                "target": target_name,
                "variables": variables,
                "n_ref_time": n_ref_time,
                "n_sim_time": n_sim_time,
            },
            exc_msg=f"Target '{target_name}' has 0 common time steps across {variables}.",
        )
    if all_dropped:
        qa.append(
            QACheck(
                name=f"target:{target_name}:station_intersection",
                status="warn",
                message=(
                    f"Target '{target_name}': dropped {len(all_dropped)} station(s) not present across all "
                    f"variables {variables}; continuing with {n_stations} common station(s): {all_dropped[:10]}"
                ),
                context={
                    "target": target_name,
                    "variables": variables,
                    "dropped_stations": all_dropped,
                    "n_common_stations": n_stations,
                },
            )
        )
    return aligned_ref, aligned_hist, aligned_sim


def _evaluate_block(
    variables: list[str],
    specs: dict[str, VariableSpec],
    periods: PeriodsConfig,
    plan: CellPlan,
    adj: Any | None,
    zeros: ZeroHandling | None,
    ref_by_var: dict[str, xr.DataArray],
    hist_by_var: dict[str, xr.DataArray],
    ref_cal_ds: xr.Dataset,
    hist_cal_ds: xr.Dataset,
    units: dict[str, str],
    qa: list[QACheck],
    *,
    post_trained: tuple[tuple[MethodSpec, Any, dict[str, Any]], ...] | None = None,
) -> tuple[dict[str, dict[str, pd.DataFrame]], dict[str, list[xr.Dataset]]]:
    """Apply the trained correction to hist over calibration & validation and score each variable.

    Calibration scores are in-sample; validation scores are the independent
    check of how well it generalises. Each period is isolated: a failure becomes
    a 'warn' check, and the other period -- and the correction itself -- are kept.
    """
    method = get_method(plan.method)
    tables_by_var: dict[str, dict[str, pd.DataFrame]] = {v: {} for v in variables}
    series_by_var: dict[str, list[xr.Dataset]] = {v: [] for v in variables}

    for label, period in _evaluation_periods(periods):
        if (
            method.family == "adjust_only"
            and not method.supports_projection
            and label == "validation"
        ):
            for v in variables:
                qa.append(
                    QACheck(
                        name=f"evaluation:{v}:validation:skipped",
                        status="warn",
                        message=(
                            f"Evaluation of the validation period skipped for '{v}': "
                            f"method '{plan.method}' does not support out-of-sample projection."
                        ),
                        context={"variable": v, "period_label": label},
                    )
                )
            continue
        try:
            obs_slice = {v: _slice(ref_by_var[v], period) for v in variables}
            raw_slice = {v: _slice(hist_by_var[v], period) for v in variables}
            if any(
                obs_slice[v].sizes["time"] == 0 or raw_slice[v].sizes["time"] == 0
                for v in variables
            ):
                where = f"{period[0]}..{period[1]}" if period else "the common period"
                raise ValueError(f"no reference or model data in {where}")
            raw_ds = xr.Dataset(raw_slice)
            if method.family == "train_adjust":
                corrected_ds = adjust_trained(
                    plan,
                    adj,
                    sim=raw_ds,
                    ref=ref_cal_ds,
                    hist=hist_cal_ds,
                    specs=specs,
                    units=units,
                    zeros=zeros,
                    post_trained=post_trained,
                )
            else:
                eval_plan = (
                    plan
                    if method.supports_projection
                    else replace(plan, has_projection=False)
                )
                corrected_ds = run_cell(
                    eval_plan,
                    ref=ref_cal_ds,
                    hist=hist_cal_ds,
                    sim=raw_ds,
                    specs=specs,
                    units=units,
                    seed=_SEED,
                    qa=[],
                ).scen

            for v in variables:
                obs_v, raw_v, corrected_v = obs_slice[v], raw_slice[v], corrected_ds[v]
                obs_paired = obs_on_model_dates(obs_v, raw_v)
                tables_by_var[v][label] = evaluate(
                    obs_v,
                    raw_v,
                    corrected_v,
                    wet_thresh=_wet_threshold(specs[v], units[v]),
                    period=label,
                    obs_paired=obs_paired,
                )
                series_by_var[v].append(
                    paired_series(
                        obs_v, raw_v, corrected_v, period=label, obs_paired=obs_paired
                    )
                )
        except Exception as exc:  # noqa: BLE001 -- isolate evaluation from the correction itself
            for v in variables:
                qa.append(
                    QACheck(
                        name=f"evaluation:{v}:{label}:skipped",
                        status="warn",
                        message=f"Evaluation of the {label} period skipped for '{v}': {exc}",
                        context={"variable": v, "period_label": label},
                    )
                )
    return tables_by_var, series_by_var


def _non_finite(scen: xr.DataArray, sim: xr.DataArray) -> list[str]:
    """Stations (or cells) where the correction turned valid model values
    into NaN or inf -- typically no observation in the calibration period,
    so nothing could be trained for them."""
    bad = ~np.isfinite(scen.values) & np.isfinite(sim.transpose(*scen.dims).values)
    lost = bad.sum(axis=scen.get_axis_num("time"))
    return [str(s) for s, n in zip(scen["station"].values, lost) if n]


def _slice(da: xr.DataArray, period: tuple[str, str] | None) -> xr.DataArray:
    return da if period is None else da.sel(time=slice(*period))


def _span(da: xr.DataArray) -> dict:
    t = da["time"]
    if t.size == 0:
        return {"start": None, "end": None, "n": 0}
    return {
        "start": t.isel(time=0).dt.strftime("%Y-%m-%d").item(),
        "end": t.isel(time=-1).dt.strftime("%Y-%m-%d").item(),
        "n": int(t.size),
    }


def _calibration_slice(
    da: xr.DataArray, period, qa: list[QACheck], *, label: str
) -> xr.DataArray:
    out = _slice(da, period)
    if out.sizes["time"] == 0:
        available = _span(da)
        _fail_qa(
            qa,
            f"periods:{label}:calibration_empty",
            f"No {label} data in the calibration period {period[0]}..{period[1]} (data covers {available['start']}..{available['end']}).",
            {
                "variable": label.split(":")[0],
                "period": list(period),
                "available": available,
            },
            exc_msg=f"No {label} data in the calibration period -- see QACheck 'periods:{label}:calibration_empty'.",
        )
    return out


def _wet_threshold(spec: VariableSpec, units: str) -> float | None:
    """Wet-day threshold for precipitation, in the data's units; None otherwise."""
    if spec.name != "precip" or not units:
        return None
    try:
        return float(
            convert_units_to(spec.adapt_freq_thresh or _DEFAULT_WET_DAY, units)
        )
    except pint.PintError:
        return None


def _evaluation_periods(
    periods: PeriodsConfig,
) -> list[tuple[str, tuple[str, str] | None]]:
    """Calibration always (None = all common data), validation when set."""
    out: list[tuple[str, tuple[str, str] | None]] = [
        ("calibration", periods.calibration)
    ]
    if periods.validation is not None:
        out.append(("validation", periods.validation))
    return out


def _summarize_evaluation(
    spec: VariableSpec,
    periods: PeriodsConfig,
    parts: list[dict],
    qa: list[QACheck],
    *,
    unit: str,
) -> tuple[pd.DataFrame | None, xr.Dataset | None]:
    """One summary check per period over every block, the joined table and
    the obs/raw/corrected series."""
    name = spec.name
    all_tables = []
    for label, period in _evaluation_periods(periods):
        pieces = [p["tables"][label] for p in parts if label in p["tables"]]
        if pieces:
            table = pd.concat(pieces, ignore_index=True)
            summarize(table, qa, name=name, label=label, period=period, unit=unit)
            all_tables.append(table)

    if periods.validation is None:
        qa.append(
            QACheck(
                name=f"evaluation:{name}:in_sample",
                status="warn",
                message=(
                    f"No validation period set for '{name}': only the calibration period was evaluated, "
                    "and those scores are in-sample (optimistic). Set periods.validation for an independent check."
                ),
                context={"variable": name},
            )
        )

    if not all_tables:
        return None, None
    per_block = [
        cast(xr.Dataset, xr.concat(p["series"], dim="time")).sortby("time")
        for p in parts
        if p["series"]
    ]
    hist_eval = (
        per_block[0]
        if len(per_block) == 1
        else cast(xr.Dataset, xr.concat(per_block, dim="station"))
    )
    return pd.concat(all_tables, ignore_index=True), hist_eval
