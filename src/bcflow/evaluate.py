"""Before/after evaluation: is the corrected model closer to the station
observations than the raw model?

Most metrics compare distributions per station, not day-by-day values. A GCM run
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
    annual_cycle_rmse    RMSE of 12 monthly climatological means        ideal 0 (VALUE)
    lag1_autocorr_diff   lag-1 autocorrelation(model) - (obs)           ideal 0 (VALUE)
    ks_stat              maximum empirical CDF distance                ideal 0 (KS)
    rx1day_bias          mean annual daily maximum(model) - (obs)      ideal 0 (ETCCDI)
    r95p_bias            mean annual total above obs wet-day p95,
                         model - obs                                   ideal 0 (ETCCDI)
    cdd_bias/cwd_bias    mean annual longest dry/wet spell,
                         model - obs                                   ideal 0 (ETCCDI)

The last four indices require a wet-day threshold. Annual indices and the
annual cycle require at least a full year of daily data.

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
- Zhang, X., et al. (2011). Indices for monitoring changes in extremes based
  on daily temperature and precipitation data. WIREs Climate Change, 2, 851-870.
  https://doi.org/10.1002/wcc.147
"""

from __future__ import annotations

from collections.abc import Callable

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
    "annual_cycle_rmse": 0.0,
    "lag1_autocorr_diff": 0.0,
    "ks_stat": 0.0,
    "rx1day_bias": 0.0,
    "r95p_bias": 0.0,
    "cdd_bias": 0.0,
    "cwd_bias": 0.0,
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


def _station_array(values: np.ndarray, da: xr.DataArray) -> xr.DataArray:
    return xr.DataArray(values, dims=("station",), coords={"station": da.station})


def _full_year_indices(da: xr.DataArray) -> list[np.ndarray]:
    """Indices of complete calendar years, including cftime calendars."""
    if da.sizes["time"] < 360:
        return []
    years = np.asarray(da.time.dt.year.values)
    months = np.asarray(da.time.dt.month.values)
    days = np.asarray(da.time.dt.day.values)
    days_in_month = np.asarray(da.time.dt.days_in_month.values)
    return [
        idx
        for year in np.unique(years)
        if (idx := np.flatnonzero(years == year)).size >= 360
        and months[idx[0]] == 1
        and days[idx[0]] == 1
        and months[idx[-1]] == 12
        and days[idx[-1]] == days_in_month[idx[-1]]
    ]


def _longest_run(condition: np.ndarray) -> np.ndarray:
    """Longest true run along time for every station; false and NaN break runs."""
    positions = np.arange(1, condition.shape[0] + 1)[:, None]
    last_break = np.maximum.accumulate(np.where(condition, 0, positions), axis=0)
    return np.where(condition, positions - last_break, 0).max(axis=0)


def _annual_stats(
    da: xr.DataArray, wet_thresh: float, r95_threshold: xr.DataArray
) -> dict[str, xr.DataArray]:
    values = np.asarray(da.transpose("time", "station").values, dtype=float)
    r95 = np.asarray(r95_threshold.sel(station=da.station).values, dtype=float)
    annual: dict[str, list[np.ndarray]] = {k: [] for k in ("rx1day", "r95p", "cdd", "cwd")}
    for idx in _full_year_indices(da):
        chunk = values[idx]
        valid = np.isfinite(chunk)
        has_data = valid.any(axis=0)
        rx1day = np.max(np.where(valid, chunk, -np.inf), axis=0)
        r95p = np.where(valid & (chunk > r95), chunk, 0).sum(axis=0)
        dry = _longest_run(valid & (chunk < wet_thresh))
        wet = _longest_run(valid & (chunk >= wet_thresh))
        for name, result in zip(annual, (rx1day, r95p, dry, wet), strict=True):
            defined = has_data & np.isfinite(r95) if name == "r95p" else has_data
            annual[name].append(np.where(defined, result, np.nan))
    result = {}
    for name, parts in annual.items():
        if parts:
            rows = np.stack(parts)
            count = np.isfinite(rows).sum(axis=0)
            mean = np.divide(
                np.nansum(rows, axis=0), count,
                out=np.full(da.sizes["station"], np.nan), where=count > 0,
            )
        else:
            mean = np.full(da.sizes["station"], np.nan)
        result[name] = _station_array(mean, da)
    return result


def _lag1(da: xr.DataArray) -> xr.DataArray:
    values = np.asarray(da.transpose("time", "station").values, dtype=float)
    if len(values) < 3:
        return _station_array(np.full(da.sizes["station"], np.nan), da)
    left, right = values[:-1], values[1:]
    valid = np.isfinite(left) & np.isfinite(right)
    count = valid.sum(axis=0)
    n = np.maximum(count, 1)
    mean_left = np.where(valid, left, 0).sum(axis=0) / n
    mean_right = np.where(valid, right, 0).sum(axis=0) / n
    x = np.where(valid, left - mean_left, 0)
    y = np.where(valid, right - mean_right, 0)
    denom = np.sqrt((x * x).sum(axis=0) * (y * y).sum(axis=0))
    result = np.divide((x * y).sum(axis=0), denom, out=np.full(len(n), np.nan), where=(count >= 2) & (denom > 0))
    return _station_array(result, da)


def _annual_cycle(da: xr.DataArray) -> xr.DataArray:
    if not _full_year_indices(da):
        return xr.DataArray(
            np.full((12, da.sizes["station"]), np.nan),
            dims=("month", "station"),
            coords={"month": np.arange(1, 13), "station": da.station},
        )
    return da.groupby("time.month").mean("time").reindex(month=np.arange(1, 13))


def _ks_stat(model: xr.DataArray, obs: xr.DataArray) -> xr.DataArray:
    """Exact two-sample KS distance, sorting station blocks rather than stations."""
    a = np.asarray(model.transpose("time", "station").values, dtype=float)
    b = np.asarray(obs.sel(station=model.station).transpose("time", "station").values, dtype=float)
    result = np.full(a.shape[1], np.nan)
    for start in range(0, a.shape[1], 64):
        aa, bb = a[:, start : start + 64], b[:, start : start + 64]
        na, nb = np.isfinite(aa).sum(axis=0), np.isfinite(bb).sum(axis=0)
        pooled = np.concatenate((aa, bb), axis=0)
        is_a = np.broadcast_to(
            np.arange(pooled.shape[0])[:, None] < len(aa), pooled.shape
        )
        order = np.argsort(np.where(np.isfinite(pooled), pooled, np.inf), axis=0)
        sorted_values = np.take_along_axis(pooled, order, axis=0)
        sorted_a = np.take_along_axis(is_a, order, axis=0)
        cdf_a = np.cumsum(sorted_a & np.isfinite(sorted_values), axis=0) / np.maximum(na, 1)
        cdf_b = np.cumsum(~sorted_a & np.isfinite(sorted_values), axis=0) / np.maximum(nb, 1)
        last_tie = np.r_[sorted_values[1:] != sorted_values[:-1], np.ones((1, sorted_values.shape[1]), dtype=bool)]
        distance = np.where(last_tie & np.isfinite(sorted_values), np.abs(cdf_a - cdf_b), 0).max(axis=0)
        result[start : start + aa.shape[1]] = np.where((na >= 2) & (nb >= 2), distance, np.nan)
    return _station_array(result, model)


def _obs_stats(obs: xr.DataArray, wet_thresh: float | None) -> dict[str, xr.DataArray]:
    """What the metrics need from the observations, computed once for raw AND corrected."""
    stats = {
        "mean": obs.mean("time"),
        "std": obs.std("time"),
        "quantiles": obs.quantile(_QQ_LEVELS, dim="time"),
        "annual_cycle": _annual_cycle(obs),
        "lag1": _lag1(obs),
        "series": obs,
    }
    if wet_thresh is not None:
        stats["wet_freq"] = _wet_freq(obs, wet_thresh)
        stats["r95_threshold"] = obs.where(obs >= wet_thresh).quantile(0.95, dim="time")
        stats.update(_annual_stats(obs, wet_thresh, stats["r95_threshold"]))
    return stats


def _metrics(
    model: xr.DataArray,
    obs: dict[str, xr.DataArray],
    obs_paired: xr.DataArray,
    wet_thresh: float | None,
) -> dict[str, xr.DataArray]:
    """Every metric, one value per station; `obs` comes from _obs_stats."""
    qm = model.quantile(_QQ_LEVELS, dim="time")
    annual = _annual_stats(model, wet_thresh, obs["r95_threshold"]) if wet_thresh is not None else {}
    stats = {
        "mean": model.mean("time"),
        "std": model.std("time"),
        "quantiles": qm,
        "annual_cycle": _annual_cycle(model),
        "lag1": _lag1(model),
        "rmse": np.sqrt(((model.reset_coords(drop=True) - obs_paired) ** 2).mean("time")),
        "ks": _ks_stat(model, obs["series"]),
        **annual,
    }
    metrics: dict[str, Callable[[], xr.DataArray]] = {
        "mean_bias": lambda: stats["mean"] - obs["mean"],
        "sd_ratio": lambda: stats["std"] / obs["std"],
        "qq_rmse": lambda: np.sqrt(((qm - obs["quantiles"]) ** 2).mean("quantile")),
        "rmse": lambda: stats["rmse"],
        "annual_cycle_rmse": lambda: np.sqrt(((stats["annual_cycle"] - obs["annual_cycle"]) ** 2).mean("month", skipna=False)),
        "lag1_autocorr_diff": lambda: stats["lag1"] - obs["lag1"],
        "ks_stat": lambda: stats["ks"],
    }
    metrics.update({
        f"q{round(q * 100)}_bias": lambda q=q: qm.sel(quantile=q, method="nearest")
        - obs["quantiles"].sel(quantile=q, method="nearest")
        for q in _REPORTED_QUANTILES
    })
    if wet_thresh is not None:
        metrics.update({
            "wet_day_freq_diff": lambda: _wet_freq(model, wet_thresh) - obs["wet_freq"],
            **{
                f"{name}_bias": lambda name=name: stats[name] - obs[name]
                for name in ("rx1day", "r95p", "cdd", "cwd")
            },
        })
    return {name: metric() for name, metric in metrics.items()}


def change_signal(
    hist: xr.DataArray,
    sim: xr.DataArray,
    scen_hist: xr.DataArray,
    scen_sim: xr.DataArray,
    kind: str,
) -> xr.Dataset:
    """Historical-to-scenario mean signal for raw and corrected simulations.

    Returns station-wise ``raw``, ``corrected``, and ``difference``
    (corrected minus raw). ``kind`` is '+' for a change or '*' for a ratio.
    """
    if kind not in ("+", "*"):
        raise ValueError("kind must be '+' or '*'")
    raw = sim.mean("time") - hist.mean("time") if kind == "+" else sim.mean("time") / hist.mean("time")
    corrected = scen_sim.mean("time") - scen_hist.mean("time") if kind == "+" else scen_sim.mean("time") / scen_hist.mean("time")
    return xr.Dataset({"raw": raw, "corrected": corrected, "difference": corrected - raw})


def evaluate(
    obs: xr.DataArray,
    raw: xr.DataArray,
    corrected: xr.DataArray,
    *,
    wet_thresh: float | None = None,
    period: str = "",
    obs_paired: xr.DataArray | None = None,
) -> pd.DataFrame:
    """Compare raw and corrected model to observations, per station.

    All three are (time, station) arrays over the SAME evaluation period.
    Returns a long table with columns:
        period, station, metric, ideal, raw, corrected, improved
    where `improved` is True if the corrected value is at least as close to
    the ideal as the raw one (None when a value is missing).
    """
    if obs_paired is None:
        obs_paired = obs_on_model_dates(
            obs, raw
        )  # raw and corrected share one time axis
    stats = _obs_stats(obs, wet_thresh)
    before = _metrics(raw, stats, obs_paired, wet_thresh)
    after = _metrics(corrected, stats, obs_paired, wet_thresh)

    # Whole arrays per metric, in the observations' station order (a .sel per
    # station and metric used to dominate the cost for grids of thousands of cells).
    stations = obs["station"].values
    names = [str(s) for s in stations]
    columns: dict[str, list] = {name: [] for name in COLUMNS}
    for metric, ideal in IDEAL.items():
        if metric not in before:
            continue
        raw_values = np.asarray(
            before[metric].sel(station=stations).values, dtype=float
        )
        new_values = np.asarray(after[metric].sel(station=stations).values, dtype=float)
        finite = np.isfinite(raw_values) & np.isfinite(new_values)
        closer = np.abs(new_values - ideal) <= np.abs(raw_values - ideal) + 1e-12
        columns["period"] += [period] * len(names)
        columns["station"] += names
        columns["metric"] += [metric] * len(names)
        columns["ideal"] += [ideal] * len(names)
        columns["raw"] += raw_values.tolist()
        columns["corrected"] += new_values.tolist()
        columns["improved"] += [
            bool(c) if f else None for c, f in zip(closer, finite, strict=True)
        ]
    return pd.DataFrame(columns, columns=COLUMNS)


def paired_series(
    obs: xr.DataArray,
    raw: xr.DataArray,
    corrected: xr.DataArray,
    *,
    period: str,
    obs_paired: xr.DataArray | None = None,
) -> xr.Dataset:
    """obs / raw / corrected on the model's dates, with a 'period' label per day.

    For plotting and for users who want to recompute their own scores.
    """
    if obs_paired is None:
        obs_paired = obs_on_model_dates(obs, raw)
    ds = xr.Dataset(
        {
            "obs": obs_paired,
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
    unit: str = "station",
) -> None:
    """One QA check for one evaluation period (label: 'calibration' | 'validation').

    `unit` names the rows: "station", or "cell" for a gridded reference.
    """
    period_txt = f"{period[0]}..{period[1]}" if period else "the full common period"
    deviation = {
        metric: {
            "raw": _mean_abs_diff(group["raw"], group["ideal"]),
            "corrected": _mean_abs_diff(group["corrected"], group["ideal"]),
        }
        for metric, group in table.groupby("metric", sort=False)
    }

    def arrow(metric: str) -> str:
        d = deviation.get(metric)
        return f"{d['raw']:.3g} -> {d['corrected']:.3g}" if d else "n/a"

    mean_bias = table[table["metric"] == "mean_bias"]
    # `improved` is None where a score could not be computed: only False counts.
    not_improved = sorted(
        str(s)
        for s, flag in zip(
            mean_bias["station"].tolist(), mean_bias["improved"].tolist()
        )
        if flag is False
    )
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
                    f"at {len(not_improved)}/{len(mean_bias)} {unit}(s) ({_first(not_improved)}); {scores}."
                ),
                context={**context, "stations": not_improved},
            )
        )
    else:
        qa.append(
            QACheck(
                name=f"evaluation:{name}:{label}",
                status="pass",
                message=f"{label.capitalize()} {period_txt} for '{name}', all {len(mean_bias)} {unit}(s): {scores}.",
                context=context,
            )
        )


def _mean_abs_diff(a: pd.Series | pd.DataFrame, b: pd.Series | pd.DataFrame) -> float:
    """Mean |a - b| over the stations where both are defined (NaN if none)."""
    d = np.abs(np.asarray(a, dtype=float).ravel() - np.asarray(b, dtype=float).ravel())
    d = d[np.isfinite(d)]
    return float(d.mean()) if d.size else float("nan")


def _first(names: list[str], n: int = 20) -> str:
    """The first n names, so a grid of thousands of cells keeps the message short."""
    return ", ".join(names[:n]) + (
        f", ... (+{len(names) - n})" if len(names) > n else ""
    )
