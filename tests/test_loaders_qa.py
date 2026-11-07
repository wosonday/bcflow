"""Loaders: the swatprep station layout, and their warn / fail branches.

Each test damages a copy of the toy data on purpose, then checks the
QACheck and the behaviour: carry on with the rest, or stop clearly.
"""

from __future__ import annotations

import pandas as pd
import pytest
import xarray as xr

from bcflow.config_schema import ModelConfig, StationsConfig
from bcflow.loaders import (
    load_model_variable,
    load_station_metadata,
    load_station_variable,
)
from bcflow.qa import QACheck


def _stations(toy_paths, **changes) -> StationsConfig:
    fields = {"metadata": str(toy_paths["stations_meta"]), "data_dir": str(toy_paths["weather_dir"])}
    return StationsConfig(**{**fields, **changes})


def test_station_variable_loads_with_coords(toy_paths):
    qa: list[QACheck] = []
    da = load_station_variable(_stations(toy_paths), "tmax", "degC", qa)

    assert da.dims == ("time", "station")
    assert list(da["station"].values) == ["ST01", "ST02", "ST03"]
    assert list(da["name"].values) == ["hanoi", "danang", "hcmc"]
    assert float(da["lat"].sel(station="ST01")) == 21.03
    assert da.attrs["units"] == "degC"
    assert qa[-1].name == "stations:tmax:loaded" and qa[-1].status == "pass"


def test_metadata_headers_any_case_and_aliases(toy_paths, tmp_path):
    meta = pd.read_csv(toy_paths["stations_meta"], dtype=str)
    meta.columns = ["ID", " Name", "LATITUDE", "LONG", "ELEVATION"]
    path = tmp_path / "SWAT_master.csv"
    meta.to_csv(path, index=False)

    out = load_station_metadata(_stations(toy_paths, metadata=str(path)))

    assert list(out.index) == ["ST01", "ST02", "ST03"]
    assert {"name", "lat", "lon", "elev"} <= set(out.columns)


def test_ids_with_leading_zeros_stay_text(toy_paths, tmp_path):
    meta = pd.read_csv(toy_paths["stations_meta"], dtype=str)
    meta["id"] = ["001", "002", "003"]
    path = tmp_path / "meta.csv"
    meta.to_csv(path, index=False)
    assert list(load_station_metadata(_stations(toy_paths, metadata=str(path))).index) == [
        "001",
        "002",
        "003",
    ]


def test_metadata_missing_column_is_clear(toy_paths, tmp_path):
    path = tmp_path / "meta.csv"
    pd.read_csv(toy_paths["stations_meta"]).drop(columns=["lat"]).to_csv(path, index=False)
    with pytest.raises(ValueError, match="lat"):
        load_station_metadata(_stations(toy_paths, metadata=str(path)))


def test_missing_station_file_warns_and_continues(toy_paths):
    (toy_paths["weather_dir"] / "ST02.csv").unlink()
    qa: list[QACheck] = []

    da = load_station_variable(_stations(toy_paths), "precip", "mm d-1", qa)

    assert da.sizes["station"] == 2
    warn = next(c for c in qa if c.name == "stations:precip:missing_file")
    assert warn.status == "warn" and warn.context["stations"] == ["ST02"]


def test_missing_column_in_one_station_warns(toy_paths):
    f = toy_paths["weather_dir"] / "ST03.csv"
    pd.read_csv(f).drop(columns=["tmax"]).to_csv(f, index=False)
    qa: list[QACheck] = []

    da = load_station_variable(_stations(toy_paths), "tmax", "degC", qa)

    assert list(da["station"].values) == ["ST01", "ST02"]
    assert any(c.name == "stations:tmax:missing_column" for c in qa)


def test_no_station_has_the_variable_fails(toy_paths):
    qa: list[QACheck] = []
    with pytest.raises(ValueError, match="No station data for 'wind'"):
        load_station_variable(_stations(toy_paths), "wind", "m s-1", qa)
    assert qa[-1].name == "stations:wind:none_loaded" and qa[-1].status == "fail"


def test_file_pattern_by_name(toy_paths):
    for sid, name in [("ST01", "hanoi"), ("ST02", "danang"), ("ST03", "hcmc")]:
        (toy_paths["weather_dir"] / f"{sid}.csv").rename(toy_paths["weather_dir"] / f"{name}_daily.csv")
    qa: list[QACheck] = []
    da = load_station_variable(_stations(toy_paths, file_pattern="{name}_daily.csv"), "tmax", "degC", qa)
    assert da.sizes["station"] == 3


def test_minus_99_is_flagged(toy_paths):
    f = toy_paths["weather_dir"] / "ST01.csv"
    df = pd.read_csv(f)
    df.loc[10, "precip"] = -99
    df.to_csv(f, index=False)
    qa: list[QACheck] = []

    load_station_variable(_stations(toy_paths), "precip", "mm d-1", qa)

    assert any(c.name == "stations:precip:minus_99" and c.status == "warn" for c in qa)


def _model(toy_paths, **changes) -> ModelConfig:
    fields = {
        "hist": str(toy_paths["hist_template"]),
        "sim": str(toy_paths["sim_template"]),
        "var_map": {"precip": "pr", "tmax": "tasmax"},
    }
    return ModelConfig(**{**fields, **changes})


def test_model_file_per_variable(toy_paths):
    da = load_model_variable(_model(toy_paths), "hist", "tmax", [])
    assert da.name == "tasmax" and "time" in da.dims


def test_model_single_file_with_all_variables(toy_paths, tmp_path):
    merged = tmp_path / "all_vars_hist.nc"
    with xr.open_dataset(toy_paths["hist_pr"]) as pr, xr.open_dataset(toy_paths["hist_tasmax"]) as tx:
        xr.merge([pr, tx]).to_netcdf(merged)
    model = _model(toy_paths, hist=str(merged))

    assert load_model_variable(model, "hist", "precip", []).name == "pr"
    assert load_model_variable(model, "hist", "tmax", []).name == "tasmax"


def test_model_missing_file_fails_clearly(toy_paths, tmp_path):
    qa: list[QACheck] = []
    with pytest.raises(ValueError, match="not found"):
        load_model_variable(_model(toy_paths, sim=str(tmp_path / "nope_{var}.nc")), "sim", "precip", qa)
    assert qa[-1].name == "model:precip:sim:missing_file" and qa[-1].status == "fail"


def test_model_missing_variable_fails_clearly(toy_paths):
    qa: list[QACheck] = []
    # one file for every variable, without {var}: the file exists, the name does not
    model = _model(
        toy_paths, hist=str(toy_paths["hist_pr"]), var_map={"precip": "precipitation", "tmax": "tasmax"}
    )
    with pytest.raises(ValueError, match="precipitation"):
        load_model_variable(model, "hist", "precip", qa)
    assert qa[-1].name == "model:precip:hist:missing_variable"
    assert qa[-1].context["available"] == ["pr"]


def test_model_time_axis_is_renamed(toy_paths, tmp_path):
    path = tmp_path / "renamed_tasmax.nc"
    with xr.open_dataset(toy_paths["hist_tasmax"]) as ds:
        ds.load().rename({"time": "valid_time"}).to_netcdf(path)
    da = load_model_variable(_model(toy_paths, hist=str(path)), "hist", "tmax", [])
    assert "time" in da.dims and "valid_time" not in da.dims
