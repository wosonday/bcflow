"""Build the xsdba Grouper from config, and auto-detect lat/lon dimension names.

The alias-dict pattern (instead of scanning a candidate list) is ported from
`swatprep/core/weather.py::station_column()` (repo wosonday/swat-preprocessing):
every column/dimension name is normalised to ONE canonical name through a
fixed mapping, which avoids false ambiguity when a dataset carries both an
original name and its alias (having both 'lat' and 'latitude' is not a conflict).
"""

from __future__ import annotations

from typing import Any, Hashable

import xarray as xr
from xsdba.base import Grouper

from .qa import QACheck

# Aliases for spatial dimension names -- extend this mapping for a new
# convention instead of adding if/elif branches.
_LATLON_ALIASES = {
    "latitude": "lat",
    "nav_lat": "lat",
    "y_latitude": "lat",
    "y": "lat",
    "longitude": "lon",
    "nav_lon": "lon",
    "x_longitude": "lon",
    "x": "lon",
}


def _canonical(name: str) -> str:
    key = name.strip().lower()
    return _LATLON_ALIASES.get(key, key)


def build_grouper(var_cfg) -> str | Grouper:
    """Build xsdba.base.Grouper from VariableConfig.group (validated by pydantic)."""
    g = var_cfg.group
    name = f"{g.dim}.{g.prop}" if g.prop else g.dim
    return Grouper(name, window=g.window, add_dims=g.add_dims)


def resolve_latlon_dims(
    da: xr.DataArray, grid_cfg, qa: list[QACheck], *, source_label: str = ""
) -> tuple[Hashable | Any, Hashable | Any]:
    """Return the (lat_dim, lon_dim) names that exist in da.

    Absolute priority: lat_dim/lon_dim declared explicitly in config.
    Otherwise every existing dim/coord is normalised through the alias dict
    to find the canonical 'lat'/'lon'. Not declared and not found -> clear fail.
    """
    tag = f" ({source_label})" if source_label else ""
    available = set(da.coords) | set(da.dims)
    canon_to_actual = {_canonical(n): n for n in available}

    lat_dim = grid_cfg.lat_dim
    lon_dim = grid_cfg.lon_dim
    auto_used = []

    if lat_dim is None:
        lat_dim = canon_to_actual.get("lat")
        if lat_dim:
            auto_used.append(f"lat_dim='{lat_dim}'")
    if lon_dim is None:
        lon_dim = canon_to_actual.get("lon")
        if lon_dim:
            auto_used.append(f"lon_dim='{lon_dim}'")

    if lat_dim is None or lon_dim is None:
        qa.append(QACheck(
            name=f"grouping:latlon_not_found{tag}",
            status="fail",
            message=(
                f"Could not detect lat/lon{tag}. "
                f"Found: {sorted(available)}. "
                "Accepted: lat/latitude/nav_lat/y_latitude and lon/longitude/nav_lon/x_longitude. "
                "Please set lat_dim/lon_dim in the config."
            ),
            context={"available": sorted(available)},
        ))
        raise ValueError(f"Could not detect lat/lon{tag} -- see QACheck 'grouping:latlon_not_found{tag}'.")

    if auto_used:
        qa.append(QACheck(
            name=f"grouping:latlon_auto_detected{tag}",
            status="pass",
            message=f"Auto-detected spatial dimensions{tag}: {', '.join(auto_used)}.",
        ))

    return lat_dim, lon_dim
