"""Per-variable correction defaults, keyed by swat-preprocessing variable names.

Station data follow the swatprep conventions: one CSV per station with a
`date` column and one column per variable, in fixed units (precip mm/day,
temperature degC, wind m/s, solar MJ/m2/day, humidity fraction 0-1). Each
variable therefore gets a sensible correction setup by default, and the
optional `correction:` block of config.yaml overrides any field for one
variable, e.g. `correction: {tmax: {group: dayofyear, window: 31}}`.

Defaults use Quantile Delta Mapping, which preserves the model's projected
changes in quantiles (Cannon et al., 2015, J. Climate 28(17), 6938-6959,
doi:10.1175/JCLI-D-14-00754.1), grouped by month.

Exact zeros (dry days) break the multiplicative methods: the adjustment
factor ref/hist of a quantile where hist is 0 is inf or NaN. As in Cannon
et al. (2015, section 3), values below a small trace threshold are replaced
by uniform random values below it before training and adjusting, and set
back to 0 afterwards (`jitter_under_thresh`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

VARIABLES = ("precip", "tmax", "tmin", "wind", "solar", "humidity")

GROUPS = ("dayofyear", "month", "none")


@dataclass(frozen=True)
class VariableSpec:
    """Everything needed to correct one variable."""

    name: str
    units: str  # units of the station data (swatprep convention)
    kind: str  # "+" additive (temperature), "*" multiplicative (precip, wind, ...)
    method: str = "quantile_delta_mapping"
    group: str = "month"  # "dayofyear" | "month" | "none"
    window: int = 1  # days around each day of year; only used with group="dayofyear"
    adapt_freq: bool = False
    adapt_freq_thresh: str | None = None
    train_params: dict[str, Any] = field(default_factory=lambda: {"nquantiles": 50})
    adjust_params: dict[str, Any] = field(
        default_factory=lambda: {"interp": "linear", "extrapolation": "constant"}
    )
    bounds: tuple[float | None, float | None] = (None, None)  # physical limits
    jitter_under_thresh: str | None = None  # trace threshold for exact zeros, e.g. "0.01 mm d-1"


DEFAULTS: dict[str, VariableSpec] = {
    "precip": VariableSpec(
        name="precip",
        units="mm d-1",
        kind="*",
        adapt_freq=True,  # fixes the "drizzle" bias: too many light-rain days
        adapt_freq_thresh="1 mm d-1",  # ETCCDI wet-day threshold
        adjust_params={"interp": "nearest", "extrapolation": "constant"},
        bounds=(0.0, None),
        jitter_under_thresh="0.01 mm d-1",
    ),
    "tmax": VariableSpec(name="tmax", units="degC", kind="+"),
    "tmin": VariableSpec(name="tmin", units="degC", kind="+"),
    "wind": VariableSpec(name="wind", units="m s-1", kind="*", bounds=(0.0, None)),
    "solar": VariableSpec(name="solar", units="MJ m-2 d-1", kind="*", bounds=(0.0, None)),
    "humidity": VariableSpec(name="humidity", units="1", kind="+", bounds=(0.0, 1.0)),
}


def resolve(name: str, overrides: dict[str, Any] | None = None) -> VariableSpec:
    """The default spec of `name`, with the given fields replaced."""
    return replace(DEFAULTS[name], **(overrides or {}))
