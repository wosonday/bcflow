"""Early warning for stations without enough data to train the correction.

Quantile mapping estimates each station's distribution from its own
observations in the calibration period. A station with few or no valid days
there gets an unreliable correction -- or, with none at all, an all-NaN
series -- and nothing in the output says why. These checks name such
stations BEFORE training, and they need only the station data and the
config, so `bcflow validate` runs them too, in seconds, without opening the
model files.

Two rules, both warnings (the stations are kept):

- coverage: the share of calendar days in the calibration period with a
  valid observation must reach `periods.min_coverage` (default 0.8, in the
  spirit of the 80% completeness rule for climate normals);
- length: a calibration period shorter than 30 years may not represent the
  climate. 30 years is the standard reference period for climate normals
  (WMO, 2017: WMO Guidelines on the Calculation of Climate Normals,
  WMO-No. 1203, Geneva).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import xarray as xr

from .config_schema import _span
from .qa import QACheck

CLIMATE_YEARS = 30
_DAYS_PER_YEAR = 365.25


def calibration_window(
    ref: xr.DataArray, period: tuple[str, str] | None
) -> xr.DataArray:
    """Station data on EVERY calendar day of the calibration period.

    Days without an observation become NaN, so a station that starts late
    or has gaps is counted as missing those days. Without a calibration
    period, the window is the stations' whole record. The result depends
    only on the station files and the config, so `validate` and `run`
    report the same numbers.
    """
    index = ref.indexes["time"]
    if period is None:
        start, end = index.min(), index.max()
    else:
        start, end = _span(period, "calibration")
    # floor("D") turns an end such as 2004-12-31 23:59:59 into that day.
    days = pd.date_range(
        pd.Timestamp(start), pd.Timestamp(end), freq="D", name="time"
    ).floor("D")
    return ref.reindex(time=days)


def coverage_table(ref_cal: xr.DataArray) -> pd.DataFrame:
    """Per station: valid days, days in the period, coverage (0-1), valid years.

    `ref_cal` holds every day of the calibration period on its time axis
    (missing observations as NaN), so its length is the denominator.
    """
    total = int(ref_cal.sizes["time"])
    valid = np.asarray(ref_cal.notnull().sum("time").values, dtype=int)
    return pd.DataFrame(
        {
            "station": [str(s) for s in ref_cal["station"].values],
            "valid_days": valid,
            "total_days": total,
            "coverage": valid / total if total else np.zeros(len(valid)),
            "valid_years": valid / _DAYS_PER_YEAR,
        }
    )


def coverage_table_from_counts(
    ids: list[str], valid: np.ndarray, total: int
) -> pd.DataFrame:
    """The same table from counts already made (a gridded reference is
    scanned a year at a time, never loaded whole)."""
    valid = np.asarray(valid, dtype=int)
    return pd.DataFrame(
        {
            "station": ids,
            "valid_days": valid,
            "total_days": total,
            "coverage": valid / total if total else np.zeros(len(valid)),
            "valid_years": valid / _DAYS_PER_YEAR,
        }
    )


def period_years(ref_cal: xr.DataArray) -> float:
    """Length of the calibration period, in years (first to last day)."""
    if ref_cal.sizes["time"] == 0:
        return 0.0
    return ref_cal.sizes["time"] / _DAYS_PER_YEAR


def check_coverage(
    ref: xr.DataArray,
    period: tuple[str, str] | None,
    qa: list[QACheck],
    *,
    name: str,
    min_coverage: float,
) -> pd.DataFrame:
    """Append the coverage and period-length checks for one variable; return the table.

    `ref` is the station data as loaded (standard calendar, daily).
    """
    ref_cal = calibration_window(ref, period)
    table = coverage_table(ref_cal)
    report_coverage(
        table, period_years(ref_cal), qa, name=name, min_coverage=min_coverage
    )
    return table


def report_coverage(
    table: pd.DataFrame,
    years: float,
    qa: list[QACheck],
    *,
    name: str,
    min_coverage: float,
    unit: str = "station",
) -> None:
    """The coverage and period-length checks from a coverage table.

    `unit` names what the rows are: "station", or "cell" for a gridded reference.
    """
    low = table[table["coverage"] < min_coverage]
    empty = low.loc[low["valid_days"] == 0, "station"].tolist()

    if low.empty:
        qa.append(
            QACheck(
                name=f"coverage:{name}:ok",
                status="pass",
                message=(
                    f"All {len(table)} {unit}(s) have at least {min_coverage:.0%} valid days "
                    f"in the calibration period for '{name}'."
                ),
                context={"variable": name, "min_coverage": min_coverage},
            )
        )
    else:
        # Rounded down, so one at 79.6% never reads "80%" in a list of stations below 80%.
        listed = ", ".join(
            f"{s} ({math.floor(c * 100)}%)"
            for s, c in zip(low["station"], low["coverage"])
        )
        note = (
            f" {len(empty)} of them have no valid day at all: their corrected series will be empty (NaN)."
            if empty
            else ""
        )
        qa.append(
            QACheck(
                name=f"coverage:{name}:low",
                status="warn",
                message=(
                    f"{len(low)}/{len(table)} {unit}(s) have less than {min_coverage:.0%} valid days "
                    f"in the calibration period for '{name}'; their correction is less reliable.{note} "
                    f"{unit.capitalize()}s: {_shorten(listed)}"
                ),
                context={
                    "variable": name,
                    "min_coverage": min_coverage,
                    "stations": [
                        {
                            "station": str(s),
                            "coverage": round(float(c), 4),
                            "valid_years": round(float(y), 2),
                        }
                        for s, c, y in zip(
                            low["station"], low["coverage"], low["valid_years"]
                        )
                    ],
                    "empty": empty,
                },
            )
        )

    if round(years, 1) < CLIMATE_YEARS:  # 30 calendar years are 29.998 x 365.25 days
        qa.append(
            QACheck(
                name=f"coverage:{name}:short_period",
                status="warn",
                message=(
                    f"The calibration period for '{name}' spans {years:.1f} years; at least "
                    f"{CLIMATE_YEARS} years are recommended so the distributions represent the "
                    "climate (WMO climate normals)."
                ),
                context={
                    "variable": name,
                    "years": round(years, 2),
                    "recommended": CLIMATE_YEARS,
                },
            )
        )


def _shorten(text: str, limit: int = 600) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(", ", 1)[0] + ", ..."
