"""Run the full correction for ONE variable.

Steps, in order:
    1. resolve the variable's settings: defaults + `correction:` overrides
       (variables.py), and check the method name    (methods.py)
    2. load the stations and the model hist/sim     (loaders.py)
    3. take the grid cell nearest to each station   (extract.py)
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

import numpy as np
import pandas as pd
import pint
import xarray as xr
import xsdba
from xsdba.units import convert_units_to

from .config_schema import PeriodsConfig, PipelineConfig
from .evaluate import evaluate, paired_series, summarize
from .extract import extract_nearest
from .grouping import build_grouper
from .loaders import load_model_variable, load_station_variable
from .methods import METHOD_REGISTRY
from .qa import QACheck
from .timeaxis import align_daily
from .units import harmonize
from .variables import VariableSpec

# Wet-day threshold for evaluating precipitation when the config gives none:
# 1 mm/d, the ETCCDI convention (Zhang et al., 2011, WIREs Climate Change
# 2(6), 851-870, doi:10.1002/wcc.147).
_DEFAULT_WET_DAY = "1 mm d-1"

# Fixed seed for the random steps (jitter of exact zeros, adapt_freq), so
# two runs on the same data write identical files.
_SEED = 0


def run_variable(cfg: PipelineConfig, variable: str, qa: list[QACheck]) -> dict:
    """Bias-correct one variable of a validated config.

    Returns {"scen": corrected sim (time, station),
             "adj": the trained xsdba object,
             "evaluation": before/after table with a 'period' column, or None,
             "hist_eval": obs / raw hist / corrected hist (xr.Dataset) over the
                          calibration and validation periods, or None}.
    """
    spec = cfg.spec(variable)
    # Before loading: a typo in the method name should not cost a full data load.
    _check_method(spec, qa)

    model = cfg.model
    ref = load_station_variable(cfg.stations, variable, spec.units, qa)
    grid = {"lat_name": model.lat_name, "lon_name": model.lon_name, "search_radius": model.search_radius}
    hist = extract_nearest(
        load_model_variable(model, "hist", variable, qa), ref, qa, source_label=f"{variable}:hist", **grid
    )
    sim = extract_nearest(
        load_model_variable(model, "sim", variable, qa), ref, qa, source_label=f"{variable}:sim", **grid
    )
    ref, hist, sim, units = harmonize(ref, hist, sim, spec.units, qa, name=variable)
    ref, hist, sim = align_daily(ref, hist, sim, qa, name=variable)

    calibration = cfg.periods.calibration
    ref_cal = _calibration_slice(ref, calibration, qa, label=f"{variable}:ref")
    hist_cal = _calibration_slice(hist, calibration, qa, label=f"{variable}:hist")

    zeros = _ZeroHandling(spec, units)
    ref_cal, hist_cal = zeros.jitter(ref_cal), zeros.jitter(hist_cal)

    group = build_grouper(spec)
    hist_train = hist_cal
    if spec.adapt_freq:
        thresh = spec.adapt_freq_thresh
        # Unreachable after pydantic validation (PipelineConfig requires a
        # threshold when adapt_freq is true); the check narrows `str | None`
        # to `str` for the type checker and guards direct, unvalidated calls.
        if thresh is None:
            raise ValueError(f"correction.{variable}: adapt_freq=true requires 'adapt_freq_thresh'.")
        # adapt_freq draws from NumPy's global generator; seed it for reproducibility.
        np.random.seed(_SEED)
        hist_train, _pth, _dP0 = xsdba.processing.adapt_freq(ref_cal, hist_cal, thresh=thresh, group=group)
        qa.append(
            QACheck(
                name=f"adapt_freq:{variable}:applied",
                status="pass",
                message=f"Completed frequency adaptation for '{variable}' (thresh={thresh}).",
                context={"variable": variable},
            )
        )

    train_kwargs = {**spec.train_params, "group": group, "kind": spec.kind}
    adj = METHOD_REGISTRY[spec.method].train(ref_cal, hist_train, **train_kwargs)
    scen = _adjust(adj, sim, spec, zeros)
    _check_finite(scen, sim, qa, name=variable)

    qa.append(
        QACheck(
            name=f"train_adjust:{variable}:done",
            status="pass",
            message=(
                f"Completed train+adjust for '{variable}' using '{spec.method}' "
                f"(group={spec.group}{f', window={spec.window}' if spec.group == 'dayofyear' else ''})."
            ),
            context={
                "variable": variable,
                "method": spec.method,
                "group": spec.group,
                "n_stations": int(ref.sizes.get("station", 0)),
                "calibration": _span(ref_cal),
                "sim": _span(scen),
            },
        )
    )

    evaluation, hist_eval = _evaluate_periods(spec, cfg.periods, adj, ref, hist, units, zeros, qa)
    return {"scen": scen, "adj": adj, "evaluation": evaluation, "hist_eval": hist_eval}


def _check_method(spec: VariableSpec, qa: list[QACheck]) -> None:
    if spec.method not in METHOD_REGISTRY:
        qa.append(
            QACheck(
                name=f"method:{spec.name}:unknown",
                status="fail",
                message=f"Method '{spec.method}' is not registered. Valid methods: {sorted(METHOD_REGISTRY)}.",
                context={"variable": spec.name},
            )
        )
        raise ValueError(f"Invalid method for variable '{spec.name}': {spec.method!r}")


class _ZeroHandling:
    """Jitter exact zeros below spec.jitter_under_thresh before xsdba sees
    the data, and set the values below it back to 0 afterwards.

    The same as xsdba.processing.jitter_under_thresh, but with a seeded
    random generator so two runs on the same data give identical files.
    """

    def __init__(self, spec: VariableSpec, units: str):
        self.thresh = (
            float(convert_units_to(spec.jitter_under_thresh, units)) if spec.jitter_under_thresh else None
        )
        self.rng = np.random.default_rng(_SEED)

    def jitter(self, da: xr.DataArray) -> xr.DataArray:
        if self.thresh is None:
            return da
        noise = self.rng.uniform(np.nextafter(0.0, 1.0), self.thresh, size=da.shape)
        return da.where(~((da < self.thresh) & da.notnull()), noise)

    def restore(self, da: xr.DataArray) -> xr.DataArray:
        if self.thresh is None:
            return da
        return da.where(~(da < self.thresh), 0.0)


def _check_finite(scen: xr.DataArray, sim: xr.DataArray, qa: list[QACheck], *, name: str) -> None:
    """Warn when the correction turned valid model values into NaN or inf.

    Typical cause: a station with no observation in the calibration period,
    so nothing could be trained for it and its whole series is NaN.
    """
    bad = ~np.isfinite(scen.values) & np.isfinite(sim.transpose(*scen.dims).values)
    lost = bad.sum(axis=scen.get_axis_num("time"))
    stations = [str(s) for s, n in zip(scen["station"].values, lost) if n]
    if stations:
        qa.append(
            QACheck(
                name=f"adjust:{name}:non_finite",
                status="warn",
                message=(
                    f"The correction of '{name}' produced NaN/inf where the model had valid values at "
                    f"{len(stations)} station(s), e.g. no observation in the calibration period: {stations[:10]}"
                ),
                context={"variable": name, "stations": stations, "n_values": int(bad.sum())},
            )
        )


def _adjust(adj, da: xr.DataArray, spec: VariableSpec, zeros: _ZeroHandling) -> xr.DataArray:
    out = zeros.restore(adj.adjust(zeros.jitter(da), **spec.adjust_params))
    low, high = spec.bounds
    if low is not None or high is not None:
        # Quantile mapping can step outside physical limits (e.g. negative
        # wind or humidity above 1 after extrapolation); clip them back.
        out = out.clip(min=low, max=high, keep_attrs=True)
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
        qa.append(
            QACheck(
                name=f"periods:{label}:calibration_empty",
                status="fail",
                message=(
                    f"No {label} data in the calibration period {period[0]}..{period[1]} "
                    f"(data covers {available['start']}..{available['end']})."
                ),
                context={"variable": label.split(":")[0], "period": list(period), "available": available},
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
        return float(convert_units_to(spec.adapt_freq_thresh or _DEFAULT_WET_DAY, units))
    except pint.PintError:
        return None


def _evaluation_periods(periods: PeriodsConfig) -> list[tuple[str, tuple[str, str] | None]]:
    """Calibration always (None = all common data), validation when set."""
    out: list[tuple[str, tuple[str, str] | None]] = [("calibration", periods.calibration)]
    if periods.validation is not None:
        out.append(("validation", periods.validation))
    return out


def _evaluate_periods(
    spec: VariableSpec,
    periods: PeriodsConfig,
    adj,
    ref,
    hist,
    units: str,
    zeros: _ZeroHandling,
    qa: list[QACheck],
):
    """Apply the trained correction to hist over the calibration AND the
    validation period, and score each period separately.

    Calibration scores are in-sample (the correction was fitted on those
    days, so they show how well it CAN fit); validation scores are the
    independent check of how well it generalises. Each period is isolated:
    a failure becomes a 'warn' check, and the other period -- and the
    correction itself -- are kept.

    Returns (table with a 'period' column | None, obs/raw/corrected Dataset | None).
    """
    name = spec.name
    wet_thresh = _wet_threshold(spec, units)
    tables, series = [], []

    for label, period in _evaluation_periods(periods):
        try:
            obs, raw = _slice(ref, period), _slice(hist, period)
            if obs.sizes["time"] == 0 or raw.sizes["time"] == 0:
                where = f"{period[0]}..{period[1]}" if period else "the common period"
                raise ValueError(f"no station or model data in {where}")
            corrected = _adjust(adj, raw, spec, zeros)
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

    if not tables:
        return None, None
    hist_eval = xr.concat(series, dim="time").sortby("time")
    return pd.concat(tables, ignore_index=True), hist_eval
