"""Before/after evaluation: is the corrected model closer to the station
observations than the raw model?

Metrics compare DISTRIBUTIONS per station, not day-by-day values. A GCM run
is not synchronised with observed weather, so a time-paired error (e.g. an
RMSE between model day t and station day t) mostly measures weather timing,
which bias correction neither can nor should fix (Maraun, 2016). The
metrics below follow the "marginal aspects" of the VALUE validation
framework (Maraun et al., 2015):

    mean_bias            mean(model) - mean(obs)                       ideal 0
    sd_ratio             sd(model) / sd(obs)                           ideal 1
    q10/q50/q90/q99_bias quantile(model) - quantile(obs)               ideal 0
    qq_rmse              RMSE between model and obs quantiles 1..99 %  ideal 0
    wet_day_freq_diff    wet-day frequency(model) - (obs)              ideal 0
                         (only for kind '*' with a wet-day threshold)

Each metric is computed for the RAW and the CORRECTED model against the
same observed series and period, so each row reads "before -> after".

References
- Maraun, D. (2016). Bias correcting climate change simulations - a critical
  review. Current Climate Change Reports, 2(4), 211-220.
  https://doi.org/10.1007/s40641-016-0050-x
- Maraun, D., et al. (2015). VALUE: A framework to validate downscaling
  approaches for climate change studies. Earth's Future, 3(1), 1-14.
  https://doi.org/10.1002/2014EF000259
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr

from .qa import QACheck

_QQ_LEVELS = np.round(np.linspace(0.01, 0.99, 99), 2)
_REPORTED_QUANTILES = (0.10, 0.50, 0.90, 0.99)

IDEAL = {
    "mean_bias": 0.0,
    "sd_ratio": 1.0,
    **{f"q{round(q * 100)}_bias": 0.0 for q in _REPORTED_QUANTILES},
    "qq_rmse": 0.0,
    "wet_day_freq_diff": 0.0,
}


def _wet_freq(da: xr.DataArray, thresh: float) -> xr.DataArray:
    """Fraction of non-missing days with value >= thresh."""
    return (da >= thresh).where(da.notnull()).mean("time")


def _metrics(model: xr.DataArray, obs: xr.DataArray, wet_thresh: float | None) -> dict[str, xr.DataArray]:
    """Every metric, one value per station."""
    m = {
        "mean_bias": model.mean("time") - obs.mean("time"),
        "sd_ratio": model.std("time") / obs.std("time"),
    }
    qm = model.quantile(_QQ_LEVELS, dim="time")
    qo = obs.quantile(_QQ_LEVELS, dim="time")
    for q in _REPORTED_QUANTILES:
        m[f"q{round(q * 100)}_bias"] = qm.sel(quantile=q, method="nearest") - qo.sel(quantile=q, method="nearest")
    m["qq_rmse"] = np.sqrt(((qm - qo) ** 2).mean("quantile"))
    if wet_thresh is not None:
        m["wet_day_freq_diff"] = _wet_freq(model, wet_thresh) - _wet_freq(obs, wet_thresh)
    return m


def evaluate(
    obs: xr.DataArray,
    raw: xr.DataArray,
    corrected: xr.DataArray,
    *,
    wet_thresh: float | None = None,
) -> pd.DataFrame:
    """Compare raw and corrected model to observations, per station.

    All three are (time, station) arrays over the SAME evaluation period.
    Returns a long table with columns:
        station, metric, ideal, raw, corrected, improved
    where `improved` is True if the corrected value is at least as close to
    the ideal as the raw one (None when a value is missing).
    """
    before = _metrics(raw, obs, wet_thresh)
    after = _metrics(corrected, obs, wet_thresh)

    rows = []
    for metric, ideal in IDEAL.items():
        if metric not in before:
            continue
        for station in obs["station"].values:
            r = float(before[metric].sel(station=station))
            c = float(after[metric].sel(station=station))
            improved = abs(c - ideal) <= abs(r - ideal) + 1e-12 if np.isfinite(r) and np.isfinite(c) else None
            rows.append({
                "station": str(station),
                "metric": metric,
                "ideal": ideal,
                "raw": r,
                "corrected": c,
                "improved": improved,
            })
    return pd.DataFrame(rows, columns=["station", "metric", "ideal", "raw", "corrected", "improved"])


def summarize(
    table: pd.DataFrame,
    qa: list[QACheck],
    *,
    name: str,
    period: tuple[str, str] | None,
    in_sample: bool,
) -> None:
    """Turn the evaluation table into QA checks readable without opening it."""
    period_txt = f"{period[0]}..{period[1]}" if period else "the full common period"
    deviation = {
        metric: {
            "raw": float((group["raw"] - group["ideal"]).abs().mean()),
            "corrected": float((group["corrected"] - group["ideal"]).abs().mean()),
        }
        for metric, group in table.groupby("metric", sort=False)
    }

    if in_sample:
        qa.append(QACheck(
            name=f"evaluation:{name}:in_sample",
            status="warn",
            message=(
                f"No validation period set for '{name}': evaluated on the calibration data itself, "
                "so the scores are optimistic. Set periods.validation for an independent check."
            ),
            context={"variable": name},
        ))

    mean_bias = table[table["metric"] == "mean_bias"]
    not_improved = sorted(mean_bias.loc[mean_bias["improved"].eq(False), "station"])
    context = {"variable": name, "period": list(period) if period else None, "mean_abs_deviation": deviation}
    if not_improved:
        qa.append(QACheck(
            name=f"evaluation:{name}:mean_bias_not_reduced",
            status="warn",
            message=(
                f"Correction did not reduce |mean bias| at {len(not_improved)}/{len(mean_bias)} "
                f"station(s) for '{name}' over {period_txt}: {', '.join(not_improved)}."
            ),
            context={**context, "stations": not_improved},
        ))
    else:
        mb = deviation.get("mean_bias", {"raw": float("nan"), "corrected": float("nan")})
        qa.append(QACheck(
            name=f"evaluation:{name}:summary",
            status="pass",
            message=(
                f"Correction reduced |mean bias| at all {len(mean_bias)} station(s) for '{name}' "
                f"over {period_txt}: {mb['raw']:.3g} -> {mb['corrected']:.3g}."
            ),
            context=context,
        ))
