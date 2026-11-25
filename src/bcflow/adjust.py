"""Run the full correction for ONE variable.

Steps, in order:
    1. resolve the variable's settings: defaults + `correction:` overrides
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

It knows nothing about running several variables, or about isolating one
variable's failure from another's -- that is pipeline.py's job.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import numpy as np
import pandas as pd
import pint
import xarray as xr
from dask.base import compute
from dask.delayed import delayed
from xsdba.units import convert_units_to

from .cell import CellPlan, PostStep, adjust_trained, run_cell
from .config_schema import CorrectionConfig, PeriodsConfig, PipelineConfig
from .coverage import check_coverage, coverage_table_from_counts, report_coverage
from .discovery import get_method_meta
from .evaluate import evaluate, obs_on_model_dates, paired_series, summarize
from .extract import extract_nearest
from .loaders import (
    grid_target,
    load_grid_points,
    load_model_variable,
    read_grid_cells,
    load_station_variable,
    open_grid_reference,
    scan_grid,
)
from .methods import METHODS, MethodSpec, check_available, get_method
from .preprocess import SEED, ZeroHandling
from .progress import RunControl
from .qa import QACheck
from .regrid import TargetGrid, model_box, regrid_to_points
from .resources import WorkerPool, estimate_task_bytes, select_workers
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
    if control is None:
        control = RunControl()
    control.check_cancelled()
    spec = cfg.spec(variable)
    override = cfg.correction.get(spec.name)
    # Before loading: a typo in the method name should not cost a full data load.
    _check_method(spec, qa, override=override)
    plan = _build_plan(cfg, spec)
    if cfg.grid is not None:
        return _run_grid(cfg, spec, qa, control=control, plan=plan)

    model = cfg.model
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
    hist = extract_nearest(
        hist_field,
        ref,
        qa,
        source_label=f"{variable}:hist",
        **grid,
    )
    control.progress(variable, "loading_model", source="sim")
    sim_field = load_model_variable(model, "sim", variable, qa)
    control.progress(variable, "extracting", source="sim")
    sim = extract_nearest(
        sim_field,
        ref,
        qa,
        source_label=f"{variable}:sim",
        **grid,
    )
    part = _correct(cfg, spec, ref, hist, sim, qa, control=control, plan=plan)
    control.progress(variable, "combining")
    result = _finish(cfg, spec, [part], qa, grid=None)
    control.check_cancelled()
    return result


def _run_grid(
    cfg: PipelineConfig,
    spec: VariableSpec,
    qa: list[QACheck],
    *,
    control: RunControl,
    plan: CellPlan,
) -> dict:
    """Regrid in the parent, then correct CPU/RAM-budgeted batches of spatial blocks.

    Workers keep full time series. Completed results accumulate in the parent;
    budgeting is a heuristic, not a bound on total run memory. Parallel worker
    stages are reported at batch boundaries, where cancellation is checked.
    """
    variable, grid_cfg, model = spec.name, cfg.grid, cfg.model
    assert grid_cfg is not None  # narrowed for the type checker; checked by the caller
    control.progress(variable, "loading_reference")
    reference, lat, lon = open_grid_reference(grid_cfg, variable, qa)
    has_data, valid, window_days = scan_grid(
        reference,
        period=cfg.periods.calibration,
        on_progress=lambda completed, total: control.progress(
            variable,
            "scanning_reference",
            completed=completed,
            total=total,
            unit="time steps",
        ),
    )
    target = grid_target(reference, lat, lon, has_data)
    if target.size == 0:
        qa.append(
            QACheck(
                name=f"grid:{variable}:ref:empty",
                status="fail",
                message=f"The reference grid has no data for '{variable}' (in the selected region).",
                context={"variable": variable},
            )
        )
        raise ValueError(
            f"No reference data for '{variable}' -- see QACheck 'grid:{variable}:ref:empty'."
        )
    # Early warning, before the (slow) model files are opened.
    report_coverage(
        coverage_table_from_counts(
            target.ids, valid[target.iy, target.ix], window_days
        ),
        window_days / 365.25,
        qa,
        name=variable,
        min_coverage=cfg.periods.min_coverage,
        unit="cell",
    )

    # Read once, then sliced per block (see read_grid_cells / model_box).
    cells = read_grid_cells(
        reference,
        target,
        on_progress=lambda completed, total: control.progress(
            variable,
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
    control.progress(variable, "loading_model", source="hist")
    hist_all = model_box(
        _reference_years(load_model_variable(model, "hist", variable, qa), reference),
        target,
        **options,
    )
    control.progress(variable, "loading_model", source="sim")
    sim_all = model_box(
        load_model_variable(model, "sim", variable, qa), target, **options
    )
    parts = []
    block_size = grid_cfg.block_size
    total_blocks = (target.size + block_size - 1) // block_size
    quantiles = dict(plan.train_params).get("nquantiles", 50)
    quantile_count = (
        int(quantiles) if isinstance(quantiles, (int, np.integer)) else len(quantiles)
    )
    groups = {"month": 12, "dayofyear": 366, "season": 4, "none": 1}[spec.group]
    completed_cells = 0
    warned_memory = False
    start = 0
    with WorkerPool() as pool:
        while start < target.size:
            control.check_cancelled()
            estimated_bytes = estimate_task_bytes(
                reference.sizes["time"],
                hist_all.sizes["time"],
                sim_all.sizes["time"],
                min(block_size, target.size - start),
                nquantiles=quantile_count,
                groups=groups,
            )
            selection = select_workers(
                grid_cfg.workers,
                grid_cfg.memory_fraction,
                (target.size - start + block_size - 1) // block_size,
                estimated_bytes,
            )
            control.progress(
                variable,
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
                        name=f"resources:{variable}:memory",
                        status="warn",
                        message="One block exceeds the estimated RAM budget, or available RAM could not be determined. "
                        "Using one worker; reduce grid.block_size if memory is tight. Estimates do not guarantee against out-of-memory errors.",
                        context={
                            "variable": variable,
                            "estimated_task_bytes": estimated_bytes,
                            "budget_bytes": selection.budget_bytes,
                        },
                    )
                )
                warned_memory = True

            tasks = []
            contexts = []
            batch_parts = []
            batch_stop = min(target.size, start + selection.workers * block_size)
            for block_start in range(start, batch_stop, block_size):
                block = target.block(block_start, block_start + block_size)
                block_context = {
                    "block_index": block_start // block_size + 1,
                    "total_blocks": total_blocks,
                    "completed_cells": completed_cells,
                    "total_cells": target.size,
                    "block_cells": block.size,
                }
                control.progress(variable, "block_started", **block_context)
                block_qa: list[QACheck] = []
                control.progress(
                    variable, "extracting", source="reference", **block_context
                )
                ref = load_grid_points(
                    reference,
                    block,
                    variable,
                    values=None
                    if cells is None
                    else cells[:, block_start : block_start + block.size],
                )
                control.progress(variable, "regridding", source="hist", **block_context)
                hist = _clip(
                    regrid_to_points(
                        hist_all,
                        block,
                        block_qa,
                        source_label=f"{variable}:hist",
                        **options,
                    ),
                    spec,
                )
                control.progress(variable, "regridding", source="sim", **block_context)
                sim = _clip(
                    regrid_to_points(
                        sim_all,
                        block,
                        block_qa,
                        source_label=f"{variable}:sim",
                        **options,
                    ),
                    spec,
                )
                contexts.append(block_context)
                if selection.workers == 1:
                    part = _correct(
                        cfg,
                        spec,
                        ref,
                        hist,
                        sim,
                        block_qa,
                        compact=True,
                        control=control,
                        block_context=block_context,
                        plan=plan,
                    )
                    batch_parts.append((part, block_qa))
                else:
                    # Only materialized arrays go to workers, never open files or CLI callbacks.
                    tasks.append(
                        delayed(_correct_block_task)(
                            cfg, spec, ref, hist, sim, block_qa, plan
                        )
                    )

            if tasks:
                control.progress(
                    variable,
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
                    [
                        c
                        for c in block_qa
                        if not c.name.startswith("bounds:")
                        and (context["block_index"] == 1 or c.status != "pass")
                    ]
                )
                completed_cells += context["block_cells"]
                control.progress(
                    variable,
                    "block_finished",
                    **{**context, "completed_cells": completed_cells},
                )
            # Release prepared inputs before measuring the next batch's free RAM.
            del tasks, batch_parts
            ref = hist = sim = None
            start = batch_stop
    control.progress(variable, "combining")
    result = _finish(cfg, spec, parts, qa, grid=target)
    control.check_cancelled()
    return result


def _correct_block_task(
    cfg: PipelineConfig,
    spec: VariableSpec,
    ref: xr.DataArray,
    hist: xr.DataArray,
    sim: xr.DataArray,
    block_qa: list[QACheck],
    plan: CellPlan,
) -> tuple[dict, list[QACheck]]:
    """Return both results and QA; mutations in worker processes are not shared."""
    part = _correct(cfg, spec, ref, hist, sim, block_qa, compact=True, plan=plan)
    # xsdba's pickle state omits .ds; only the trained dataset is needed after
    # correction/evaluation, so use the same dataset wrapper as combined blocks.
    part["adj"] = None if part["adj"] is None else _CombinedAdjustment(part["adj"].ds)
    return part, block_qa


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


def _correct(
    cfg: PipelineConfig,
    spec: VariableSpec,
    ref: xr.DataArray,
    hist: xr.DataArray,
    sim: xr.DataArray,
    qa: list[QACheck],
    *,
    plan: CellPlan,
    compact: bool = False,
    control: RunControl | None = None,
    block_context: dict[str, int] | None = None,
) -> dict:
    """Units, time axis, training, adjustment and evaluation of one set of
    points (all stations, or one block of grid cells).

    With compact=True the results are kept as float32, halving memory for
    large grids. Outputs are materialized before returning, so a caller
    can truthfully report block completion even when xsdba returns lazy data.
    Progress stages include block_context when supplied.
    """
    if control is None:
        control = RunControl()
    context = block_context or {}
    variable = spec.name
    control.progress(variable, "preparing", **context)
    ref, hist, sim, units = harmonize(ref, hist, sim, spec.units, qa, name=variable)
    ref, hist, sim = align_daily(ref, hist, sim, qa, name=variable)

    calibration = cfg.periods.calibration
    ref_cal = _calibration_slice(ref, calibration, qa, label=f"{variable}:ref")
    hist_cal = _calibration_slice(hist, calibration, qa, label=f"{variable}:hist")

    control.progress(variable, "training", **context)
    res = run_cell(
        plan,
        ref=ref_cal.to_dataset(name=variable),
        hist=hist_cal.to_dataset(name=variable),
        sim=sim.to_dataset(name=variable),
        specs={variable: spec},
        units={variable: units},
        control=control,
        seed=_SEED,
        qa=qa,
    )
    control.progress(variable, "adjustment", **context)
    scen = res.scen[variable]
    adj = res.trained_obj
    control.progress(variable, "evaluation", **context)
    tables, series = _evaluate_block(
        spec,
        cfg.periods,
        plan,
        adj,
        res.zeros,
        ref,
        hist,
        ref_cal,
        hist_cal,
        units,
        qa,
        post_trained=res.post_trained,
    )
    if compact:
        scen = scen.astype("float32")
        series = [s.astype("float32") for s in series]
    control.check_cancelled()
    # Accessing .values for QA computes lazy arrays but does not retain them.
    scen.load()
    if adj is not None:
        adj.ds.load()
    for s in series:
        s.load()
    control.check_cancelled()
    return {
        "scen": scen,
        "adj": adj,
        "tables": tables,
        "series": series,
        "non_finite": _non_finite(scen, sim),
        "units": units,
        "calibration": _span(ref_cal),
        "clip_counts": res.clip_counts,
    }


class _CombinedAdjustment:
    """The trained factors of several blocks of cells, for writing adj.nc."""

    def __init__(self, ds: xr.Dataset):
        self.ds = ds


def _finish(
    cfg: PipelineConfig,
    spec: VariableSpec,
    parts: list[dict],
    qa: list[QACheck],
    *,
    grid: TargetGrid | None,
) -> dict:
    """Put the blocks back together and write the summary checks once."""
    variable = spec.name
    unit = "cell" if grid is not None else "station"
    scen = (
        parts[0]["scen"]
        if len(parts) == 1
        else cast(xr.DataArray, xr.concat([p["scen"] for p in parts], dim="station"))
    )
    if parts[0]["adj"] is None:
        adj = None
    elif len(parts) == 1:
        adj = parts[0]["adj"]
    else:
        adj = _CombinedAdjustment(
            cast(xr.Dataset, xr.concat([p["adj"].ds for p in parts], dim="station"))
        )

    if grid is not None:
        low, high = spec.bounds
        if low is not None or high is not None:
            n_below = sum(
                p.get("clip_counts", {}).get(variable, (0, 0))[0] for p in parts
            )
            n_above = sum(
                p.get("clip_counts", {}).get(variable, (0, 0))[1] for p in parts
            )
            n_total = n_below + n_above
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
                        message=(
                            f"Clipped {n_total} value(s) of '{variable}' to bounds "
                            f"[{low}, {high}] ({n_below} below, {n_above} above)."
                        ),
                        context=clip_context,
                    )
                )
            else:
                qa.append(
                    QACheck(
                        name=f"bounds:{variable}:ok",
                        status="pass",
                        message=(
                            f"All values of '{variable}' are within bounds "
                            f"[{low}, {high}]."
                        ),
                        context=clip_context,
                    )
                )

    non_finite = [s for p in parts for s in p["non_finite"]]
    if non_finite:
        qa.append(
            QACheck(
                name=f"adjust:{variable}:non_finite",
                status="warn",
                message=(
                    f"The correction of '{variable}' produced NaN/inf where the model had valid values at "
                    f"{len(non_finite)} {unit}(s), e.g. no observation in the calibration period: "
                    f"{non_finite[:10]}"
                ),
                context={"variable": variable, "stations": non_finite},
            )
        )

    qa.append(
        QACheck(
            name=f"train_adjust:{variable}:done",
            status="pass",
            message=(
                f"Completed train+adjust for '{variable}' using '{spec.method}' "
                f"(group={spec.group}{f', window={spec.window}' if spec.group == 'dayofyear' else ''}"
                + (
                    f", regrid={spec.regrid}, {len(parts)} block(s)"
                    if grid is not None
                    else ""
                )
                + ")."
            ),
            context={
                "variable": variable,
                "method": spec.method,
                "group": spec.group,
                "regrid": spec.regrid if grid is not None else None,
                "n_stations": int(scen.sizes.get("station", 0)),
                "calibration": parts[0]["calibration"],
                "sim": _span(scen),
            },
        )
    )

    evaluation, hist_eval = _summarize_evaluation(
        spec, cfg.periods, parts, qa, unit=unit
    )
    return {
        "scen": scen,
        "adj": adj,
        "evaluation": evaluation,
        "hist_eval": hist_eval,
        "grid": grid,
    }


def _check_method(
    spec: VariableSpec,
    qa: list[QACheck],
    override: CorrectionConfig | None = None,
) -> None:
    if spec.method not in METHODS:
        qa.append(
            QACheck(
                name=f"method:{spec.name}:unknown",
                status="fail",
                message=f"Method '{spec.method}' is not registered. Valid methods: {sorted(METHODS)}.",
                context={"variable": spec.name},
            )
        )
        raise ValueError(f"Invalid method for variable '{spec.name}': {spec.method!r}")

    method = get_method(spec.method)
    if method.family == "post":
        qa.append(
            QACheck(
                name=f"method:{spec.name}:invalid_family",
                status="fail",
                message=(
                    f"Method '{spec.method}' has family 'post' and is only valid in "
                    "'post:' (Phase 3)."
                ),
                context={
                    "variable": spec.name,
                    "method": spec.method,
                    "family": method.family,
                },
            )
        )
        raise ValueError(
            f"Method '{spec.method}' has family 'post' and cannot be used as a primary method."
        )

    if method.min_vars > 1:
        qa.append(
            QACheck(
                name=f"method:{spec.name}:min_vars",
                status="fail",
                message=(
                    f"Method '{spec.method}' requires at least {method.min_vars} "
                    "variables and needs 'targets:' (Phase 4)."
                ),
                context={
                    "variable": spec.name,
                    "method": spec.method,
                    "min_vars": method.min_vars,
                },
            )
        )
        raise ValueError(
            f"Method '{spec.method}' requires at least {method.min_vars} variables."
        )

    try:
        check_available(method)
    except ValueError as exc:
        qa.append(
            QACheck(
                name=f"method:{spec.name}:unavailable",
                status="fail",
                message=str(exc),
                context={
                    "variable": spec.name,
                    "method": spec.method,
                    "extra": method.extra,
                },
            )
        )
        raise

    if override is not None:
        for step in override.post:
            if step.method not in METHODS:
                qa.append(
                    QACheck(
                        name=f"post:{spec.name}:unknown",
                        status="fail",
                        message=(
                            f"Post-step method '{step.method}' is not registered. "
                            f"Valid methods: {sorted(METHODS)}."
                        ),
                        context={"variable": spec.name, "method": step.method},
                    )
                )
                raise ValueError(
                    f"Invalid post-step method for variable '{spec.name}': {step.method!r}"
                )
            post_spec = get_method(step.method)
            if post_spec.family != "post":
                qa.append(
                    QACheck(
                        name=f"post:{spec.name}:invalid_family",
                        status="fail",
                        message=(
                            f"Post-step method '{step.method}' must have family 'post', "
                            f"got '{post_spec.family}'."
                        ),
                        context={
                            "variable": spec.name,
                            "method": step.method,
                            "family": post_spec.family,
                        },
                    )
                )
                raise ValueError(
                    f"Post-step method '{step.method}' must have family 'post', "
                    f"got '{post_spec.family}'."
                )
            try:
                check_available(post_spec)
            except ValueError as exc:
                qa.append(
                    QACheck(
                        name=f"post:{spec.name}:unavailable",
                        status="fail",
                        message=str(exc),
                        context={
                            "variable": spec.name,
                            "method": step.method,
                            "extra": post_spec.extra,
                        },
                    )
                )
                raise


def _build_plan(cfg: PipelineConfig, spec: VariableSpec) -> CellPlan:
    override = cfg.correction.get(spec.name)
    train_params, adjust_params = resolve_method_params(
        spec,
        get_method_meta(spec.method),
        explicit_train=override.train_params if override else None,
        explicit_adjust=override.adjust_params if override else None,
    )
    return CellPlan(
        target_name=spec.name,
        variables=(spec.name,),
        method=spec.method,
        group=spec.group,
        window=spec.window,
        pool_dims=tuple(override.pool_dims) if override else (),
        train_params=tuple(train_params.items()),
        adjust_params=tuple(adjust_params.items()),
        post=(
            tuple(
                PostStep(
                    method=p.method,
                    train_params=tuple(p.train_params.items()),
                    adjust_params=tuple(p.adjust_params.items()),
                )
                for p in override.post
            )
            if override
            else ()
        ),
    )


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
        qa.append(
            QACheck(
                name=f"periods:{label}:calibration_empty",
                status="fail",
                message=(
                    f"No {label} data in the calibration period {period[0]}..{period[1]} "
                    f"(data covers {available['start']}..{available['end']})."
                ),
                context={
                    "variable": label.split(":")[0],
                    "period": list(period),
                    "available": available,
                },
            )
        )
        raise ValueError(
            f"No {label} data in the calibration period -- see QACheck 'periods:{label}:calibration_empty'."
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


def _evaluate_block(
    spec: VariableSpec,
    periods: PeriodsConfig,
    plan: CellPlan,
    adj: Any | None,
    zeros: ZeroHandling | None,
    ref: xr.DataArray,
    hist: xr.DataArray,
    ref_cal: xr.DataArray,
    hist_cal: xr.DataArray,
    units: str,
    qa: list[QACheck],
    *,
    post_trained: tuple[tuple[MethodSpec, Any, dict[str, Any]], ...] | None = None,
) -> tuple[dict[str, pd.DataFrame], list[xr.Dataset]]:
    """Apply the trained correction to hist over the calibration AND the
    validation period, and score each period separately.

    Calibration scores are in-sample (the correction was fitted on those
    days, so they show how well it CAN fit); validation scores are the
    independent check of how well it generalises. Each period is isolated:
    a failure becomes a 'warn' check, and the other period -- and the
    correction itself -- are kept.

    Returns ({period label: table}, [obs/raw/corrected Dataset per period]).
    """
    name = spec.name
    method = get_method(plan.method)
    wet_thresh = _wet_threshold(spec, units)
    specs_map = {name: spec}
    units_map = {name: units}
    ref_cal_ds = ref_cal.to_dataset(name=name)
    hist_cal_ds = hist_cal.to_dataset(name=name)
    tables: dict[str, pd.DataFrame] = {}
    series: list[xr.Dataset] = []

    for label, period in _evaluation_periods(periods):
        if (
            method.family == "adjust_only"
            and not method.supports_projection
            and label == "validation"
        ):
            qa.append(
                QACheck(
                    name=f"evaluation:{name}:validation:skipped",
                    status="warn",
                    message=(
                        f"Evaluation of the validation period skipped for '{name}': "
                        f"method '{plan.method}' does not support out-of-sample projection."
                    ),
                    context={"variable": name, "period_label": label},
                )
            )
            continue
        try:
            obs, raw = _slice(ref, period), _slice(hist, period)
            if obs.sizes["time"] == 0 or raw.sizes["time"] == 0:
                where = f"{period[0]}..{period[1]}" if period else "the common period"
                raise ValueError(f"no reference or model data in {where}")
            raw_ds = raw.to_dataset(name=name)
            if method.family == "train_adjust":
                corrected_ds = adjust_trained(
                    plan,
                    adj,
                    sim=raw_ds,
                    ref=ref_cal_ds,
                    hist=hist_cal_ds,
                    specs=specs_map,
                    units=units_map,
                    zeros=zeros,
                    post_trained=post_trained,
                )
            elif method.supports_projection:
                corrected_ds = run_cell(
                    plan,
                    ref=ref_cal_ds,
                    hist=hist_cal_ds,
                    sim=raw_ds,
                    specs=specs_map,
                    units=units_map,
                    seed=_SEED,
                    qa=[],
                ).scen
            else:
                corrected_ds = run_cell(
                    replace(plan, has_projection=False),
                    ref=ref_cal_ds,
                    hist=hist_cal_ds,
                    sim=raw_ds,
                    specs=specs_map,
                    units=units_map,
                    seed=_SEED,
                    qa=[],
                ).scen
            corrected = corrected_ds[name]
            obs_paired = obs_on_model_dates(obs, raw)
            tables[label] = evaluate(
                obs,
                raw,
                corrected,
                wet_thresh=wet_thresh,
                period=label,
                obs_paired=obs_paired,
            )
            series.append(
                paired_series(
                    obs, raw, corrected, period=label, obs_paired=obs_paired
                )
            )
        except Exception as exc:  # noqa: BLE001 -- isolate evaluation from the correction itself
            qa.append(
                QACheck(
                    name=f"evaluation:{name}:{label}:skipped",
                    status="warn",
                    message=f"Evaluation of the {label} period skipped for '{name}': {exc}",
                    context={"variable": name, "period_label": label},
                )
            )
    return tables, series


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
                    "and those scores are in-sample (optimistic). Set periods.validation for an "
                    "independent check."
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
