"""Build the xsdba Grouper of a variable, and find the lat/lon names of a grid.

Grouping decides which days are corrected together (one transfer function
per group):

    "month"      12 groups, Jan..Dec -- the default;
    "dayofyear"  365/366 groups, each pooled with the days within
                 +-window//2 of it (a moving window, e.g. window=31), which
                 gives a smooth seasonal cycle without monthly steps;
    "none"       one group for the whole year.
"""

from __future__ import annotations

from collections.abc import Hashable

import xarray as xr
from xsdba.base import Grouper

from .qa import QACheck
from .variables import VariableSpec

# Names tried in order when lat_name/lon_name is "auto". The order matters:
# a curvilinear grid has 2-D 'lat'/'lon' coords AND 1-D 'y'/'x' index dims,
# and the true geographic coords must win.
_LAT_NAMES = ("lat", "latitude", "nav_lat", "y_latitude", "y")
_LON_NAMES = ("lon", "longitude", "nav_lon", "x_longitude", "x")


def build_grouper(spec: VariableSpec) -> Grouper:
    """xsdba Grouper for spec.group ("dayofyear" | "month" | "none")."""
    if spec.group == "dayofyear":
        return Grouper("time.dayofyear", window=spec.window)
    if spec.group == "month":
        return Grouper("time.month")
    if spec.group == "none":
        return Grouper("time")
    raise ValueError(  # unreachable after pydantic validation
        f"Invalid group {spec.group!r} for '{spec.name}'. Valid groups: dayofyear, month, none."
    )


def _find(available: dict[str, Hashable], declared: str, candidates: tuple[str, ...]) -> Hashable | None:
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
