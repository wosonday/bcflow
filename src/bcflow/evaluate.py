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
    rmse                 sqrt(mean((model(t) - obs(t))^2)) over common dates  ideal 0
                     (time-paired; read next to qq_rmse -- for a free-running
                     GCM it mostly measures weather timing, and restoring the
                     observed variance can raise it: von Storch, 1999)
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
- von Storch, H. (1999). On the use of "inflation" in statistical
  downscaling. Journal of Climate, 12(12), 3505-3506.
  https://doi.org/10.1175/1520-0442(1999)012<3505:OTUOII>2.0.CO;2
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
    "rmse": 0.0,
    "wet_day_freq_diff": 0.0,
}

COLUMNS = ["period", "station", "metric", "ideal", "raw", "corrected", "improved"]


def _date_keys(da: xr.DataArray) -> np.ndarray:
    """Calendar dates as "YYYY-MM-DD" strings; works for datetime64 and cftime."""
    return da["time"].dt.strftime("%Y-%m-%d").values


def obs_on_model_dates(obs: xr.DataArray, model: xr.DataArray) -> xr.DataArray:
    """Obs re-indexed onto the model's time axis, matched by calendar date.

    Dates the model has but obs lacks become NaN; obs dates the model lacks
    (e.g. 29 Feb for a 'noleap' model) are dropped. The result carries the
    MODEL's time coordinate, so it can be subtracted from the model directly.
    """
    by_date = obs.reset_coords(drop=True).assign_coords(time=_date_keys(obs))
    return by_date.reindex(time=_date_keys(model)).assign_coords(
        time=model["time"].values
    )


def _wet_freq(da: xr.DataArray, thresh: float) -> xr.DataArray:
    """Fraction of non-missing days with value >= thresh."""
    return (da >= thresh).where(da.notnull()).mean("time")


def _metrics(
    model: xr.DataArray,
    obs: xr.DataArray,
    obs_paired: xr.DataArray,
    wet_thresh: float | None,
) -> dict[str, xr.DataArray]:
    """Every metric, one value per station."""
    m = {
        "mean_bias": model.mean("time") - obs.mean("time"),
        "sd_ratio": model.std("time") / obs.std("time"),
    }
    qm = model.quantile(_QQ_LEVELS, dim="time")
    qo = obs.quantile(_QQ_LEVELS, dim="time")
    for q in _REPORTED_QUANTILES:
        m[f"q{round(q * 100)}_bias"] = qm.sel(quantile=q, method="nearest") - qo.sel(
            quantile=q, method="nearest"
        )
    m["qq_rmse"] = np.sqrt(((qm - qo) ** 2).mean("quantile"))  # type: ignore[reportArgumentType]
    # Time-paired: same calendar day only; NaN days (no obs) are skipped by mean().
    m["rmse"] = np.sqrt(  # type: ignore[reportArgumentType]
        ((model.reset_coords(drop=True) - obs_paired) ** 2).mean("time")
    )
    if wet_thresh is not None:
        m["wet_day_freq_diff"] = _wet_freq(model, wet_thresh) - _wet_freq(
            obs, wet_thresh
        )
    return m


def evaluate(
    obs: xr.DataArray,
    raw: xr.DataArray,
    corrected: xr.DataArray,
    *,
    wet_thresh: float | None = None,
    period: str = "",
) -> pd.DataFrame:
    """Compare raw and corrected model to observations, per station.

    All three are (time, station) arrays over the SAME evaluation period.
    Returns a long table with columns:
        period, station, metric, ideal, raw, corrected, improved
    where `improved` is True if the corrected value is at least as close to
    the ideal as the raw one (None when a value is missing).
    """
    obs_paired = obs_on_model_dates(obs, raw)  # raw and corrected share one time axis
    before = _metrics(raw, obs, obs_paired, wet_thresh)
    after = _metrics(corrected, obs, obs_paired, wet_thresh)

    rows = []
    for metric, ideal in IDEAL.items():
        if metric not in before:
            continue
        for station in obs["station"].values:
            r = float(before[metric].sel(station=station))
            c = float(after[metric].sel(station=station))
            improved = (
                abs(c - ideal) <= abs(r - ideal) + 1e-12
                if np.isfinite(r) and np.isfinite(c)
                else None
            )
            rows.append(
                {
                    "period": period,
                    "station": str(station),
                    "metric": metric,
                    "ideal": ideal,
                    "raw": r,
                    "corrected": c,
                    "improved": improved,
                }
            )
    return pd.DataFrame(rows, columns=COLUMNS)


def paired_series(
    obs: xr.DataArray, raw: xr.DataArray, corrected: xr.DataArray, *, period: str
) -> xr.Dataset:
    """obs / raw / corrected on the model's dates, with a 'period' label per day.

    For plotting and for users who want to recompute their own scores.
    """
    ds = xr.Dataset(
        {
            "obs": obs_on_model_dates(obs, raw),
            "raw": raw.reset_coords(drop=True),
            "corrected": corrected.reset_coords(drop=True),
        }
    )
    return ds.assign_coords(period=("time", np.full(ds.sizes["time"], period)))


def summarize(
    table: pd.DataFrame,
    qa: list[QACheck],
    *,
    name: str,
    label: str,
    period: tuple[str, str] | None,
) -> None:
    """One QA check for one evaluation period (label: 'calibration' | 'validation')."""
    period_txt = f"{period[0]}..{period[1]}" if period else "the full common period"
    deviation = {
        metric: {
            "raw": float((group["raw"] - group["ideal"]).abs().mean()),
            "corrected": float((group["corrected"] - group["ideal"]).abs().mean()),
        }
        for metric, group in table.groupby("metric", sort=False)
    }

    def arrow(metric: str) -> str:
        d = deviation.get(metric)
        return f"{d['raw']:.3g} -> {d['corrected']:.3g}" if d else "n/a"

    mean_bias = table[table["metric"] == "mean_bias"]
    not_improved = sorted(mean_bias.loc[mean_bias["improved"].eq(False), "station"])
    scores = f"|mean bias| {arrow('mean_bias')}, qq_rmse {arrow('qq_rmse')}, rmse {arrow('rmse')}"
    context = {
        "variable": name,
        "period_label": label,
        "period": list(period) if period else None,
        "mean_abs_deviation": deviation,
    }
    if not_improved:
        qa.append(
            QACheck(
                name=f"evaluation:{name}:{label}",
                status="warn",
                message=(
                    f"{label.capitalize()} {period_txt} for '{name}': correction did not reduce |mean bias| "
                    f"at {len(not_improved)}/{len(mean_bias)} station(s) ({', '.join(not_improved)}); {scores}."
                ),
                context={**context, "stations": not_improved},
            )
        )
    else:
        qa.append(
            QACheck(
                name=f"evaluation:{name}:{label}",
                status="pass",
                message=f"{label.capitalize()} {period_txt} for '{name}', all {len(mean_bias)} station(s): {scores}.",
                context=context,
            )
        )
