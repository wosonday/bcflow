"""Unit checks and conversion, so ref, hist and sim reach xsdba in one unit.

Each variable declares its target units in config (`units:`). Station CSV
files carry no unit metadata, so ref is taken to be in those units already.
Raster inputs (hist, sim) state theirs in the NetCDF/Zarr 'units' attribute
and are converted here.

Conversion uses xsdba's pint registry with CF-style unit strings ("K",
"degC", "mm d-1", "kg m-2 s-1"). One common case is not a pure unit change:
models usually store precipitation as a MASS flux (kg m-2 s-1) while
stations record a DEPTH rate (mm/d). The two are bridged by the density of
liquid water, 1000 kg m-3 (the "hydro" convention of CF tools such as
xclim), so 1 kg m-2 s-1 = 1 mm s-1 = 86400 mm d-1.
"""

from __future__ import annotations

import pint
import xarray as xr
from xsdba.units import convert_units_to, str2pint, units

from .qa import QACheck

_WATER_DENSITY = units.Quantity(1000, "kg m-3")


def _via_water_density(da: xr.DataArray, src: str, target: str) -> xr.DataArray:
    """Mass flux <-> depth rate through the density of water.

    Raises pint.DimensionalityError if neither direction yields `target`'s
    dimensions (i.e. the units are simply incompatible).
    """
    quantity, target_units = str2pint(src), str2pint(target).units
    for bridged in (quantity / _WATER_DENSITY, quantity * _WATER_DENSITY):
        try:
            factor = bridged.to(target_units).magnitude
        except pint.DimensionalityError:
            continue
        return (da * factor).assign_attrs({**da.attrs, "units": target})
    raise pint.DimensionalityError(src, target)


def to_units(da: xr.DataArray, target: str, qa: list[QACheck], *, label: str) -> tuple[xr.DataArray, str | None]:
    """Return (`da` in `target` units, the source unit if a conversion happened else None).

    - no 'units' attribute  -> warn, assume it is already in `target`
    - convertible           -> converted
    - incompatible/unknown  -> fail + raise ValueError
    """
    src = da.attrs.get("units", "")
    if not src:
        qa.append(QACheck(
            name=f"units:{label}:missing",
            status="warn",
            message=f"{label} has no 'units' attribute; assuming it is already in '{target}'.",
            context={"variable": label.split(":")[0], "assumed": target},
        ))
        return da.assign_attrs(units=target), None

    try:
        try:
            out = convert_units_to(da, target)
        except pint.DimensionalityError:
            out = _via_water_density(da, src, target)
        same = str2pint(src).units == str2pint(target).units
    except pint.PintError as exc:  # incompatible dimensions, or an unknown unit string
        qa.append(QACheck(
            name=f"units:{label}:incompatible",
            status="fail",
            message=f"Cannot convert {label} from '{src}' to '{target}': {exc}",
            context={"variable": label.split(":")[0], "from": src, "to": target},
        ))
        raise ValueError(
            f"Cannot convert {label} from {src!r} to {target!r} -- see QACheck 'units:{label}:incompatible'."
        ) from None

    return out, (None if same else src)


def harmonize(
    ref: xr.DataArray,
    hist: xr.DataArray,
    sim: xr.DataArray,
    declared: str,
    qa: list[QACheck],
    *,
    name: str,
) -> tuple[xr.DataArray, xr.DataArray, xr.DataArray, str]:
    """Bring ref, hist and sim to one unit. Returns (ref, hist, sim, units).

    The target is the variable's declared `units`; if none is declared,
    hist's own units are used and the station data is assumed to match
    (warned, since nothing can verify it).
    """
    target = declared or hist.attrs.get("units") or sim.attrs.get("units") or ""
    if not target:
        qa.append(QACheck(
            name=f"units:{name}:unknown",
            status="warn",
            message=f"No units declared for '{name}' and none found in hist/sim; unit checks skipped.",
            context={"variable": name},
        ))
        return ref, hist, sim, ""

    if not declared:
        qa.append(QACheck(
            name=f"units:{name}:undeclared",
            status="warn",
            message=(
                f"'units' is not set for '{name}'; using hist's units '{target}' and assuming "
                "the station data is in the same units. Set 'units' in the config to be explicit."
            ),
            context={"variable": name, "assumed": target},
        ))
        ref = ref.assign_attrs(units=target)

    converted: dict[str, str] = {}
    out = []
    for role, da in (("ref", ref), ("hist", hist), ("sim", sim)):
        da, src = to_units(da, target, qa, label=f"{name}:{role}")
        if src is not None:
            converted[role] = src
        out.append(da)

    if converted:
        detail = ", ".join(f"{role} from '{src}'" for role, src in converted.items())
        message = f"Units for '{name}': converted {detail} to '{target}'."
    else:
        message = f"Units for '{name}': all inputs already in '{target}'."
    qa.append(QACheck(
        name=f"units:{name}:summary",
        status="pass",
        message=message,
        context={"variable": name, "units": target, "converted": converted},
    ))
    return out[0], out[1], out[2], target
