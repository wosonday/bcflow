"""Gridded observations as the reference: config, run, and written files."""

from __future__ import annotations

import copy
import json

import numpy as np
import pandas as pd
import pytest
import xarray as xr
import yaml
from pydantic import ValidationError

from bcflow.cli import main
from bcflow.config_schema import load_config
from bcflow.pipeline import run_pipeline
from bcflow.writers import write_run


def test_exactly_one_reference(toy_config, grid_config):
    both = {**grid_config, "stations": toy_config["stations"]}
    with pytest.raises(ValidationError, match="exactly one reference"):
        load_config(both)
    neither = {k: v for k, v in grid_config.items() if k != "grid"}
    with pytest.raises(ValidationError, match="exactly one reference"):
        load_config(neither)


def test_regrid_defaults_and_override(grid_config):
    cfg = load_config(grid_config)
    assert cfg.spec("precip").regrid == "conservative"
    assert cfg.spec("tmax").regrid == "bilinear"
    raw = copy.deepcopy(grid_config)
    raw["correction"]["tmax"]["regrid"] = "bicubic"
    assert load_config(raw).spec("tmax").regrid == "bicubic"
    raw["correction"]["tmax"]["regrid"] = "spline"
    with pytest.raises(ValidationError):
        load_config(raw)


def test_grid_run_corrects_every_land_cell(grid_config):
    out = run_pipeline(grid_config)

    assert all(c.status != "fail" for c in out["qa"]), [
        c.message for c in out["qa"] if c.status == "fail"
    ]
    tmax = out["results"]["tmax"]
    assert tmax["grid"].size == 27 * 10 - 12  # sea corner skipped
    assert tmax["scen"].sizes["station"] == tmax["grid"].size
    done = next(c for c in out["qa"] if c.name == "train_adjust:tmax:done")
    assert "regrid=bilinear, 3 block(s)" in done.message
    # the model was too warm by ~2 degC; the correction removes most of it
    cal = tmax["evaluation"].query("metric == 'mean_bias' and period == 'calibration'")
    assert cal["corrected"].abs().mean() < cal["raw"].abs().mean() / 4


def test_blocks_do_not_change_the_result(grid_config):
    one = {
        **grid_config,
        "variables": ["tmax"],
        "correction": {"tmax": grid_config["correction"]["tmax"]},
    }
    many = copy.deepcopy(one)
    one["grid"] = {**one["grid"], "block_size": 10_000}

    a = run_pipeline(one)["results"]["tmax"]["scen"]
    b = run_pipeline(many)["results"]["tmax"]["scen"]

    np.testing.assert_allclose(
        a.values, b.sel(station=a["station"].values).values, rtol=1e-6
    )


def test_region_limits_the_cells(grid_config):
    cfg = {
        **grid_config,
        "variables": ["tmax"],
        "correction": {"tmax": grid_config["correction"]["tmax"]},
    }
    cfg["grid"] = {**cfg["grid"], "region": {"lat": [15, 18], "lon": [105, 107]}}

    target = run_pipeline(cfg)["results"]["tmax"]["grid"]

    assert target.point_lat.min() >= 15 and target.point_lat.max() <= 18
    assert target.point_lon.min() >= 105 and target.point_lon.max() <= 107


def test_bad_units_need_an_override(grid_config):
    cfg = {
        **grid_config,
        "variables": ["tmax"],
        "correction": {"tmax": grid_config["correction"]["tmax"]},
    }
    cfg["grid"] = {k: v for k, v in cfg["grid"].items() if k != "units"}

    out = run_pipeline(cfg)

    assert out["results"]["tmax"] is None
    check = next(c for c in out["qa"] if c.name == "grid:tmax:ref:bad_units")
    assert "grid.units.tmax" in check.message


def test_written_files_are_on_the_reference_grid(grid_config, tmp_path):
    cfg = {
        **grid_config,
        "variables": ["precip"],
        "correction": {"precip": grid_config["correction"]["precip"]},
    }
    cfg["output"] = {"path": str(tmp_path / "out"), "formats": ["netcdf", "csv"]}
    out = run_pipeline(cfg)

    manifest = write_run(out, load_config(cfg).output)

    var_dir = manifest.parent / "precip"
    with xr.open_dataset(var_dir / "scen.nc") as scen:
        assert scen["precip"].dims == ("time", "lat", "lon")
        assert scen.sizes["lat"] == 27 and scen.sizes["lon"] == 10
        assert bool(
            scen["precip"].isel(time=0, lat=0, lon=-1).isnull()
        )  # sea stays empty
        assert float(scen["precip"].min()) >= 0.0
    with xr.open_dataset(var_dir / "hist_eval.nc") as he:
        assert {"obs", "raw", "corrected", "period"} <= set(he.variables)
    head = pd.read_csv(var_dir / "scen.csv", nrows=5)
    assert list(head.columns) == ["time", "lat", "lon", "precip"]
    head = pd.read_csv(var_dir / "hist_eval.csv", nrows=5)
    assert list(head.columns) == [
        "time",
        "lat",
        "lon",
        "period",
        "obs",
        "raw",
        "corrected",
    ]


def test_validate_checks_the_grid(grid_config, tmp_path, capsys):
    bad = copy.deepcopy(grid_config)
    bad["grid"]["var_map"]["tmax"] = "tx"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")

    assert main(["validate", str(path), "--json"]) == 1

    errors = json.loads(capsys.readouterr().out)["errors"]
    assert [(e["variable"], e["loc"]) for e in errors] == [
        ("tmax", ["grid", "var_map", "tmax"])
    ]
