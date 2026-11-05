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

from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    hist_period: tuple[str, str]
    sim_period: tuple[str, str]


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
