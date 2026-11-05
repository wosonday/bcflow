"""Pydantic schema for config.yaml.

Goal: configuration errors (missing field, wrong type, adapt_freq=true
without adapt_freq_thresh, both hist_sim and hist/sim declared...) must
surface as soon as the config is validated -- before any data file is
opened -- with a message naming the offending field.

Note: method names are NOT checked against METHOD_REGISTRY here, but in
adjust.py (via methods.py), so this module does not depend on xsdba and can
be used on its own (e.g. by a frontend form validating input before
sending it to the backend).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

import pandas as pd
from pydantic import BaseModel, BeforeValidator, Field, model_validator


def _as_date_text(value: Any) -> Any:
    """YAML reads an unquoted 2000 as an int and 2000-01-01 as a date; accept
    both as text, so non-coders do not have to know when to quote."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value


# An ISO date or a prefix of one: "2000", "2000-06", "2000-06-15".
DateText = Annotated[str, BeforeValidator(_as_date_text)]


class GroupConfig(BaseModel):
    dim: str = "time"
    prop: Literal["dayofyear", "month", "season"] | None = None
    window: int = 1
    add_dims: list[str] = Field(default_factory=list)


class RefPointConfig(BaseModel):
    station_layout: Literal["long", "wide"]
    metadata_path: str
    path: str
    match_key: str = "name"


class RasterSourceConfig(BaseModel):
    format: Literal["netcdf", "zarr"]
    path: str
    grid_type: Literal["regular_latlon", "rotated_pole"] = "regular_latlon"
    lat_dim: str | None = None
    lon_dim: str | None = None


class HistSimSourceConfig(RasterSourceConfig):
    hist_period: tuple[DateText, DateText]
    sim_period: tuple[DateText, DateText]


class VariableDataConfig(BaseModel):
    ref: RefPointConfig
    hist: RasterSourceConfig | None = None
    sim: RasterSourceConfig | None = None
    hist_sim: HistSimSourceConfig | None = None

    @model_validator(mode="after")
    def _exactly_one_source_mode(self) -> "VariableDataConfig":
        has_split = self.hist is not None and self.sim is not None
        has_combined = self.hist_sim is not None
        if has_split and has_combined:
            raise ValueError(
                "data: cannot declare both 'hist_sim' and the 'hist'/'sim' pair -- choose one."
            )
        if not has_split and not has_combined:
            raise ValueError(
                "data: must declare EITHER the 'hist' + 'sim' pair OR 'hist_sim' (combined source)."
            )
        if (self.hist is None) != (self.sim is None):
            raise ValueError("data: 'hist' and 'sim' must be declared together, not just one of them.")
        return self


def _span(period: tuple[str, str], field: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """First and last instant covered: ("2000", "2014") -> 2000-01-01 .. 2014-12-31."""
    try:
        start, end = pd.Period(period[0]).start_time, pd.Period(period[1]).end_time
    except ValueError:
        raise ValueError(f"periods.{field}: invalid date in {list(period)}; use e.g. '2000' or '2000-01-31'.") from None
    if start > end:
        raise ValueError(f"periods.{field}: start {period[0]!r} is after end {period[1]!r}.")
    return start, end


class PeriodsConfig(BaseModel):
    """Which years train the correction and which ones check it.

    calibration: data used to TRAIN (ref and hist). Default: all common data.
    validation:  held-out data used only to EVALUATE before/after; must not
                 overlap calibration. Without it, evaluation is in-sample.
    """

    calibration: tuple[DateText, DateText] | None = None
    validation: tuple[DateText, DateText] | None = None

    @model_validator(mode="after")
    def _ordered_and_disjoint(self) -> "PeriodsConfig":
        if self.validation is not None and self.calibration is None:
            raise ValueError(
                "periods: validation requires calibration -- otherwise training would use "
                "the validation years too and the check would not be independent."
            )
        spans = {f: _span(getattr(self, f), f) for f in ("calibration", "validation") if getattr(self, f)}
        if len(spans) == 2:
            (c0, c1), (v0, v1) = spans["calibration"], spans["validation"]
            if c0 <= v1 and v0 <= c1:
                raise ValueError("periods: calibration and validation overlap; validation must use other years.")
        return self


class VariableConfig(BaseModel):
    name: str
    method: str
    kind: Literal["+", "*"]
    units: str = ""
    adapt_freq: bool = False
    adapt_freq_thresh: str | None = None
    group: GroupConfig = Field(default_factory=GroupConfig)
    train_params: dict = Field(default_factory=dict)
    adjust_params: dict = Field(default_factory=dict)
    periods: PeriodsConfig = Field(default_factory=PeriodsConfig)
    data: VariableDataConfig

    @model_validator(mode="after")
    def _adapt_freq_needs_threshold(self) -> "VariableConfig":
        if self.adapt_freq and not self.adapt_freq_thresh:
            raise ValueError(
                f"variables[name={self.name!r}]: adapt_freq=true but 'adapt_freq_thresh' is missing "
                "(e.g. '0.05 mm d-1')."
            )
        return self


# Must list the same formats as writers._WRITERS (guarded by a test in
# test_writers.py); kept as a Literal here so this module stays free of
# xarray and can still validate a frontend form on its own.
OutputFormat = Literal["netcdf", "csv"]


class OutputConfig(BaseModel):
    """Where and how to write a run's results.

    `path` is resolved relative to the config file's directory when declared
    in config.yaml (config_loader treats every 'path' key that way). The
    default "outputs" is used as-is, i.e. relative to the current directory.
    """

    path: str = "outputs"
    formats: list[OutputFormat] = Field(default_factory=lambda: ["netcdf"], min_length=1)


class PipelineConfig(BaseModel):
    variables: list[VariableConfig] = Field(min_length=1)
    output: OutputConfig = Field(default_factory=OutputConfig)


def load_config(raw: dict) -> PipelineConfig:
    return PipelineConfig.model_validate(raw)
