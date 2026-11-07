"""Tests for writers.write_run and the CLI wiring: the on-disk layout of a run."""

from __future__ import annotations

import copy
import json
from typing import get_args

import pandas as pd
import xarray as xr
import yaml

from bcflow.cli import main
from bcflow.config_schema import OutputConfig, OutputFormat
from bcflow.pipeline import run_pipeline
from bcflow.writers import _WRITERS, write_run


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_output_formats_in_schema_match_registered_writers():
    """Otherwise a format could pass config validation but have no writer."""
    assert set(get_args(OutputFormat)) == set(_WRITERS)


def test_write_run_writes_every_variable_qa_and_manifest(toy_config, tmp_path):
    out = run_pipeline(toy_config)
    manifest_path = write_run(
        out,
        OutputConfig(path=str(tmp_path), formats=["netcdf", "csv"]),
        config=toy_config,
    )
    run_dir = manifest_path.parent

    manifest = _read_json(manifest_path)
    assert (
        manifest["schema_version"] == 1
        and manifest["qa"] == "qa.json"
        and manifest["config"] == "config.json"
    )
    variables = manifest["variables"]
    assert {name: v["status"] for name, v in variables.items()} == {
        "tmax": "ok",
        "precip": "ok",
    }
    # Relative paths: the run folder stays valid when moved or shared.
    assert variables["tmax"]["scen"] == {
        "netcdf": "tmax/scen.nc",
        "csv": "tmax/scen.csv",
    }
    assert variables["precip"]["adj"] == "precip/adj.nc"
    assert variables["precip"]["evaluation"] == "precip/evaluation.csv"
    assert variables["precip"]["hist_eval"] == {
        "netcdf": "precip/hist_eval.nc",
        "csv": "precip/hist_eval.csv",
    }

    with xr.open_dataset(run_dir / "tmax" / "scen.nc") as ds:
        assert "tmax" in ds.data_vars
        assert ds.sizes["station"] == out["results"]["tmax"]["scen"].sizes["station"]

    csv = pd.read_csv(run_dir / "precip" / "scen.csv", index_col="time")
    assert len(csv) == out["results"]["precip"]["scen"].sizes["time"]

    evaluation = pd.read_csv(run_dir / "precip" / "evaluation.csv")
    assert {"period", "station", "metric", "raw", "corrected", "improved"} <= set(
        evaluation.columns
    )
    hist_csv = pd.read_csv(run_dir / "precip" / "hist_eval.csv")
    assert list(hist_csv.columns) == [
        "time",
        "station",
        "period",
        "obs",
        "raw",
        "corrected",
    ]
    with xr.open_dataset(run_dir / "precip" / "hist_eval.nc") as ds:
        assert {"obs", "raw", "corrected"} <= set(
            ds.data_vars
        ) and "period" in ds.coords

    assert len(_read_json(run_dir / "qa.json")) == len(out["qa"])
    assert _read_json(run_dir / "config.json") == toy_config


def test_failed_variable_is_listed_in_manifest_and_others_still_written(
    toy_config, tmp_path
):
    bad = copy.deepcopy(toy_config)
    bad["correction"]["tmax"]["method"] = "does_not_exist"
    out = run_pipeline(bad)

    manifest_path = write_run(out, OutputConfig(path=str(tmp_path)))
    variables = _read_json(manifest_path)["variables"]

    assert variables["tmax"] == {"status": "failed"}
    assert variables["precip"]["status"] == "ok"
    assert not (manifest_path.parent / "tmax").exists()
    assert (manifest_path.parent / "precip" / "scen.nc").is_file()


def test_write_failure_is_isolated_and_reported_in_qa(toy_config, tmp_path):
    """tmax gets an extra dimension, so its CSV cannot be written; precip must
    still be written and the failure must be visible in out['qa']."""
    out = run_pipeline(toy_config)
    out["results"]["tmax"]["scen"] = out["results"]["tmax"]["scen"].expand_dims(
        extra=[0]
    )

    manifest_path = write_run(out, OutputConfig(path=str(tmp_path), formats=["csv"]))
    variables = _read_json(manifest_path)["variables"]

    assert variables["tmax"] == {"status": "failed"}
    assert variables["precip"]["status"] == "ok"
    assert any(
        c.name == "writers:tmax:exception" and c.status == "fail" for c in out["qa"]
    )


def test_runs_never_overwrite_each_other(tmp_path):
    empty = {"results": {}, "qa": []}
    cfg = OutputConfig(path=str(tmp_path))
    first, second = write_run(empty, cfg), write_run(empty, cfg)  # same second
    assert first.parent != second.parent
    assert first.is_file() and second.is_file()


def test_cli_writes_outputs_relative_to_config_file(toy_config, tmp_path):
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    config_file = cfg_dir / "run.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {**toy_config, "output": {"path": "../results", "formats": ["netcdf"]}}
        ),
        encoding="utf-8",
    )

    assert main(["run", str(config_file)]) == 0

    # ../results is resolved from configs/, not from the current directory.
    run_dirs = list((tmp_path / "results").iterdir())
    assert len(run_dirs) == 1
    assert (
        _read_json(run_dirs[0] / "manifest.json")["variables"]["tmax"]["status"]
        == "ok"
    )


def test_cli_rejects_invalid_output_section_before_running(
    toy_config, tmp_path, capsys
):
    config_file = tmp_path / "run.yaml"
    config_file.write_text(
        yaml.safe_dump({**toy_config, "output": {"formats": ["excel"]}}),
        encoding="utf-8",
    )

    assert main(["run", str(config_file)]) == 2
    assert "formats" in capsys.readouterr().err
    assert not (tmp_path / "outputs").exists()
