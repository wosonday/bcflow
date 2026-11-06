"""Run the full correction for ONE variable.

Steps, in order:
    1. check the method name                         (methods.py)
    2. load ref, hist, sim                           (loaders.py)
    3. extract the grid cells nearest to each station (extract.py)
    4. bring all three to one unit                   (units.py)
       and onto one daily time axis                  (timeaxis.py)
    5. keep the calibration period for training      (config: periods)
    6. optional frequency adaptation, train, adjust sim (xsdba)
    7. apply the correction to hist over the calibration and validation
       periods, and evaluate before/after for each   (evaluate.py)

It knows nothing about running several variables, or about isolating one
variable's failure from another's -- that is pipeline.py's job.
"""

from __future__ import annotations

import pandas as pd
import pint
import xarray as xr
import xsdba
from xsdba.units import convert_units_to

from .config_schema import VariableConfig
from .evaluate import evaluate, paired_series, summarize
from .extract import extract_nearest
from .grouping import build_grouper
from .loaders import load_variable_sources
from .methods import METHOD_REGISTRY
from .qa import QACheck
from .timeaxis import align_daily
from .units import harmonize

# Wet-day threshold when the config gives none: 1 mm/d, the ETCCDI convention
# (Zhang et al., 2011, WIREs Climate Change 2(6), 851-870, doi:10.1002/wcc.147).
_DEFAULT_WET_DAY = "1 mm d-1"


def run_variable(var_cfg: VariableConfig, qa: list[QACheck]) -> dict:
    """Bias-correct one variable.

    Returns {"scen": corrected sim (time, station),
             "adj": the trained xsdba object,
             "evaluation": before/after table with a 'period' column, or None,
             "hist_eval": obs / raw hist / corrected hist (xr.Dataset) over the
                          calibration and validation periods, or None}.
    """
    name = var_cfg.name
    # Before loading: a typo in the method name should not cost a full data load.
    _check_method(var_cfg, qa)

    ref, hist_raw, sim_raw, grid_cfg = load_variable_sources(var_cfg, qa)
    hist = extract_nearest(hist_raw, ref, grid_cfg, qa, source_label=f"{name}:hist")
    sim = extract_nearest(sim_raw, ref, grid_cfg, qa, source_label=f"{name}:sim")
    ref, hist, sim, units = harmonize(ref, hist, sim, var_cfg.units, qa, name=name)
    ref, hist, sim = align_daily(ref, hist, sim, qa, name=name)

    calibration = var_cfg.periods.calibration
    ref_cal = _calibration_slice(ref, calibration, qa, label=f"{name}:ref")
    hist_cal = _calibration_slice(hist, calibration, qa, label=f"{name}:hist")

    group = build_grouper(var_cfg)
    hist_train = hist_cal
    if var_cfg.adapt_freq:
        thresh = var_cfg.adapt_freq_thresh
        # Unreachable after pydantic validation (config_schema.py requires a
        # threshold when adapt_freq is true); the check narrows `str | None`
        # to `str` for the type checker and guards direct, unvalidated calls.
        if thresh is None:
            raise ValueError(
                f"variables[name={name!r}]: adapt_freq=true requires 'adapt_freq_thresh'."
            )
        hist_train, _pth, _dP0 = xsdba.processing.adapt_freq(
            ref_cal, hist_cal, thresh=thresh, group=group
        )
        qa.append(
            QACheck(
                name=f"adapt_freq:{name}:applied",
                status="pass",
                message=f"Completed frequency adaptation for '{name}' (thresh={var_cfg.adapt_freq_thresh}).",
                context={"variable": name},
            )
        )

    train_kwargs = {**var_cfg.train_params, "group": group, "kind": var_cfg.kind}
    adj = METHOD_REGISTRY[var_cfg.method].train(ref_cal, hist_train, **train_kwargs)
    scen = _adjust(adj, sim, var_cfg)

    qa.append(
        QACheck(
            name=f"train_adjust:{name}:done",
            status="pass",
            message=f"Completed train+adjust for '{name}' using '{var_cfg.method}'.",
            context={
                "variable": name,
                "n_stations": int(ref.sizes.get("station", 0)),
                "calibration": _span(ref_cal),
                "sim": _span(scen),
            },
        )
    )

    evaluation, hist_eval = _evaluate_periods(var_cfg, adj, ref, hist, units, qa)
    return {"scen": scen, "adj": adj, "evaluation": evaluation, "hist_eval": hist_eval}


def _check_method(var_cfg: VariableConfig, qa: list[QACheck]) -> None:
    if var_cfg.method not in METHOD_REGISTRY:
        qa.append(
            QACheck(
                name=f"method:{var_cfg.name}:unknown",
                status="fail",
                message=f"Method '{var_cfg.method}' is not registered. Valid methods: {sorted(METHOD_REGISTRY)}.",
                context={"variable": var_cfg.name},
            )
        )
        raise ValueError(
            f"Invalid method for variable '{var_cfg.name}': {var_cfg.method!r}"
        )


def _adjust(adj, da: xr.DataArray, var_cfg: VariableConfig) -> xr.DataArray:
    out = adj.adjust(da, **var_cfg.adjust_params)
    # xsdba's output dim order depends on Python's per-process string hashing
    # (it builds dims from a set), so fix it: identical files on every run.
    return out.transpose("time", ...)


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


def _wet_threshold(var_cfg: VariableConfig, units: str) -> float | None:
    """Wet-day threshold in the data's units, or None if not meaningful."""
    if var_cfg.kind != "*" or not units:
        return None
    try:
        return float(
            convert_units_to(var_cfg.adapt_freq_thresh or _DEFAULT_WET_DAY, units)
        )
    except pint.PintError:  # a '*' variable that is not a precipitation depth
        return None


def _evaluation_periods(periods) -> list[tuple[str, tuple[str, str] | None]]:
    """Calibration always (None = all common data), validation when set."""
    out = [("calibration", periods.calibration)]
    if periods.validation is not None:
        out.append(("validation", periods.validation))
    return out


def _evaluate_periods(var_cfg, adj, ref, hist, units: str, qa: list[QACheck]):
    """Apply the trained correction to hist over the calibration AND the
    validation period, and score each period separately.

    Calibration scores are in-sample (the correction was fitted on those
    days, so they show how well it CAN fit); validation scores are the
    independent check of how well it generalises. Each period is isolated:
    a failure becomes a 'warn' check, and the other period -- and the
    correction itself -- are kept.

    Returns (table with a 'period' column | None, obs/raw/corrected Dataset | None).
    """
    name = var_cfg.name
    wet_thresh = _wet_threshold(var_cfg, units)
    tables, series = [], []

    for label, period in _evaluation_periods(var_cfg.periods):
        try:
            obs, raw = _slice(ref, period), _slice(hist, period)
            if obs.sizes["time"] == 0 or raw.sizes["time"] == 0:
                where = f"{period[0]}..{period[1]}" if period else "the common period"
                raise ValueError(f"no station or model data in {where}")
            corrected = _adjust(adj, raw, var_cfg)
            table = evaluate(obs, raw, corrected, wet_thresh=wet_thresh, period=label)
            summarize(table, qa, name=name, label=label, period=period)
            tables.append(table)
            series.append(paired_series(obs, raw, corrected, period=label))
        except Exception as exc:  # noqa: BLE001 -- isolate evaluation from the correction itself
            qa.append(
                QACheck(
                    name=f"evaluation:{name}:{label}:skipped",
                    status="warn",
                    message=f"Evaluation of the {label} period skipped for '{name}': {exc}",
                    context={"variable": name, "period_label": label},
                )
            )

    if var_cfg.periods.validation is None:
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

    if not tables:
        return None, None
    hist_eval = xr.concat(series, dim="time").sortby("time")
    return pd.concat(tables, ignore_index=True), hist_eval
