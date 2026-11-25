"""Build the xsdba Grouper of a variable, and find the lat/lon names of a grid.

Grouping decides which days are corrected together (one transfer function
per group):

    "month"      12 groups, Jan..Dec -- the default;
    "season"     4 groups, DJF/MAM/JJA/SON;
    "dayofyear"  365/366 groups, each pooled with the days within
                 +-window//2 of it (a moving window, e.g. window=31), which
                 gives a smooth seasonal cycle without monthly steps;
    "none"       one group for the whole year.

`effective_samples()` and `sample_size_qa()` warn when a group holds too few
samples for the requested number of quantiles (a transfer function fitted on
fewer than ~3*nquantiles samples is mostly interpolation noise).
"""

from __future__ import annotations

from collections.abc import Hashable

import xarray as xr
from xsdba.base import Grouper

from .qa import QACheck
from .variables import GROUPS, VariableSpec

# Average length of a year in daily time steps, and how many groups one year
# is split into.
_DAYS_PER_YEAR = 365.25
_GROUPS_PER_YEAR: dict[str, float] = {
    "dayofyear": _DAYS_PER_YEAR,  # one group per calendar day
    "month": 12.0,
    "season": 4.0,
    "none": 1.0,
}

# Names tried in order when lat_name/lon_name is "auto". The order matters:
# a curvilinear grid has 2-D 'lat'/'lon' coords AND 1-D 'y'/'x' index dims,
# and the true geographic coords must win.
_LAT_NAMES = ("lat", "latitude", "nav_lat", "y_latitude", "y")
_LON_NAMES = ("lon", "longitude", "nav_lon", "x_longitude", "x")


def build_grouper(
    spec: VariableSpec | str,
    *,
    window: int | None = None,
    pool_dims: tuple[str, ...] = (),
) -> Grouper:
    """xsdba Grouper for a group name or a spec's group.

    `spec` is either a `VariableSpec` (its `group` is used) or a group name.
    `window=None` means "use the spec's own window": a `dayofyear` spec keeps
    its `spec.window`, so existing `build_grouper(spec)` calls behave exactly
    as before (for `month`/`none` the spec's window is not meaningful and a
    window of 1 is used). An explicit int always wins, including `window=1`.
    `pool_dims` become xsdba's `add_dims`, so groups are pooled over e.g. all
    stations.
    """
    if isinstance(spec, VariableSpec):
        group = spec.group
        # spec.window is documented as only used with group="dayofyear"; for
        # "month"/"none" the spec has no window of its own, so keep 1.
        default_win = spec.window if group == "dayofyear" else 1
        win = default_win if window is None else window
    else:
        group = spec
        win = 1 if window is None else window

    add_dims = list(pool_dims) or None
    if group == "dayofyear":
        return Grouper("time.dayofyear", window=win, add_dims=add_dims)
    if group == "month":
        return Grouper("time.month", window=win, add_dims=add_dims)
    if group == "season":
        return Grouper("time.season", window=win, add_dims=add_dims)
    if group == "none":
        return Grouper("time", window=win, add_dims=add_dims)
    label = spec.name if isinstance(spec, VariableSpec) else spec
    raise ValueError(  # unreachable after pydantic validation
        f"Invalid group {group!r} for '{label}'. Valid groups: {', '.join(GROUPS)}."
    )


def effective_samples(group: str, window: int, n_years: int) -> int:
    """Approximate number of samples in one group.

    One year contributes ~365 daily time steps split over the group's groups.
    Only for `group="dayofyear"` does the rolling `window` (in days/time
    steps) pool neighbouring days into the same group; for `month`/`season`/
    `none` the window is measured in days, not group units, so it does not
    widen the sample. The result is rounded and never negative.
    """
    try:
        groups_per_year = _GROUPS_PER_YEAR[group]
    except KeyError:
        raise ValueError(
            f"Unknown group {group!r}. Valid groups: {', '.join(GROUPS)}."
        ) from None
    span = max(1, window) if group == "dayofyear" else 1
    samples = _DAYS_PER_YEAR * max(0, n_years) * span / groups_per_year
    return max(0, round(samples))


def sample_size_qa(
    group: str,
    window: int,
    n_years: int,
    nquantiles: int,
    *,
    variable: str,
) -> QACheck:
    """One QACheck on whether a group has enough samples for `nquantiles`.

    Warns first when `window > 1` is applied to a `month`/`season` group: the
    window is measured in days (time steps), not months or seasons, so a
    window of 3 does not mean "3 months". Warns when the requested quantiles
    exceed a third of the samples in a group. Otherwise passes.
    """
    samples = effective_samples(group, window, n_years)
    context = {
        "variable": variable,
        "group": group,
        "window": window,
        "n_years": n_years,
        "nquantiles": nquantiles,
        "samples_per_group": samples,
    }
    if window > 1 and group in ("month", "season"):
        return QACheck(
            name=f"grouping:{variable}:window_units",
            status="warn",
            message=(
                f"{variable}: group={group!r} with window={window}. 'window' is measured "
                f"in days (time steps), not months/seasons, so it combines "
                f"~{window} neighbouring days into each group. Use window=1 with "
                f"group={group!r}, or group='dayofyear' for a moving window."
            ),
            context=context,
        )
    if nquantiles > samples // 3:
        return QACheck(
            name=f"grouping:{variable}:sample_size",
            status="warn",
            message=(
                f"{variable}: only ~{samples} samples per {group!r} group over "
                f"{n_years} year(s), but nquantiles={nquantiles} "
                f"(need samples // 3 = {samples // 3} or more). Widen the "
                "calibration period, coarsen the group, or lower nquantiles."
            ),
            context=context,
        )
    return QACheck(
        name=f"grouping:{variable}:sample_size",
        status="pass",
        message=(
            f"{variable}: ~{samples} samples per {group!r} group over "
            f"{n_years} year(s), enough for nquantiles={nquantiles} "
            f"(samples // 3 = {samples // 3})."
        ),
        context=context,
    )


def _find(
    available: dict[str, Hashable], declared: str, candidates: tuple[str, ...]
) -> Hashable | None:
    if declared != "auto":
        return available.get(declared.strip().lower())
    return next((available[c] for c in candidates if c in available), None)


def resolve_latlon_dims(
    da: xr.DataArray,
    lat_name: str,
    lon_name: str,
    qa: list[QACheck],
    *,
    source_label: str,
) -> tuple[Hashable, Hashable]:
    """Return the names of da's latitude and longitude coordinates.

    A declared name (not "auto") is used as given (case-insensitive);
    "auto" tries the usual names in _LAT_NAMES / _LON_NAMES.
    """
    available = {str(n).strip().lower(): n for n in (*da.coords, *da.dims)}
    lat = _find(available, lat_name, _LAT_NAMES)
    lon = _find(available, lon_name, _LON_NAMES)

    if lat is None or lon is None:
        found = sorted(str(n) for n in available.values())
        qa.append(
            QACheck(
                name=f"grid:{source_label}:latlon_not_found",
                status="fail",
                message=(
                    f"Could not find latitude/longitude in {source_label} "
                    f"(lat_name={lat_name!r}, lon_name={lon_name!r}). Found: {found}. "
                    "Set model.lat_name / model.lon_name in the config."
                ),
                context={"variable": source_label.split(":")[0], "available": found},
            )
        )
        raise ValueError(
            f"Could not find lat/lon in {source_label} -- see QACheck 'grid:{source_label}:latlon_not_found'."
        )
    return lat, lon
