"""Put station and model data on ONE daily time axis before training.

Why this is needed: xsdba trains only when `ref` and `hist` share exactly
the same time values (all four registered methods have
`_allow_diff_training_times = False`). Real data rarely do out of the box:

- time of day: stations are stamped 00:00, while many models stamp the
  middle of the day, 12:00 (e.g. CNRM-ESM2-1), so "the same day" is two
  different instants and nothing matches;
- calendar: models often use 'noleap' or '360_day', stations the standard
  calendar, so the two time coordinates are not even the same type.

bcflow works on DAILY data, so every stamp is reduced to its calendar day,
station data is converted to the model's calendar, and ref/hist keep only
the days they have in common.
"""

from __future__ import annotations

import xarray as xr

from .qa import QACheck


def to_daily(
    da: xr.DataArray, qa: list[QACheck], *, label: str
) -> tuple[xr.DataArray, bool]:
    """Reduce every time stamp to 00:00 of its day.

    Returns (data, True if any stamp was moved). Raises if two values fall
    on the same day, i.e. the data are sub-daily and must be aggregated first.
    """
    index = da.indexes["time"]
    days = index.floor("D")
    if days.has_duplicates:
        qa.append(
            QACheck(
                name=f"time:{label}:sub_daily",
                status="fail",
                message=(
                    f"{label} has several values on the same day (sub-daily data); "
                    "bcflow expects one value per day -- aggregate to daily first."
                ),
                context={"variable": label.split(":")[0]},
            )
        )
        raise ValueError(
            f"{label} is not daily data -- see QACheck 'time:{label}:sub_daily'."
        )
    shifted = bool((days != index).any())
    return da.assign_coords(time=days), shifted


def align_daily(
    ref: xr.DataArray,
    hist: xr.DataArray,
    sim: xr.DataArray,
    qa: list[QACheck],
    *,
    name: str,
) -> tuple[xr.DataArray, xr.DataArray, xr.DataArray]:
    """Return (ref, hist, sim) where ref and hist share one identical daily axis.

    1. every stamp -> 00:00 of its day (ref, hist and sim);
    2. ref converted to hist's calendar when they differ (e.g. standard ->
       noleap drops 29 Feb from the station series);
    3. ref and hist reduced to the days both have.
    sim keeps its own period; it only gets step 1.
    """
    ref, ref_shifted = to_daily(ref, qa, label=f"{name}:ref")
    hist, hist_shifted = to_daily(hist, qa, label=f"{name}:hist")
    sim, sim_shifted = to_daily(sim, qa, label=f"{name}:sim")

    ref_calendar = ref["time"].dt.calendar
    model_calendar = hist["time"].dt.calendar
    if ref_calendar != model_calendar:
        ref = ref.convert_calendar(model_calendar, align_on="date")

    ref, hist = xr.align(ref, hist, join="inner")
    if ref.sizes["time"] == 0:
        qa.append(
            QACheck(
                name=f"time:{name}:no_common_days",
                status="fail",
                message=f"Station data and model hist for '{name}' have no day in common.",
                context={"variable": name},
            )
        )
        raise ValueError(
            f"No common days for '{name}' -- see QACheck 'time:{name}:no_common_days'."
        )

    notes = []
    shifted = [
        role
        for role, s in (
            ("ref", ref_shifted),
            ("hist", hist_shifted),
            ("sim", sim_shifted),
        )
        if s
    ]
    if shifted:
        notes.append(f"stamps of {', '.join(shifted)} moved to 00:00")
    if ref_calendar != model_calendar:
        notes.append(
            f"station data converted from '{ref_calendar}' to the model's '{model_calendar}' calendar"
        )
    t = ref["time"]
    first = t.isel(time=0).dt.strftime("%Y-%m-%d").item()
    last = t.isel(time=-1).dt.strftime("%Y-%m-%d").item()
    qa.append(
        QACheck(
            name=f"time:{name}:aligned",
            status="pass",
            message=(
                f"Time axis for '{name}': {int(t.size)} common days ({first}..{last})"
                + (f"; {'; '.join(notes)}." if notes else ".")
            ),
            context={
                "variable": name,
                "common_days": int(t.size),
                "first": first,
                "last": last,
                "shifted": shifted,
                "calendar": model_calendar,
                "ref_calendar": ref_calendar,
            },
        )
    )
    return ref, hist, sim
