"""Early warning for stations with too few calibration days (coverage.py)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
import yaml
from pydantic import ValidationError

from bcflow.cli import main
from bcflow.config_schema import load_config
from bcflow.coverage import calibration_window, check_coverage
from bcflow.pipeline import run_pipeline
from bcflow.qa import QACheck


def _ref(time: pd.DatetimeIndex, columns: dict[str, np.ndarray]) -> xr.DataArray:
    return xr.DataArray(
        np.column_stack(list(columns.values())),
        dims=("time", "station"),
        coords={"time": time, "station": list(columns)},
    )


def test_low_and_empty_stations_are_named():
    time = pd.date_range("1981-01-01", "2010-12-31")  # 30 years
    n = len(time)
    half = np.where(np.arange(n) < n // 2, 1.0, np.nan)
    ref = _ref(time, {"FULL": np.ones(n), "HALF": half, "EMPTY": np.full(n, np.nan)})
    qa: list[QACheck] = []

    table = check_coverage(ref, ("1981", "2010"), qa, name="tmax", min_coverage=0.8)

    assert table.set_index("station").loc["HALF", "coverage"] == pytest.approx(
        0.5, abs=1e-3
    )
    (low,) = [c for c in qa if c.name == "coverage:tmax:low"]
    assert low.status == "warn"
    assert [s["station"] for s in low.context["stations"]] == ["HALF", "EMPTY"]
    assert low.context["empty"] == ["EMPTY"]
    assert "NaN" in low.message
    assert not any(
        c.name == "coverage:tmax:short_period" for c in qa
    )  # exactly 30 years


def test_all_stations_complete_passes():
    time = pd.date_range("1981-01-01", "2010-12-31")
    qa: list[QACheck] = []
    check_coverage(
        _ref(time, {"A": np.ones(len(time))}),
        ("1981", "2010"),
        qa,
        name="tmax",
        min_coverage=0.8,
    )
    assert [c.name for c in qa] == ["coverage:tmax:ok"]


def test_late_start_counts_as_missing():
    """A station that starts in 2000 covers only 5 of the 25 calibration years."""
    time = pd.date_range("2000-01-01", "2004-12-31")
    ref = _ref(time, {"LATE": np.ones(len(time))})

    window = calibration_window(ref, ("1980", "2004"))
    qa: list[QACheck] = []
    table = check_coverage(ref, ("1980", "2004"), qa, name="tmax", min_coverage=0.8)

    assert str(window["time"].values[0])[:10] == "1980-01-01"
    assert table["coverage"].iloc[0] == pytest.approx(5 / 25, abs=0.01)
    assert any(c.name == "coverage:tmax:low" for c in qa)


def test_short_calibration_period_recommends_30_years():
    time = pd.date_range("2000-01-01", "2012-12-31")
    qa: list[QACheck] = []
    check_coverage(
        _ref(time, {"A": np.ones(len(time))}),
        ("2000", "2012"),
        qa,
        name="precip",
        min_coverage=0.8,
    )
    short = next(c for c in qa if c.name == "coverage:precip:short_period")
    assert short.status == "warn" and short.context["recommended"] == 30
    assert short.context["years"] == pytest.approx(13, abs=0.1)


def test_threshold_is_configurable(toy_config):
    cfg = copy.deepcopy(toy_config)
    cfg["periods"] = {"calibration": ["2000", "2012"], "min_coverage": 0.5}
    assert load_config(cfg).periods.min_coverage == 0.5
    for bad in (0, 1.5):
        cfg["periods"]["min_coverage"] = bad
        with pytest.raises(ValidationError):
            load_config(cfg)


def _drop_tmax_before(toy_config, station: str, before: str) -> None:
    path = Path(toy_config["stations"]["data_dir"]) / f"{station}.csv"
    df = pd.read_csv(path)
    df.loc[pd.to_datetime(df["date"]) < before, "tmax"] = np.nan
    df.to_csv(path, index=False)


def test_run_warns_before_loading_the_model(toy_config):
    _drop_tmax_before(toy_config, "ST02", "2010-01-01")
    cfg = {
        **toy_config,
        "variables": ["tmax"],
        "periods": {"calibration": ["2000", "2012"]},
    }
    del cfg["correction"]["precip"]

    out = run_pipeline(cfg)

    names = [c.name for c in out["qa"]]
    low = next(c for c in out["qa"] if c.name == "coverage:tmax:low")
    assert [s["station"] for s in low.context["stations"]] == ["ST02"]
    assert names.index("coverage:tmax:low") < names.index("extract:tmax:hist:nearest")
    assert out["results"]["tmax"] is not None  # the station is kept, only reported


def test_validate_reports_sparse_stations_without_reading_the_model(
    toy_config, tmp_path, capsys
):
    _drop_tmax_before(toy_config, "ST03", "2012-01-01")
    cfg = copy.deepcopy(toy_config)
    cfg["periods"] = {"calibration": ["2000", "2012"]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    assert main(["validate", str(path), "--json"]) == 0  # warnings do not invalidate

    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True
    low = next(w for w in result["warnings"] if w["name"] == "coverage:tmax:low")
    assert low["variable"] == "tmax" and low["loc"] == ["stations"]
    assert low["context"]["stations"][0]["station"] == "ST03"
    assert any(w["name"] == "coverage:precip:short_period" for w in result["warnings"])


def test_validate_fails_when_no_station_has_the_variable(toy_config, tmp_path, capsys):
    for f in Path(toy_config["stations"]["data_dir"]).glob("*.csv"):
        pd.read_csv(f).drop(columns=["tmax"]).to_csv(f, index=False)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(toy_config), encoding="utf-8")

    assert main(["validate", str(path), "--json"]) == 1

    errors = json.loads(capsys.readouterr().out)["errors"]
    assert {(e["variable"], tuple(e["loc"])) for e in errors} == {
        ("tmax", ("variables",))
    }
