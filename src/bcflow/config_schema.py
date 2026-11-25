"""Pydantic schema for config.yaml.

Input conventions follow swat-preprocessing (swatprep), so the same station
files serve both tools:

    stations:   metadata CSV `id, name, lat, lon[, elev]` + one CSV per
                station (`file_pattern`) with `date, precip, tmax, ...`
    grid:       OR gridded observations (NetCDF/Zarr) as the reference; the
                model is then regridded onto that grid (regrid.py)
    variables:  which swatprep variables to correct
    model:      GCM files for hist and sim; `{var}` in a path is replaced by
                the variable's name in `var_map` (one file per variable, as
                in CMIP), a path without `{var}` is one file holding them all
    periods:    calibration / validation years, shared by every variable
    correction: optional per-variable overrides of the defaults in variables.py
    output:     where and in which formats to write the results

Goal: configuration errors must surface as soon as the config is validated
-- before any data file is opened -- with a message naming the field.
Method names are NOT checked against METHOD_REGISTRY here (that needs
xsdba), so this module stays light enough to validate a frontend form.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from typing import Annotated, Any, Literal

import pandas as pd
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .variables import VARIABLES, VariableSpec, resolve


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

# Must list the same names as variables.VARIABLES (guarded by a test).
VariableName = Literal["precip", "tmax", "tmin", "wind", "solar", "humidity"]


class StationsConfig(BaseModel):
    """Station observations, in swatprep layout."""

    model_config = ConfigDict(extra="forbid")

    metadata: str  # CSV: id, name, lat, lon[, elev]
    data_dir: str  # one CSV per station: date, precip, tmax, tmin, ...
    file_pattern: str = "{id}.csv"  # or "{name}.csv"

    @field_validator("file_pattern")
    @classmethod
    def _has_placeholder(cls, value: str) -> str:
        if "{id}" not in value and "{name}" not in value:
            raise ValueError(
                "file_pattern must contain {id} or {name}, e.g. '{id}.csv'."
            )
        return value


# Must list the same names as regrid.METHODS (guarded by a test).
RegridMethod = Literal["nearest", "idw", "bilinear", "bicubic", "conservative", "patch"]


class RegionConfig(BaseModel):
    """A lat/lon box that limits a gridded reference, e.g. to one basin."""

    model_config = ConfigDict(extra="forbid")

    lat: tuple[float, float]
    lon: tuple[float, float]

    @model_validator(mode="after")
    def _ordered(self) -> RegionConfig:
        for name in ("lat", "lon"):
            lo, hi = getattr(self, name)
            if lo >= hi:
                raise ValueError(f"region.{name}: min {lo} must be below max {hi}.")
        return self


class GridReferenceConfig(BaseModel):
    """Gridded observations used as the reference instead of stations."""

    model_config = ConfigDict(extra="forbid")

    path: str  # one NetCDF/Zarr holding every variable
    var_map: dict[str, str]  # variable name -> name in the file
    # Units of a variable when the file's own 'units' attribute is missing or
    # wrong (e.g. CDO's placeholder "var_unit"): {precip: "mm d-1", tmax: degC}.
    units: dict[str, str] = Field(default_factory=dict)
    lat_name: str = "auto"
    lon_name: str = "auto"
    time_name: str = "auto"
    region: RegionConfig | None = None
    block_size: int = Field(
        default=500, ge=1
    )  # cells corrected together; lower it to save memory
    workers: Literal["auto"] | Annotated[int, Field(ge=1, strict=True)] = "auto"
    memory_fraction: float = Field(default=0.6, gt=0, le=1)


class ModelConfig(BaseModel):
    """The climate model to correct (NetCDF or Zarr)."""

    model_config = ConfigDict(extra="forbid")

    hist: str  # historical run; may contain {var}
    sim: str  # run to correct (e.g. a scenario); may contain {var}
    var_map: dict[str, str]  # variable name -> name in the file
    lat_name: str = "auto"
    lon_name: str = "auto"
    time_name: str = "auto"
    search_radius: int = Field(
        1, ge=0, le=5
    )  # cells; used when the nearest cell is empty
    # Units of a variable when the model file's 'units' attribute is missing or wrong.
    units: dict[str, str] = Field(default_factory=dict)

    def path(self, which: Literal["hist", "sim"], variable: str) -> str:
        """File of `variable` for the hist or sim run."""
        return getattr(self, which).replace("{var}", self.var_map[variable])


class PostConfig(BaseModel):
    """Post-processing step applied after primary bias adjustment (shape-only)."""

    model_config = ConfigDict(extra="forbid")

    method: str
    train_params: dict[str, Any] = Field(default_factory=dict)
    adjust_params: dict[str, Any] = Field(default_factory=dict)


class TargetConfig(BaseModel):
    """Correction unit covering one (univariate) or several (multivariate) variables."""

    model_config = ConfigDict(extra="forbid")

    name: str
    variables: list[str] = Field(min_length=1)
    method: str = "quantile_delta_mapping"
    group: Literal["dayofyear", "month", "season", "none"] = "month"
    window: int = Field(default=1, ge=1)
    pool_dims: list[str] = Field(default_factory=list)
    train_params: dict[str, Any] = Field(default_factory=dict)
    adjust_params: dict[str, Any] = Field(default_factory=dict)
    post: list[PostConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_target(self) -> TargetConfig:
        if not self.name.strip():
            raise ValueError("target name must not be empty or whitespace.")
        duplicated = sorted({v for v in self.variables if self.variables.count(v) > 1})
        if duplicated:
            raise ValueError(
                f"target {self.name!r}: variables listed more than once: {duplicated}."
            )
        if self.window > 1 and self.group == "none":
            raise ValueError(
                "window > 1 cannot be used with group='none' (single whole-year group)."
            )
        if self.method == "mbcn" and self.group != "none":
            raise ValueError(
                "method 'mbcn' requires group='none' (MBCn does not support grouped adjustment)."
            )
        return self


class CustomVariableConfig(BaseModel):
    """Schema for a user-defined variable outside the built-in VARIABLES."""

    model_config = ConfigDict(extra="forbid")

    units: str
    kind: Literal["+", "*"]
    bounds: tuple[float | None, float | None] = (None, None)
    adapt_freq: bool = False
    adapt_freq_thresh: str | None = None
    jitter_under_thresh: str | None = None
    regrid: RegridMethod = "bilinear"
    regrid_params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_bounds(self) -> CustomVariableConfig:
        lo, hi = self.bounds
        if self.kind == "*" and (lo is None or lo < 0.0):
            raise ValueError(
                f"kind '*' requires a non-negative lower bound, got {lo!r}."
            )
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(
                f"bounds: lower bound {lo} must be <= upper bound {hi}."
            )
        return self


class CorrectionConfig(BaseModel):
    """Overrides of one variable's defaults (see variables.py); all optional."""

    model_config = ConfigDict(extra="forbid")

    method: str | None = None
    group: Literal["dayofyear", "month", "season", "none"] | None = None
    window: int | None = Field(None, ge=1)
    pool_dims: list[str] = Field(default_factory=list)
    adapt_freq: bool | None = None
    adapt_freq_thresh: str | None = None
    train_params: dict[str, Any] | None = None
    adjust_params: dict[str, Any] | None = None
    post: list[PostConfig] = Field(default_factory=list)
    regrid: RegridMethod | None = None  # only used with a gridded reference
    regrid_params: dict[str, Any] | None = None  # e.g. idw: {neighbours: 4, power: 2}

    @model_validator(mode="after")
    def _no_window_with_group_none(self) -> CorrectionConfig:
        if self.window is not None and self.window > 1 and self.group == "none":
            raise ValueError(
                "window > 1 cannot be used with group='none' (single whole-year group)."
            )
        return self


def _span(period: tuple[str, str], field: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """First and last instant covered: ("2000", "2014") -> 2000-01-01 .. 2014-12-31."""
    message = f"periods.{field}: invalid date in {list(period)}; use e.g. '2000' or '2000-01-31'."
    try:
        first, last = pd.Period(period[0]), pd.Period(period[1])
    except ValueError:
        raise ValueError(message) from None
    if not isinstance(first, pd.Period) or not isinstance(last, pd.Period):  # "NaT"
        raise ValueError(message)
    start, end = first.start_time, last.end_time
    if start > end:
        raise ValueError(
            f"periods.{field}: start {period[0]!r} is after end {period[1]!r}."
        )
    return start, end


class PeriodsConfig(BaseModel):
    """Which years train the correction and which ones check it.

    calibration: data used to TRAIN (ref and hist). Default: all common data.
    validation:  held-out data used only to EVALUATE before/after; must not
                 overlap calibration. Without it, evaluation is in-sample.
    min_coverage: share of calibration days a station needs with a valid
                 observation; stations below it are reported (coverage.py)
                 but still corrected.
    """

    model_config = ConfigDict(extra="forbid")

    calibration: tuple[DateText, DateText] | None = None
    validation: tuple[DateText, DateText] | None = None
    min_coverage: float = Field(default=0.8, gt=0, le=1)

    @model_validator(mode="after")
    def _ordered_and_disjoint(self) -> PeriodsConfig:
        if self.validation is not None and self.calibration is None:
            raise ValueError(
                "periods: validation requires calibration -- otherwise training would use "
                "the validation years too and the check would not be independent."
            )
        spans = {
            f: _span(getattr(self, f), f)
            for f in ("calibration", "validation")
            if getattr(self, f)
        }
        if len(spans) == 2:
            (c0, c1), (v0, v1) = spans["calibration"], spans["validation"]
            if c0 <= v1 and v0 <= c1:
                raise ValueError(
                    "periods: calibration and validation overlap; validation must use other years."
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

    model_config = ConfigDict(extra="forbid")

    path: str = "outputs"
    formats: list[OutputFormat] = Field(
        default_factory=lambda: ["netcdf"], min_length=1
    )


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stations: StationsConfig | None = None
    grid: GridReferenceConfig | None = None
    variables: list[str] = Field(min_length=1)
    custom_variables: dict[str, CustomVariableConfig] = Field(default_factory=dict)
    model: ModelConfig
    periods: PeriodsConfig = Field(default_factory=PeriodsConfig)
    correction: dict[str, CorrectionConfig] = Field(default_factory=dict)
    targets: list[TargetConfig] | None = None
    output: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="after")
    def _consistent(self) -> PipelineConfig:
        if (self.stations is None) == (self.grid is None):
            raise ValueError(
                "Set exactly one reference: 'stations' (station CSV files) or 'grid' "
                "(gridded observations)."
            )
        for name in self.custom_variables:
            if not name.strip():
                raise ValueError(
                    "custom_variables: variable name must not be empty or whitespace."
                )
            if name in VARIABLES:
                raise ValueError(
                    f"custom_variables: {name!r} collides with built-in variable; "
                    f"use correction.{name} to override."
                )
        allowed = set(VARIABLES) | set(self.custom_variables)
        unknown_vars = [v for v in self.variables if v not in allowed]
        if unknown_vars:
            raise ValueError(
                f"variables: unknown variable(s) {unknown_vars}; declare them in "
                f"custom_variables or use {list(VARIABLES)}."
            )
        mapping_checks = [
            ("correction", self.correction),
            ("model.var_map", self.model.var_map),
            ("model.units", self.model.units),
        ]
        if self.grid is not None:
            mapping_checks.extend(
                [
                    ("grid.var_map", self.grid.var_map),
                    ("grid.units", self.grid.units),
                ]
            )
        for field_name, mapping in mapping_checks:
            unknown = sorted(set(mapping) - allowed)
            if unknown:
                raise ValueError(
                    f"{field_name}: unknown variable(s) {unknown}; declare them in "
                    f"custom_variables or use {list(VARIABLES)}."
                )
        if self.grid is not None:
            missing = [v for v in self.variables if v not in self.grid.var_map]
            if missing:
                raise ValueError(
                    f"grid.var_map has no entry for {missing}; add e.g. "
                    "{precip: pr, tmax: tasmax, tmin: tasmin}."
                )
        duplicated = sorted({v for v in self.variables if self.variables.count(v) > 1})
        if duplicated:
            raise ValueError(f"variables: listed more than once: {duplicated}.")
        unmapped = [v for v in self.variables if v not in self.model.var_map]
        if unmapped:
            raise ValueError(
                f"model.var_map has no entry for {unmapped}; add e.g. "
                "{precip: pr, tmax: tasmax, tmin: tasmin}."
            )
        unused = sorted(set(self.correction) - set(self.variables))
        if unused:
            raise ValueError(f"correction: {unused} not in variables {self.variables}.")
        for v in self.variables:
            spec = self.spec(v)
            if spec.adapt_freq and not spec.adapt_freq_thresh:
                raise ValueError(
                    f"correction.{v}: adapt_freq=true needs 'adapt_freq_thresh' (e.g. '1 mm d-1')."
                )
            if spec.group == "none" and spec.window > 1:
                raise ValueError(
                    f"correction.{v}: window > 1 cannot be used with group='none'."
                )
        if self.targets is not None:
            if not self.targets:
                raise ValueError(
                    "targets: must not be empty when provided; omit 'targets' to use per-variable defaults."
                )
            target_names = [t.name for t in self.targets]
            dup_names = sorted({n for n in target_names if target_names.count(n) > 1})
            if dup_names:
                raise ValueError(f"targets: duplicate target name(s): {dup_names}.")
            declared = set(self.variables)
            unknown_target_vars = sorted(
                {v for t in self.targets for v in t.variables if v not in declared}
            )
            if unknown_target_vars:
                raise ValueError(
                    f"targets: variable(s) {unknown_target_vars} are not in variables {self.variables}."
                )
            seen_vars: set[str] = set()
            overlapping: set[str] = set()
            for target in self.targets:
                for v in target.variables:
                    if v in seen_vars:
                        overlapping.add(v)
                    seen_vars.add(v)
            if overlapping:
                raise ValueError(
                    f"targets: variable(s) {sorted(overlapping)} appear in more than one target."
                )
            missing_target_vars = [v for v in self.variables if v not in seen_vars]
            if missing_target_vars:
                raise ValueError(
                    f"targets: variable(s) {missing_target_vars} in variables are not covered by any target."
                )
        return self

    def resolved_targets(self) -> list[TargetConfig]:
        """Return configured targets, or derive one univariate target per variable."""
        if self.targets is not None:
            return list(self.targets)
        resolved: list[TargetConfig] = []
        for v in self.variables:
            spec = self.spec(v)
            override = self.correction.get(v)
            resolved.append(
                TargetConfig(
                    name=v,
                    variables=[v],
                    method=spec.method,
                    group=spec.group,  # type: ignore[arg-type]
                    window=spec.window,
                    pool_dims=list(override.pool_dims) if override else [],
                    train_params=(
                        dict(override.train_params)
                        if override and override.train_params is not None
                        else {}
                    ),
                    adjust_params=(
                        dict(override.adjust_params)
                        if override and override.adjust_params is not None
                        else {}
                    ),
                    post=list(override.post) if override else [],
                )
            )
        return resolved

    def spec(self, variable: str) -> VariableSpec:
        """Defaults of `variable` merged with its `correction:` overrides."""
        override = self.correction.get(variable)
        overrides = (
            override.model_dump(exclude_none=True, exclude={"post", "pool_dims"})
            if override
            else {}
        )
        if variable in self.custom_variables:
            base = VariableSpec(
                name=variable,
                **self.custom_variables[variable].model_dump(),
            )
            return replace(base, **overrides) if overrides else base
        return resolve(variable, overrides or None)


def load_config(raw: dict) -> PipelineConfig:
    return PipelineConfig.model_validate(raw)
