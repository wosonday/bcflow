"""Run the full correction for ONE variable.

Steps, in order:
    1. check the method name                         (methods.py)
    2. load ref, hist, sim                           (loaders.py)
    3. extract the grid cells nearest to each station (extract.py)
    4. bring all three to one unit                   (units.py)
    5. keep the calibration period for training      (config: periods)
    6. optional frequency adaptation, train, adjust sim (xsdba)
    7. evaluate before/after                         (evaluate.py)

It knows nothing about running several variables, or about isolating one
variable's failure from another's -- that is pipeline.py's job.
"""

from __future__ import annotations

import pint
import xarray as xr
import xsdba
from xsdba.units import convert_units_to

from .config_schema import VariableConfig
from .evaluate import evaluate, summarize
from .extract import extract_nearest
from .grouping import build_grouper
from .loaders import load_variable_sources
from .methods import METHOD_REGISTRY
from .qa import QACheck
from .units import harmonize

# Wet-day threshold when the config gives none: 1 mm/d, the ETCCDI convention
# (Zhang et al., 2011, WIREs Climate Change 2(6), 851-870, doi:10.1002/wcc.147).
_DEFAULT_WET_DAY = "1 mm d-1"


def run_variable(var_cfg: VariableConfig, qa: list[QACheck]) -> dict:
    """Bias-correct one variable.

    Returns {"scen": corrected sim (time, station),
             "adj": the trained xsdba object,
             "evaluation": before/after table (pandas.DataFrame) or None}.
    """
    name = var_cfg.name
    _check_method(var_cfg, qa)  # before loading: a typo should not cost a full data load

    ref, hist_raw, sim_raw, grid_cfg = load_variable_sources(var_cfg, qa)
    hist = extract_nearest(hist_raw, ref, grid_cfg, qa, source_label=f"{name}:hist")
    sim = extract_nearest(sim_raw, ref, grid_cfg, qa, source_label=f"{name}:sim")
    ref, hist, sim, units = harmonize(ref, hist, sim, var_cfg.units, qa, name=name)

    calibration = var_cfg.periods.calibration
    ref_cal = _calibration_slice(ref, calibration, qa, label=f"{name}:ref")
    hist_cal = _calibration_slice(hist, calibration, qa, label=f"{name}:hist")

    group = build_grouper(var_cfg)
    hist_train = hist_cal
    if var_cfg.adapt_freq:
        # adapt_freq_thresh is guaranteed by pydantic (config_schema.py).
        hist_train, _pth, _dP0 = xsdba.processing.adapt_freq(
            ref_cal, hist_cal, thresh=var_cfg.adapt_freq_thresh, group=group
        )
        qa.append(QACheck(
            name=f"adapt_freq:{name}:applied",
            status="pass",
            message=f"Completed frequency adaptation for '{name}' (thresh={var_cfg.adapt_freq_thresh}).",
            context={"variable": name},
        ))

    train_kwargs = {**var_cfg.train_params, "group": group, "kind": var_cfg.kind}
    adj = METHOD_REGISTRY[var_cfg.method].train(ref_cal, hist_train, **train_kwargs)
    scen = _adjust(adj, sim, var_cfg)

    qa.append(QACheck(
        name=f"train_adjust:{name}:done",
        status="pass",
        message=f"Completed train+adjust for '{name}' using '{var_cfg.method}'.",
        context={
            "variable": name,
            "n_stations": int(ref.sizes.get("station", 0)),
            "calibration": _span(ref_cal),
            "sim": _span(scen),
        },
    ))

    evaluation = _evaluate_safely(var_cfg, adj, ref, hist, units, qa)
    return {"scen": scen, "adj": adj, "evaluation": evaluation}


def _check_method(var_cfg: VariableConfig, qa: list[QACheck]) -> None:
    if var_cfg.method not in METHOD_REGISTRY:
        qa.append(QACheck(
            name=f"method:{var_cfg.name}:unknown",
            status="fail",
            message=f"Method '{var_cfg.method}' is not registered. Valid methods: {sorted(METHOD_REGISTRY)}.",
            context={"variable": var_cfg.name},
        ))
        raise ValueError(f"Invalid method for variable '{var_cfg.name}': {var_cfg.method!r}")


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


def _calibration_slice(da: xr.DataArray, period, qa: list[QACheck], *, label: str) -> xr.DataArray:
    out = _slice(da, period)
    if out.sizes["time"] == 0:
        available = _span(da)
        qa.append(QACheck(
            name=f"periods:{label}:calibration_empty",
            status="fail",
            message=(
                f"No {label} data in the calibration period {period[0]}..{period[1]} "
                f"(data covers {available['start']}..{available['end']})."
            ),
            context={"variable": label.split(":")[0], "period": list(period), "available": available},
        ))
        raise ValueError(f"No {label} data in the calibration period -- see QACheck 'periods:{label}:calibration_empty'.")
    return out


def _wet_threshold(var_cfg: VariableConfig, units: str) -> float | None:
    """Wet-day threshold in the data's units, or None if not meaningful."""
    if var_cfg.kind != "*" or not units:
        return None
    try:
        return float(convert_units_to(var_cfg.adapt_freq_thresh or _DEFAULT_WET_DAY, units))
    except pint.PintError:  # a '*' variable that is not a precipitation depth
        return None


def _evaluate_safely(var_cfg, adj, ref, hist, units: str, qa: list[QACheck]):
    """Evaluate on the validation period (or, failing that, in-sample).

    Evaluation must never discard a correction that succeeded, so any error
    here becomes a 'warn' check and evaluation=None.
    """
    name, periods = var_cfg.name, var_cfg.periods
    period = periods.validation or periods.calibration
    try:
        obs, raw = _slice(ref, period), _slice(hist, period)
        if obs.sizes["time"] == 0 or raw.sizes["time"] == 0:
            raise ValueError(f"no station or model data in the evaluation period {period[0]}..{period[1]}")
        corrected = _adjust(adj, raw, var_cfg)
        table = evaluate(obs, raw, corrected, wet_thresh=_wet_threshold(var_cfg, units))
        summarize(table, qa, name=name, period=period, in_sample=periods.validation is None)
        return table
    except Exception as exc:  # noqa: BLE001 -- isolate evaluation from the correction itself
        qa.append(QACheck(
            name=f"evaluation:{name}:skipped",
            status="warn",
            message=f"Evaluation skipped for '{name}': {exc}",
            context={"variable": name},
        ))
        return None
